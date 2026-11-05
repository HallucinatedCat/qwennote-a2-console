# QwenNote A2 host console

An unofficial, self-hosted backend and web console for the **QwenNote A2** voice recorder. It runs on your own machine, talks to the vendor cloud with **your own account**, and gives you a local browser UI for your recordings, notes and device settings.

> **Unofficial project.** It is not affiliated with, endorsed by or supported by QwenNote, DingTalk, Alibaba or any related company. Product names belong to their owners. It is meant only for **your own account and your own device**. Using it may conflict with the vendor's terms of service, and the vendor can change or block its interfaces at any time. You use it at your own risk; there is no warranty (see [LICENSE](LICENSE)).

## What it does

- **Recorder:** start and stop recordings, see the recorder's real state (read back from the device and the cloud, not just the last command), and an optional live transcript.
- **Notes:** list and search notes; read summaries and transcripts (with speakers and times); edit and regenerate summaries; find and replace in transcripts; manage tags; favorite; share; translate; export as TXT, Markdown, DOCX, PDF or SRT; save the original audio.
- **Memos and assistant:** read voice memos, and ask questions about a note through the built-in assistant or your own OpenAI-compatible provider.
- **Settings:** recording, transcription, summary, alert and device-name settings, plus membership quotas and 4G usage.
- **Documents (MCP):** a separate client for the vendor's official OAuth/MCP document service (read-only by default).
- **Dev tools:** credential health, request timings, protocol keys and diagnostics. Destructive actions (delete, unbind) are hidden behind an explicit switch and a typed confirmation.
- English and Simplified Chinese UI.

What it does **not** do: firmware updates, device reset, voice/speaker playback on the device, or anything that needs a device you do not own.

## Live demo

**[qwennote-a2-console-demo.pages.dev](https://qwennote-a2-console-demo.pages.dev/)** runs the real web UI in your browser with invented data. There is no backend: the API is answered in the page by the same simulation as `tools/ui_preview.py` (via Pyodide). It is read-only, so anything that would change a recorder, note or setting is refused. Use the switcher at the bottom to try the `empty`, `recording`, `attention` and `worst` scenarios. Build it yourself with `python3 tools/build_demo.py`.

## Layout

| Path | What it is |
| --- | --- |
| [`artifacts/standalone-session/`](artifacts/standalone-session/README.md) | The backend (`web_backend.py`), web UI (`web.html`, `i18n/`) and protocol modules, with tests |
| [`artifacts/mcp-reader/`](artifacts/mcp-reader/README.md) | Command-line client for the official OAuth/MCP document service |
| [`tools/ui_preview.py`](tools/ui_preview.py) | Runs the UI with synthetic data and no vendor connection, for UI work |
| [`tools/build_demo.py`](tools/build_demo.py) | Builds the static read-only demo into `dist/demo/` |
| [`tools/rtc_probe.py`](tools/rtc_probe.py) | Command-line check of the live-transcript (RTC) session |
| [`tools/a2-ble/`](tools/a2-ble/README.md) | Experimental, paused Bluetooth client for Windows |
| [`docs/credentials.md`](docs/credentials.md) | Which credentials the backend needs and how to import them |
| [`docs/host-api.md`](docs/host-api.md) | The backend's own HTTP API: every route, its guards, cache and recovery rules |

## Quick start

Requirements: Linux or macOS, Python 3.11+, a QwenNote account with a bound A2.

```sh
git clone https://github.com/HallucinatedCat/qwennote-a2-console.git
cd qwennote-a2-console/artifacts/standalone-session
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt            # add -r requirements-rtc.txt for live transcript
```

Just want to see the UI? From the repository root run `python3 tools/ui_preview.py` and open the printed address. It uses synthetic data only.

To use your own account:

1. **Get your session credentials.** This project does **not** ship tools or instructions for extracting them from the vendor app. [docs/credentials.md](docs/credentials.md) lists exactly what the backend needs; obtaining it is up to you.
2. **Import them:** `.venv/bin/python import_session.py --source /path/to/native-session.json`
3. **Start the console:** `.venv/bin/python web_backend.py --mcp-root ../mcp-reader`
4. Open **http://127.0.0.1:18780/**.

The document (MCP) features use the vendor's own OAuth login instead, which you can start from the console's Settings or from the [MCP reader](artifacts/mcp-reader/README.md).

Full installation, service setup and tests: [DEPLOYMENT.md](artifacts/standalone-session/DEPLOYMENT.md).

## Security

- The console listens on **127.0.0.1 only**. Anyone who can reach that port on your machine gets full control of your recorder and notes, so do not run it on a shared multi-user host. For remote use, forward the port over SSH; never expose it publicly.
- Credentials are stored in `.private/` (directory 0700, files 0600). Never commit, paste or share them. `.gitignore` excludes `.private/`.
- The UI uses a per-session cookie, a CSRF token, Host/Origin checks and a strict CSP.
- Please report security problems privately via GitHub's "Report a vulnerability" rather than as a public issue.

## Development

Run the tests from the repository root (with the venv created above):

```sh
artifacts/standalone-session/.venv/bin/python -m unittest discover -s artifacts/standalone-session -q
artifacts/standalone-session/.venv/bin/python -m unittest discover -s artifacts/mcp-reader -q
python3 -m unittest discover -s tools/a2-ble -q
node tools/web-checks/check-web-ui.js
```

The UI can be replaced without touching the backend; see [FRONTEND.md](artifacts/standalone-session/FRONTEND.md) and [FRONTEND_API.md](artifacts/standalone-session/FRONTEND_API.md).

## License

[GPL-3.0](LICENSE).
