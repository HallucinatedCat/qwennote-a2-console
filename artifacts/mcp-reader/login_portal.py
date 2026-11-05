"""Loopback browser handoff for OAuth. No phone number or SMS input here."""
from __future__ import annotations

import asyncio
import html
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class LoginTimeout(Exception):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__("Login wait expired")


# RFC 6749 section 4.1.2.1 authorization error codes. Anything else is reported as "other".
OAUTH_ERRORS = frozenset({"access_denied", "invalid_request", "unauthorized_client", "unsupported_response_type",
                          "invalid_scope", "server_error", "temporarily_unavailable"})


class LoginDenied(Exception):
    """The authorization server ended the flow. ``reason`` is always an allowlisted code."""

    def __init__(self, reason: str = "access_denied"):
        self.reason = reason if reason in OAUTH_ERRORS else "other"
        super().__init__("Authorization ended: " + self.reason)


class Callback:
    def __init__(self, port: int, *, validator, ready_timeout: int = 1800, login_timeout: int = 900, scope: str = "qwennote:read"):
        self.scope = scope  # the only scope this login may request
        self.expected_state = ""
        self.completed = False
        self.result: queue.Queue = queue.Queue(maxsize=1)
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.guard = threading.Lock()
        self.authorization_url = ""
        self.phase = "preparing"
        self.csrf = secrets.token_urlsafe(32)
        self.ready_timeout = ready_timeout
        self.login_timeout = login_timeout
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Callback URLs contain authorization codes.

            def reply(self, status, text="", *, location=None, markup=False):
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8" if markup else "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://minutes.qwennote.cn; frame-ancestors 'none'; base-uri 'none'")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(text.encode())

            def valid_host(self):
                return self.headers.get("Host") in owner.allowed_hosts

            def do_GET(self):
                if not self.valid_host():
                    self.reply(403, "Use the local loopback address.")
                    return
                path = urlsplit(self.path).path
                if path == "/":
                    self.reply(200, owner.page(), markup=True)
                    return
                if path != "/callback":
                    self.reply(404, "Not found")
                    return
                try:
                    params = parse_qs(urlsplit(self.path).query)
                    if params.get("error"):
                        # Any error callback with a valid state is terminal; waiting would only run out the login timeout.
                        states = params.get("state", [])
                        if len(states) != 1 or not owner.expected_state or not secrets.compare_digest(states[0], owner.expected_state):
                            raise ValueError("Invalid state")
                        if "iss" in params and params["iss"] != ["https://minutes.qwennote.cn"]:
                            raise ValueError("Invalid issuer")
                        value = LoginDenied(params["error"][0])
                    else:
                        value = validator(self.path, owner.expected_state)
                    with owner.guard:
                        if owner.completed or owner.stopped.is_set():
                            raise ValueError("Callback closed")
                        owner.completed = True
                        owner.phase = "denied" if isinstance(value, LoginDenied) else "exchanging"
                        owner.result.put_nowait(value)
                except (ValueError, queue.Full):
                    self.reply(400, "Invalid or already completed callback.")
                    return
                # Remove the code from the visible URL and never echo it in HTML.
                self.reply(303, location="/")

            def do_POST(self):
                if not self.valid_host() or urlsplit(self.path).path != "/start":
                    self.reply(403, "Invalid login action.")
                    return
                origin = self.headers.get("Origin", "")
                # Privacy policies can suppress Origin or serialize it as null.
                # The loopback Host check and per-page CSRF token still apply.
                expected_origins = {"http://" + host for host in owner.allowed_hosts}
                if origin not in expected_origins and origin not in {"", "null"}:
                    self.reply(403, "Open http://127.0.0.1:" + str(owner.server.server_address[1]) + "/ directly in your mapped browser and try again.")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024:
                        raise ValueError()
                    body = parse_qs(self.rfile.read(length).decode("utf-8"))
                    csrf = body.get("csrf", [])
                    if len(csrf) != 1 or not secrets.compare_digest(csrf[0], owner.csrf):
                        raise ValueError()
                except (ValueError, UnicodeDecodeError):
                    self.reply(403, "Invalid login action.")
                    return
                with owner.guard:
                    if owner.completed or owner.stopped.is_set():
                        self.reply(409, "This login is finished. Return to the local page.")
                        return
                    if not owner.authorization_url:
                        self.reply(503, "Sign-in is being prepared. Refresh the local page.")
                        return
                    owner.phase = "authorizing"
                    owner.started.set()
                    url = owner.authorization_url
                # A vendor consent request is created only when the browser follows this.
                self.reply(303, location=url)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        actual_port = self.server.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
        self.local_url = f"http://127.0.0.1:{actual_port}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def page(self):
        messages = {
            "preparing": ("Preparing sign-in", "Please wait a moment. This page will refresh."),
            "ready": ("Connect QwenNote", "Continue to QwenNote and enter your phone number and SMS code on its page. Access is read-only."),
            "authorizing": ("Finish sign-in on QwenNote", "If QwenNote says the request expired, return here and press Continue again to open a fresh consent request."),
            "exchanging": ("Authorization received", "Finishing the connection and retrieving available tools. This page will refresh."),
            "done": ("QwenNote connected", "Authorization completed and the tool list was retrieved. You can close this tab and return to the conversation."),
            "denied": ("Authorization declined", "No account connection was completed. You can close this tab."),
            "expired": ("Login waiting period ended", "The reader needs a fresh login run. No code should be sent in chat."),
            "error": ("Connection did not finish", "Return to the conversation for the connection status. No code should be sent in chat."),
        }
        title, message = messages.get(self.phase, messages["error"])
        refresh = '<meta http-equiv="refresh" content="2">' if self.phase in {"preparing", "exchanging"} else ""
        form = ""
        if self.phase in {"ready", "authorizing"}:
            form = ('<form method="post" action="/start"><input type="hidden" name="csrf" value="'
                    + html.escape(self.csrf, quote=True) + '"><button type="submit">Continue to QwenNote</button></form>')
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                + refresh + '<title>' + html.escape(title) + '</title><style>body{font:18px system-ui;max-width:600px;margin:10vh auto;padding:24px;line-height:1.6;color:#17232d;background:#f6f8fa}main{background:white;border:1px solid #dde3e9;border-radius:16px;padding:28px}button{font:inherit;border:0;border-radius:8px;padding:12px 20px;color:white;background:#225ad6;cursor:pointer}p{color:#465766}</style></head><body><main><h1>'
                + html.escape(title) + '</h1><p>' + html.escape(message) + '</p>' + form + '</main></body></html>')

    async def redirect(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "minutes.qwennote.cn" or parsed.path != "/authorize":
            raise ValueError("Unexpected authorization endpoint")
        params = parse_qs(parsed.query)
        if params.get("scope") != [self.scope]:
            raise ValueError("Unexpected authorization scope")
        state = params.get("state", [])
        if len(state) != 1 or not state[0]:
            raise ValueError("Missing OAuth state")
        with self.guard:
            self.expected_state = state[0]
            self.authorization_url = url
            self.phase = "ready"
        print(f"Login ready: {self.local_url}\nMap this port to your browser, open the page, and press Continue. Authorization starts then.", flush=True)
        deadline = time.monotonic() + self.ready_timeout
        while not self.started.is_set():
            if self.stopped.is_set():
                raise LoginTimeout("closed")
            if time.monotonic() >= deadline:
                self.phase = "expired"
                raise LoginTimeout("start")
            await asyncio.sleep(0.1)
        print("Browser sign-in started. Enter credentials only on the vendor page.", flush=True)

    async def receive(self):
        deadline = time.monotonic() + self.login_timeout
        while not self.stopped.is_set():
            try:
                value = self.result.get_nowait()
                if isinstance(value, Exception):
                    raise value
                return value
            except queue.Empty:
                if time.monotonic() >= deadline:
                    self.phase = "expired"
                    raise LoginTimeout("callback")
                await asyncio.sleep(0.1)
        raise LoginTimeout("closed")

    def close(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
