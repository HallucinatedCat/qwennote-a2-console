# QwenNote MCP document reader

A small command-line client for the vendor's official OAuth/MCP document service. It reads your notes, transcripts, summaries and memos. It uses the Python MCP SDK for OAuth PKCE, token refresh and streamable HTTP. It does not control the device; that is the host backend's job.

It is **read-only by default**. Writes need a separate, explicit grant (see below).

## Install

Python 3.11+ on Linux or macOS. Keep the whole directory together, including `login_portal.py`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Log in

```sh
.venv/bin/python qwennote_reader.py tools --login
```

Open **http://127.0.0.1:18765/** in your browser and press **Continue to QwenNote**. Enter your phone number and SMS code only on the vendor's own HTTPS page, then approve the requested permission. The callback comes back to the local page, the reader stores the credentials privately and fetches the tool list.

On a remote machine, forward the same port first: `ssh -N -L 18765:127.0.0.1:18765 you@host`.

- The local page waits up to 30 minutes for you to start, then 15 minutes after **Continue**. If the vendor page says the request expired, go back to the local page and press **Continue** again. This does not send another SMS on its own.
- The reader never prints the authorization code or the full authorization URL.
- Timeouts: `--ready-timeout SECONDS` and `--login-timeout SECONDS`, placed before the subcommand. `--port` and `--state-dir` are global options too. Keep the same callback port for a saved client.
- Login only happens when you pass `--login`.

## Read

After login, `.private/tools-result.json` holds the live tool list and the argument schemas. Write an arguments file from those schemas, then:

```sh
.venv/bin/python qwennote_reader.py read list_my_minutes --arguments list-args.json
.venv/bin/python qwennote_reader.py read get_minutes_transcription --arguments transcript-args.json
```

A tool must be on the reader's read allowlist **and** in the live inventory before it is called.

## Write (opt-in)

```sh
.venv/bin/python qwennote_reader.py --state-dir .private-write tools --login --write
.venv/bin/python qwennote_reader.py --state-dir .private-write write <tool> --arguments args.json
```

`--write` asks the vendor for the write scope. Use a separate `--state-dir` for it, so the read-only store keeps its narrower permission. Run `qwennote_reader.py write --help` for the list of write tools.

## Storage

Credentials and results live in `.private/` (directory 0700, files 0600). A process lock coordinates token refresh, so do not run two readers on the same store at the same time, and do not let another tool use the store without the lock. The local page never asks for a password or SMS code.

## Tests

```sh
.venv/bin/python -m unittest discover -q
```

The tests run against local fixtures only. They cover state, path and issuer checks, replay rejection, callback redirects, atomic private storage, symlink rejection, expiry across restarts, refresh rotation, scope checks, host/origin/CSRF validation, denial and timeouts.
