# Installation and operation

Paths below are relative to this directory (`artifacts/standalone-session/`) unless they say otherwise.

## Install

Python 3.11+ on Linux or macOS. Linux is the tested platform.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-rtc.txt   # optional: live transcript (aiortc); also needed by the full test suite
```

No vendor SDK, app, keys or secrets are included or downloaded.

## Credentials

1. Prepare your native session file and import it. See [docs/credentials.md](../../docs/credentials.md).

   ```sh
   .venv/bin/python import_session.py --source /path/to/private/native-session.json
   ```

2. Document (MCP) login is separate. Start it later from **Settings** in the console, or with the [MCP reader](../mcp-reader/README.md).

## Run

```sh
.venv/bin/python web_backend.py --mcp-root ../mcp-reader
```

Then open **http://127.0.0.1:18780/**.

| Option | Default | Meaning |
| --- | --- | --- |
| `--state DIR` | `./.private` | Private state directory (credentials, journals, cache) |
| `--port N` | `18780` | Loopback port |
| `--mcp-root DIR` | none | Directory of the MCP reader; enables document features |
| `--frontend FILE` | built-in `web.html` | Use a replacement HTML template |

The server binds to `127.0.0.1` only. For access from another computer, forward the port over SSH (`ssh -N -L 18780:127.0.0.1:18780 you@host`) and open the same address locally. Do not expose it publicly or run it on a shared multi-user host: anyone who can reach the port can control the recorder.

`GET /healthz` needs no cookie and reports whether a restart is safe right now (`restart_safe`), and why not (`blocking`), without identifiers. Before restarting, make sure no recording, live transcript or capture is running. A restart changes the UI cookie; reload the page.

### systemd (optional)

Example user unit. Adjust the paths to your checkout:

```ini
# ~/.config/systemd/user/qwennote-console.service
[Unit]
Description=QwenNote A2 host console
After=network-online.target

[Service]
WorkingDirectory=%h/qwennote-a2-console/artifacts/standalone-session
ExecStart=%h/qwennote-a2-console/artifacts/standalone-session/.venv/bin/python web_backend.py --mcp-root ../mcp-reader
Restart=on-failure
TimeoutStopSec=120
UMask=0077

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now qwennote-console.service
```

`TimeoutStopSec` gives a running live-transcript session time to clean up on shutdown.

## Replace the frontend template

The default `web.html` is a standalone browser client. To use your own copy, add `--frontend /absolute/path/to/page.html`. Adding or removing the option needs a restart. After that, the file is read on every page request, so you can edit it and just reload the browser. The backend still injects the CSRF token, a fresh CSP nonce and the translation catalogs, and still enforces every API guard.

The file must be UTF-8, at most 2 MiB, with exactly one `__CSRF__`, `__NONCE__` and `__I18N__` placeholder. See [FRONTEND.md](FRONTEND.md), [FRONTEND_API.md](FRONTEND_API.md) and `frontend-contract.json`.

## Custom assistant provider (optional)

Create `.private/assistant.json` (mode 0600):

```json
{"url": "https://provider.example/v1/chat/completions", "model": "model-name", "api_key": "...", "system_prompt": "optional"}
```

The provider must speak the OpenAI-style chat-completions format, over HTTPS (or plain HTTP on loopback). Only text you explicitly submit is sent. Without this file, the built-in assistant is used.

## Device actions and safety

- Device writes (recording, settings, device name) need the controls to be armed first. One confirmation arms them until you press Disarm.
- Destructive actions (delete note or recording, clear voiceprint, unbind) appear only after you enable them in Dev tools, and each asks for a typed word.
- Writes are journaled before they are sent. An ambiguous write is never retried automatically; the next refresh reconciles it instead.
- Setting changes keep restore journals in `.private/`, so earlier values can be restored.

## Tests

From the repository root, with both requirement files installed:

```sh
artifacts/standalone-session/.venv/bin/python -m unittest discover -s artifacts/standalone-session -q
PYTHONPATH=artifacts/standalone-session artifacts/standalone-session/.venv/bin/python -m unittest discover -s tools -q
artifacts/standalone-session/.venv/bin/python -m unittest discover -s artifacts/mcp-reader -q
python3 -m unittest discover -s tools/a2-ble -q
node tools/web-checks/check-web-ui.js        # after editing web.html or i18n/*.json
node tools/web-checks/test-quota-format.js
```

`test_i18n.py`, `test_public_errors.py` and `test_credential_status.py` also run with a plain `python3`. All tests use synthetic data and local loopback servers; they never contact the vendor or a device.

## Live transcript probe

`tools/rtc_probe.py` tests the live-transcript (RTC) session from the command line. Give it its **own** native session in a separate state directory. Never point it at the running console's session, because only one program may renew a session. Audio is received only: it is counted, not saved or played.
