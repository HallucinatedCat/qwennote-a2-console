# QwenNote A2 host backend

The Python backend and browser UI. It runs directly on your machine. See [DEPLOYMENT.md](DEPLOYMENT.md) for installation and operation, and [docs/credentials.md](../../docs/credentials.md) for credentials.

## Modules

| Files | Responsibility |
| --- | --- |
| `web_backend.py`, `web.html` | Loopback API/UI, readiness, ownership, settings, document and assistant integration. The independent offline HTML template covers everyday views and Dev tools; `--frontend` selects a replacement without backend edits |
| `public_errors.py` | Refusal classes whose literal messages are safe to show in the UI; everything else stays generic |
| `ui_page.py`, `i18n/*.json` | Validates and reloads the selected template, then injects per-response security values and translation catalogs |
| `capabilities.py` | Evidence level of each function: `verified`, or still needing a check on real hardware |
| `cache.py` | Server-side cache of cloud reads in protected storage (24 h, option lists 7 days) |
| `credential_status.py` | Read-only credential health (expiry, renewal, file protection) for Dev tools; never returns values |
| `FRONTEND.md`, `FRONTEND_API.md`, `frontend-contract.json` | Template replacement and design guide, detailed reference for every frontend-callable function, and its machine-readable route contract |
| `lwp_client.py`, `native_auth.py`, `import_session.py` | Native WSS, refresh and protected import |
| `device_protocol.py`, `device_name.py`, `settings_catalog.py`, `generation_settings.py`, `notification_settings.py` | Bounded commands, guarded device rename/restoration, scoped settings, atomic local journals and validated editors |
| `live_transcript.py`, `live_diagnostics.py`, `capture_report.py`, `rtc_report.py` | Partial/final text reduction, bounded private capture, counts-only replay |
| `rtc_protocol.py`, `rtc_session.py`, `rtc_controller.py` | RTC request shapes, shared lifecycle, host ownership and cancellation-safe cleanup |
| `mcs_signaling.py`, `mcs_media.py` | MCS signalling and receive-only Opus measurements; no microphone publication/playback/audio files |
| `rtc_translation.py` | Read-only interpreter catalog; subscription builders are offline-only |
| `audio_memos.py` | Read-only saved memo text; manual assistant drafting without automatic forwarding |
| `builtin_assistant.py` | Built-in general/note Q&A, continued sessions, saved-conversation listing/restoration, current history and explicit existing-task recovery; memory-only credentials and identifiers, no prompt replay |
| `quota_protocol.py` | Read-only account/device membership, bounded quota projections, preserved unknowns/dates and device 4G usage |
| `note_replace.py` | Literal transcript replacement, timed-word reconstruction, bounded native paging, saved-transcript projection and preview/readback helpers |
| `note_tags.py` | Native tag catalog/membership validation, metadata-preserving create/rename/auto-match/delete contracts, revision fingerprints and opaque note-bound handles |
| `note_summary.py` | Default Markdown summary identity/format checks, revision fingerprints, source-matched edit payload and two-stage asynchronous regeneration |
| `note_translate.py` | Native language options and translated title/default-summary/key-point/action-item/transcript contracts without original-note or preference writes |
| `note_export.py` | Original, translated and bilingual application export in TXT, Markdown, DOCX and PDF, plus transcript SRT; bounded download proxy keeps signed transport URLs private |
| `note_media.py` | Application detail/play/download-URL readers, official play-URL fallback, origin-pinned bounded audio download, protected local save and ffprobe validation |
| `note_favorites.py` | Application-path Favorite/Remove favorite request shapes and strict identity/readback parsing; the backend owns guarded journals and reconciliation |
| `test_*.py` | Synthetic contract, privacy, lifecycle and loopback tests |

Native and MCP credentials are separate. One process owns refresh for each session. State stays in 0700 directories with 0600 files, never in browser JavaScript or the repository. The UI requires a same-origin cookie and CSRF token; each page load gets its own CSP script nonce. Polling is every two seconds with an event cursor. Nothing records or changes settings on page load. For UI work without a device, run `python3 tools/ui_preview.py` (synthetic data, no vendor connection).

Recording controls need your confirmation, and the recorder state shown comes from a device check (online state, `recording_fid` and its cloud task) rather than from the last command sent. Out of reach for now: live translation, speaker playback and custom device voice routing.

Passive diagnostics store raw packets privately, with 300-second / 8-MiB / 5,000-row per-capture limits and 16-directory / 64-MiB storage bounds. `capture_report.py` prints aggregate counts. Captures do not start device functions or delete old evidence automatically.

The console never resets, flashes or updates firmware. When testing, use disposable recordings, and keep the cleanup/restore journals after an ambiguous failure. Inherited settings require explicit persistent-override confirmation; an old inherited value is not exact inheritance restoration.
