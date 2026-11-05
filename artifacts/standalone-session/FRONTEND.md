# Browser application

## Scope

`web.html` is the browser client for the current `web_backend.py` contract. Python routes, credentials, journals, recordings, and device operations are outside the frontend.

The deliverable is one HTML file with inline CSS and JavaScript. It has no build step, package dependency, external font, remote asset or framework. The same file is used by the authenticated local service and the offline preview. Browser `localStorage` holds only three per-browser preferences (language, theme, and the Dev "Show protocol keys" switch); the page works without it.

## Replace the template

Copy `web.html` and start the backend with `--frontend /absolute/path/to/page.html`. The selected file is independent of `web_backend.py`; it is read again on every `GET /`, so replacing it and reloading the browser does not require a backend restart. Use a regular UTF-8 file of at most 2 MiB, with each of `__CSRF__`, `__NONCE__`, and `__I18N__` exactly once. Symlinks, missing files, invalid UTF-8, oversized files, and wrong placeholder counts are refused. Keep `i18n/*.json` beside the backend; catalogs are injected into the template rather than duplicated into it.

The backend still owns the UI cookie, CSRF checks, CSP nonce, API validation, credentials, journals, and device operations. A replacement changes presentation and browser behavior only. It cannot authorize vendor calls by itself.

- [Complete frontend API reference](FRONTEND_API.md): transport, response behavior, side effects, errors, and request fields for all 53 POST actions plus status and health reads.
- [Machine-readable contract](frontend-contract.json): the same route inventory and template limits for client generators and compatibility tests.
- [Backend API](../../docs/host-api.md): every route, its guards, the server-side cache and recovery rules.

To revert, remove `--frontend` and reopen the page. For the installed host service, replacing `web.html` in place is the simplest option and needs no restart; keep the original and add `--frontend` to the service command when the replacement lives elsewhere. To exercise a candidate through the preview server (synthetic data, no vendor session), run from the repository root:

```sh
python3 tools/ui_preview.py --frontend /absolute/path/to/page.html --port 18781
```

## Design

A daily-use console for one person and one A2: a light neutral shell (dark mode follows the system or the Display setting), a slim left icon rail, a top bar, list and detail panes, and an assistant panel that slides in from any screen. Blue is reserved for primary actions and selection; recording red is used only for recording state and destructive actions; green and amber mark status. Type is the system UI stack (with CJK fonts) and tabular figures for numbers. Controls use an 8 px radius, cards 12 px, and one stroke icon family drawn as inline SVG symbols.

Motion is limited to press feedback, menu and dialog entry, and the recording pulse; all of it is removed under `prefers-reduced-motion`. Hover styles sit inside `(hover:hover) and (pointer:fine)`.

Under 768 px the rail becomes a bottom tab bar, the notes list and reader become one pane at a time with a back button, and the assistant panel becomes full screen. Inputs are 16 px on touch screens, and safe-area insets are respected.

## Views

- **Home** (`#live`): the recorder (status, timer, and the large recorder controls), the live transcript, recent notes, and a device summary.
- **Notes**: searchable list, then a reader with Summary (rendered Markdown, Edit, Regenerate), Transcript (application or document source), Translate (with translated and bilingual export), Organize (tags, sharing, find and replace) and Details (keywords, to-dos, details, cloud summary). Export and Save original audio are in the Export menu; Ask assistant opens the panel with the note as context.
- **Memos**: voice memos, each with "Add to draft".
- **Assistant**: the full-width assistant, with the conversation list. The same component is the side panel on every other view.
- **Settings**: Recording, Transcription, Summaries and any other editable fields, each group with one Save; then Alerts, Device identity, Quotas and Display. Read-only and internal fields are in Dev.
- **Dev**: the "Show protocol keys" switch, request timings, credentials, the realtime session test, recording task, push capture, evidence levels, all device settings with their keys, events, and the document tool runner.

The recorder controls are one set of buttons: large on Home, compact in the top bar on every other view. Device writes (settings, alerts, device name, recording) need armed controls; if the controls are not armed, Save opens the readiness checklist and then continues with that save.

Functions that do not work yet are not shown as placeholders.

## Runtime contract

The HTML embeds the two JSON catalogs and the current session CSRF value. Mutations use `POST /api/<action>`, JSON bodies, and `X-CSRF-Token`.

`GET /api/status` is polled every two seconds while visible and every ten seconds while hidden, with bounded exponential backoff after failures. Each poll sends the event cursor (`since`) and the setting catalog revision it already holds (`settings_revision`), so the backend leaves out the catalog (about 90% of a full status) until it changes. A 403 ends the browser session and stops polling; reload after restarting the local service.

Each region of the page repaints only when the data it shows has changed (`changed()`), so polling does not rebuild the page or disturb focus. `run()` is the shared action path: it prevents duplicate submissions of the same action, marks the button busy, translates public errors, shows success messages, and then refreshes status (in the background by default, or awaited when the next screen depends on it). The backend queues a second request of the same kind instead of refusing it (see FRONTEND_API.md, Concurrency), so independent reads, such as a note's summary and favorite state, are sent together.

DOM content is built with `textContent` and `createElement` only. Summaries, translated summaries and assistant replies go through a small Markdown renderer (`markdown()`) that builds DOM nodes for headings, lists, rules, bold, italic and inline code; everything else stays plain text. There is no HTML string insertion, string evaluation or remote script.

## Recording, device state and saved data

One confirmation arms the controls until Disarm; the backend saves that, so it survives restarts. `withReadiness()` asks for it when a device write needs it and then continues with the write.

The recorder shows what the A2 is doing, not what the console last sent. `recorderView()` uses `status.device_check` (a device read plus the current recording task, the way the official app derives it) and only shows the command just sent for the few seconds before the next check. The backend checks every 30 seconds while a recording is open, every 5 minutes otherwise, 8 seconds after each command, and before each start; the Check device button runs one at once. A recording started with the A2's button is picked up, and an A2 that goes offline or idle closes the open recording. "Mark as stopped" (`confirm-stopped`) stays as the fallback when the check cannot reach the cloud. No frontend path retries an ambiguous command. The realtime session test is a Dev tool and needs armed controls.

Cloud reads come from the server-side cache when they are younger than a day (option lists a week). Automatic loads accept the saved copy; reload buttons run inside `fresh()`, which adds `refresh: true`. `api()` reads `X-Cached-At` to show "saved copy from ..." on the notes list and the open note. Dev tools shows the cache size and can clear it.

## Recordings, dangerous actions and MCP writes

The Recordings tab lists the A2's cloud-stored recordings by opaque handle and saves original audio through the backend, which streams the checked file. Destructive actions (note and recording delete, voiceprint clear, unbind) appear only while `status.dangerous_enabled` is true and each asks for a typed word (`askTyped`). MCP-only functions (note title, speaker rename, hot words) appear when `status.mcp_write_available` is true; Settings → Note edits chooses whether summary saves and find and replace use the application path or MCP. The recorder's Live transcript switch starts a realtime transcription session (`rtc-start`) instead of a plain start, and Stop ends it (`rtc-stop`).

## Internationalization

`i18n/en.json` and `i18n/zh-CN.json` have identical key sets. The language is chosen in Settings, Display (and remembered per browser). `t(key, vars, count)` handles interpolation and locale plurals; `serverText()` maps exact public backend messages through the `server` catalog. Vendor values (such as language names from the application catalog) are shown as sent.

Changing language rerenders dynamic state without discarding the assistant thread, the open note, or selected tag values.

## Adding a function

Add a function to the UI only when it works: a backend route with its guards (mirror a neighbouring route), a caller in `web.html` through `run('<action>', ...)`, catalog strings in both languages, and the contract and reference entries (`frontend-contract.json`, `FRONTEND_API.md`). Do not add placeholders for functions that do not work yet.

## Verification

```sh
python3 -m unittest discover -s artifacts/standalone-session --pattern "test_frontend_*.py"
python3 -m unittest discover -s artifacts/standalone-session --pattern test_i18n.py
node tools/web-checks/check-web-ui.js
node tools/web-checks/test-quota-format.js
```

`test_frontend_rebuild.py` checks landmarks and shared controls, that every referenced element exists, routes, record-command locking, the single recorder control set, the readiness flow for device writes, the lean status poll, protocol keys, the mobile and offline baseline, and safe DOM construction. `test_frontend_template.py` checks replacement validation, reloading and CSP serving. `test_frontend_contract.py` requires every backend action to have a browser caller, a machine contract entry and a reference section. `test_i18n.py` checks catalog parity, page keys and public error coverage.

Use `tools/ui_preview.py` (`?scenario=demo|empty|recording|attention|worst`) to review every view at phone and desktop widths, in both languages and themes, without touching device data. A browser preview verifies layout and client behavior only; it is not physical device evidence.
