#!/usr/bin/env python3
"""QwenNote MCP client: read tools on the read grant; write tools only on a separate,
write grant the user consented to (its own --state-dir).

Requires Python 3.11+, mcp==1.27.1 and httpx==0.28.1. Use on the machine
running your browser, or forward the callback port over SSH. Credentials and
results stay in --state-dir, with private permissions. See README.md.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
from login_portal import Callback as LoginPortal, LoginDenied, LoginTimeout
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata,
    OAuthToken, ProtectedResourceMetadata,
)

SERVER = "https://minutes.qwennote.cn/mcp"
ISSUER = "https://minutes.qwennote.cn"
REQUEST_TIMEOUT = 60  # Total noninteractive read budget, including discovery and stream cleanup.
READ_TOOLS = frozenset({
    "list_my_minutes", "list_minutes_tags", "list_minutes_by_tag",
    "get_minutes_basic_info", "batch_get_minutes_details",
    "get_minutes_transcription", "get_minutes_ai_summary",
    "get_minutes_keywords", "list_minutes_todos", "get_minutes_media_url",
    "list_hot_words", "list_audio_memos", "list_custom_templates",
    "list_system_templates",
})
# Design: MCP writes as an alternative to the native application path.
# Names and arguments follow the public DingTalk Workspace CLI (github.com/DingTalk-Real-AI/dingtalk-workspace-cli);
# a tool runs only if the server advertises it to the write grant.
WRITE_TOOLS = frozenset({
    "update_minutes_title", "update_minutes_summary", "replace_minutes_text",
    "replace_speaker", "add_personal_hot_word", "delete_personal_hotword",
})
READ_SCOPE = "qwennote:read"
WRITE_SCOPE = "qwennote:read qwennote:write"


def private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def private_read(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError(f"Private file permissions required: {path.name}")
    return json.loads(path.read_text())


class Storage:
    """SDK token storage that persists the absolute expiry. Takes no lock: callers hold session.lock.
    client_root, when given, holds an existing client registration to reuse (the write store
    reuses the read store's public client, so no second client is registered)."""
    def __init__(self, root: Path, client_root: Path | None = None):
        self.root = root
        self.client_root = client_root or root

    async def get_tokens(self) -> OAuthToken | None:
        stored = private_read(self.root / "tokens.json")
        if stored is None:
            return None
        data = dict(stored["tokens"])
        if stored.get("expires_at") is not None:
            data["expires_in"] = max(0, int(stored["expires_at"] - time.time()))
        return OAuthToken.model_validate(data)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        private_json(self.root / "tokens.json", {
            "tokens": tokens.model_dump(mode="json"),
            "expires_at": None if tokens.expires_in is None else time.time() + tokens.expires_in,
        })

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        # Tokens saved by the console record the client that issued them; refresh with that one.
        stored = private_read(self.root / "tokens.json")
        issued = stored.get("client_id") if isinstance(stored, dict) else None
        data = None
        for path in dict.fromkeys((self.root / "client.json", self.client_root / "client.json")):
            candidate = private_read(path)
            if issued and candidate and candidate.get("client_id") == issued:
                data = candidate
                break
        if data is None:
            data = private_read(self.client_root / "client.json")
        return None if data is None else OAuthClientInformationFull.model_validate(data)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        private_json(self.root / "client.json", client_info.model_dump(mode="json"))


class PersistentOAuthClientProvider(OAuthClientProvider):
    """Small persistence fixes for the pinned MCP SDK 1.27.1.

    Its initializer does not restore expiry; its refresh handler discards a
    previous refresh token when a valid response omits a replacement. Scope
    discovery also overwrites requested scopes; consent is constrained to the
    scope this store was created for (read, or read plus write for the write grant).
    """
    requested_scope = READ_SCOPE

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        # SDK 1.27.1 overwrites the requested scope with advertised scopes after
        # discovery. Keep every consent request limited to this store's scope.
        self.context.client_metadata.scope = self.requested_scope
        return await super()._perform_authorization_code_grant()

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        prior = self.context.current_tokens
        if response.status_code == 200 and prior and prior.refresh_token:
            await response.aread()
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and not data.get("refresh_token"):
                data["refresh_token"] = prior.refresh_token
                response = httpx.Response(200, json=data)
        return await super()._handle_refresh_response(response)


def callback_values(path: str, expected_state: str) -> tuple[str, str]:
    parsed = urlsplit(path)
    if parsed.path != "/callback":
        raise ValueError("Wrong callback path")
    params = parse_qs(parsed.query)
    if any(len(params.get(k, [])) != 1 for k in ("code", "state")):
        raise ValueError("Missing or repeated callback parameter")
    state = params["state"][0]
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise ValueError("OAuth state mismatch")
    if "iss" in params and params["iss"] != [ISSUER]:
        raise ValueError("OAuth issuer mismatch")
    if "error" in params:
        raise ValueError("Authorization was not completed")
    return params["code"][0], state


class Callback(LoginPortal):
    def __init__(self, port: int, **kwargs):
        super().__init__(port, validator=callback_values, **kwargs)


def safe_failure(exc: BaseException) -> str:
    """Classify failures without exposing URLs, credentials or server bodies."""
    leaves = []
    def walk(error):
        if isinstance(error, BaseExceptionGroup):
            for nested in error.exceptions:
                walk(nested)
        else:
            leaves.append(error)
    walk(exc)
    for error in leaves:
        if isinstance(error, LoginTimeout):
            if error.stage == "start":
                return "Timed out before browser sign-in started. Run tools --login again, then open the local page."
            return "No authorization callback arrived within the login window. Check the port mapping and start a fresh login."
        if isinstance(error, LoginDenied):
            if error.reason == "access_denied":
                return "Authorization was declined. No account connection was completed."
            return f"The authorization server ended sign-in ({error.reason}). Start a fresh login. No account connection was completed."
        if isinstance(error, OSError) and error.errno in {48, 98}:
            return "The callback port is occupied. Stop the earlier reader or select the registered port."
        if isinstance(error, (TimeoutError, httpx.TimeoutException)):
            return "A network request timed out. No account access is being claimed."
        if isinstance(error, httpx.HTTPStatusError):
            return f"The service returned HTTP {error.response.status_code}. Response contents were not printed."
    kinds = ", ".join(sorted({type(error).__name__ for error in leaves}))
    return f"Operation failed ({kinds}). Check authorization, port mapping and connectivity. Credentials were not printed."


async def reject_login(*_args):
    raise RuntimeError("Authorization required. Run tools --login locally.")


async def discover() -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        challenge = await client.get(SERVER)
        resource = await client.get(ISSUER + "/.well-known/oauth-protected-resource/mcp")
        metadata = await client.get(ISSUER + "/.well-known/oauth-authorization-server")
        resource.raise_for_status()
        metadata.raise_for_status()
        return {
            "server": SERVER,
            "unauthenticated_status": challenge.status_code,
            "www_authenticate": challenge.headers.get("www-authenticate"),
            "resource_metadata": resource.json(),
            "authorization_server": metadata.json(),
            "authenticated": False,
        }


async def connected(args, root: Path) -> dict:
    # Interactive consent keeps its explicit owner-wait budget; reads have one total deadline.
    budget = args.ready_timeout + args.login_timeout + REQUEST_TIMEOUT + 20 if args.login else REQUEST_TIMEOUT
    async with asyncio.timeout(budget):
        return await _connected(args, root)


async def _connected(args, root: Path) -> dict:
    writing = bool(getattr(args, "write", False))
    # The write store lives inside the read store (.private/write) and reuses its client.
    shared = root.parent if writing and (root.parent / "client.json").is_file() else None
    storage = Storage(root, shared)
    cached = await storage.get_client_info()
    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    # Only an interactive login uses the callback; a console-registered client has another address.
    if args.login and cached and [str(u) for u in cached.redirect_uris or []] != [redirect_uri]:
        raise ValueError("Use the original callback port for this state directory")
    if not args.login and await storage.get_tokens() is None:
        raise RuntimeError("Authorization required. Run tools --login locally.")
    callback = Callback(args.port, ready_timeout=args.ready_timeout, login_timeout=args.login_timeout,
                        scope=WRITE_SCOPE if writing else READ_SCOPE) if args.login else None
    try:
        metadata = await discover()
        scope = WRITE_SCOPE if writing else READ_SCOPE
        auth = PersistentOAuthClientProvider(
            server_url=SERVER,
            client_metadata=OAuthClientMetadata(
                client_name="QwenNote Personal Interoperability Reader",
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"], token_endpoint_auth_method="none",
                scope=scope,
            ),
            storage=storage,
            redirect_handler=callback.redirect if callback else reject_login,
            callback_handler=callback.receive if callback else reject_login,
        )
        auth.requested_scope = scope
        auth.context.oauth_metadata = OAuthMetadata.model_validate(metadata["authorization_server"])
        auth.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(metadata["resource_metadata"])
        if str(auth.context.oauth_metadata.issuer).rstrip("/") != ISSUER:
            raise ValueError("Unexpected OAuth issuer")
        if str(auth.context.protected_resource_metadata.resource) != SERVER:
            raise ValueError("Unexpected OAuth resource")
        async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(60, read=120)) as http:
            async with streamable_http_client(SERVER, http_client=http) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    tools, cursor, seen = [], None, set()
                    for _ in range(100):
                        page = await session.list_tools(cursor=cursor)
                        tools.extend(tool.model_dump(mode="json") for tool in page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                        if cursor in seen:
                            raise RuntimeError("Repeated tools pagination cursor")
                        seen.add(cursor)
                    else:
                        raise RuntimeError("Tool pagination exceeded bound")
                    result = {"initialize": initialized.model_dump(mode="json"), "tools": tools}
                    if args.command in ("read", "write"):
                        allowed = WRITE_TOOLS if args.command == "write" else READ_TOOLS
                        if args.tool not in allowed or (args.command == "write") != bool(getattr(args, "write", False)):
                            raise ValueError("Only the documented tools for this grant are enabled")
                        if args.tool not in {t["name"] for t in tools}:
                            raise ValueError("Requested tool is not currently available")
                        arguments = json.loads(Path(args.arguments).read_text()) if args.arguments else {}
                        if not isinstance(arguments, dict):
                            raise ValueError("Arguments must be a JSON object")
                        called = await session.call_tool(args.tool, arguments)
                        result["call"] = {"tool": args.tool, "result": called.model_dump(mode="json")}
                    if callback:
                        callback.phase = "done"
                    return result
    except BaseException:
        if callback and callback.phase not in {"denied", "expired"}:
            callback.phase = "error"
        raise
    finally:
        if callback:
            # Allow the user's browser to receive the clean result page.
            try:
                if callback.completed:
                    await asyncio.sleep(20)
            finally:
                callback.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(Path(__file__).parent / ".private"))
    parser.add_argument("--port", type=int, default=18765, help="Loopback OAuth callback port (default: 18765)")
    parser.add_argument("--ready-timeout", type=int, default=1800, help="Seconds to wait for the user to start from the local page (default: 1800)")
    parser.add_argument("--login-timeout", type=int, default=900, help="Seconds to wait after browser sign-in starts (default: 900)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="Public metadata only; no registration or login")
    for command in ("tools", "read", "write"):
        item = sub.add_parser(command)
        item.add_argument("--login", action="store_true", help="Allow interactive OAuth and public-client registration")
        if command == "tools":
            item.add_argument("--write", action="store_true", help="Consent to qwennote:write for this store (use a separate --state-dir)")
        if command in ("read", "write"):
            item.add_argument("tool", choices=sorted(READ_TOOLS if command == "read" else WRITE_TOOLS))
            item.add_argument("--arguments", help="JSON arguments file; derive schema from tools output")
    args = parser.parse_args()
    if args.command == "write":
        args.write = True
    if not 1024 <= args.port <= 65535:
        parser.error("Use a callback port between 1024 and 65535")
    if not 60 <= args.ready_timeout <= 7200 or not 60 <= args.login_timeout <= 3600:
        parser.error("Use ready-timeout 60..7200 and login-timeout 60..3600 seconds")
    root = Path(args.state_dir).expanduser().resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.stat().st_mode & 0o077:
        parser.error("State directory must be private (chmod 700)")
    logging.disable(logging.CRITICAL)
    lock_fd = os.open(root / "session.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = asyncio.run(discover() if args.command == "discover" else connected(args, root))
        path = root / (args.command + "-result.json")
        private_json(path, result)
        print(f"Saved {args.command} result: {path}")
        if "tools" in result:
            print("Available tool names: " + ", ".join(t["name"] for t in result["tools"]))
        if result.get("call", {}).get("result", {}).get("isError"):
            print("The tool returned an error; inspect the private result file.", file=sys.stderr)
            return 2
        return 0
    except BlockingIOError:
        print("Another client holds this credential store; wait for it to finish.", file=sys.stderr)
        return 2
    except Exception as exc:
        # SDK/network exceptions may contain credential-bearing URLs or response bodies.
        print(safe_failure(exc), file=sys.stderr)
        return 2
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
