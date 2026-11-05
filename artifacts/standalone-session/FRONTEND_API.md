# Frontend API reference

Contract version 1. This reference covers the independent `web.html` template and every host function it can call. The machine-readable equivalent is `frontend-contract.json`.

## Template and transport

`web.html` contains markup, CSS and browser JavaScript only. It has no backend credentials, vendor signing logic, storage requirement or build step. Copy it as the starting point for a replacement.

Start the backend with `--frontend /absolute/path/to/page.html`. The file is reloaded on every `GET /`, so an atomic file replacement followed by a browser reload needs no backend restart. It must be a regular UTF-8 file of at most 2 MiB. Symlinks and invalid templates are refused.

Each template must contain each placeholder exactly once:

| Placeholder | Backend value |
| --- | --- |
| `__CSRF__` | Process-local value used in `X-CSRF-Token` |
| `__NONCE__` | Fresh per-response CSP script nonce |
| `__I18N__` | Safely encoded JSON object containing all locale catalogs |

Open `GET /` before API calls. Send JSON objects to `POST /api/<action>` with `Content-Type: application/json` and `X-CSRF-Token`. Bodies are limited to 64 KiB. Poll status every two seconds while visible and ten seconds while hidden. A 403 expires the browser session; reopen the page after a backend restart.

The default client helper is:

```javascript
async function api(action, body = {}) {
  const response = await fetch(`/api/${action}`, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": CSRF},
    body: JSON.stringify(body)
  });
  if (!response.ok) throw await response.json();
  return response.json();
}
```

Error JSON has `error`, `code` and `message`. Common HTTP results are 403 for session/origin/CSRF refusal, 404 for an unknown action, 409 for a public refusal or unavailable dependency, and 502 for an unexpected failure. Display the public message and do not retry a mutation automatically.

**Concurrency.** Note features (favorite, summary and regenerate, sharing and share URL, tags, find and replace), export, media download, document reads, transcript and translation reads, quota reads and recording-task reads each have their own queue. A second request of the same kind waits for the earlier one, up to 60 seconds, before it returns `busy`. Different kinds run concurrently, and a device command in progress does not block them. Device commands, setting changes and device rename still return `busy` at once while another device operation runs. Another process holding the MCP session lock also returns `busy` at once.

**Cache.** Cloud reads of notes, summaries, tags, sharing, favorites, quotas and memos are served from a server-side cache for 24 hours, and option lists (translation languages, interpreter options, generation options) for 7 days. A response from the cache carries X-Cached-At (Unix seconds when it was read from the cloud). Send "refresh": true in any POST body to bypass the cache. Writes drop the entries they affect.

## Read endpoints

### `GET /api/status`

- **Query:** Optional ?since=N returns only events with id greater than N. Optional ?settings_revision=R leaves out setting_details and setting_choices when R equals the current settings_revision, which every response carries.
- **Response:** Cached status object: connection, device, settings, generation/notification groups, recording ownership, record_task, transcript, rtc, live_diagnostics, pending flags, credentials, events, event_cursor, assistant state, settings_revision, action_timings (per action: count, failures, last_ms, max_ms, total_ms since start) and server_time.
- **Side effects:** None. It reads host memory only and never triggers a vendor refresh.

### `GET /healthz`

- **Query:** None. No UI cookie is required, but loopback Host and same-site checks apply.
- **Response:** connection, worker_running, restart_safe and blocking[].
- **Side effects:** None. Use before an intentional restart; a safe response does not prove physical device behavior.

## POST actions

Required and optional fields are exact unless the action notes a choice. Do not add arbitrary identifiers or vendor options.

### `account-settings`

Read the account-level settings: smart slicing and voiceprint.

`POST /api/account-settings`

Send an empty JSON object: `{}`.

**Response:** slicing {enabled} and voiceprint {allowed, enabled, enrolled, available_to_account}.

**Side effects:** Two reads (MoziCustomerSettingAppI.queryCustomerSetting, MoziVoicePrintAppI.getVoicePrintConfig), cached for a day.

**Common public error codes:** `unavailable`.

### `assistant`

Send one prompt to the built-in QwenNote assistant or the configured custom provider.

`POST /api/assistant`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `text` | string: nonblank, 1 to 32000 characters | Required |
| `request_id` | string: caller-generated local cancellation handle | Optional |
| `provider` | string: builtin (default) or custom | Optional |
| `task_uuid` | string: listed note UUID, built-in provider only | Optional |

**Response:** JSON with text, provider and route or cancellation/provider-outcome metadata.

**Side effects:** Starts one bounded provider request. Built-in prompts reuse the backend-held conversation session until `assistant-new` or `assistant-select-conversation`. No microphone, device audio, note write or automatic forwarding. Built-in note context requires a listed task_uuid.

**Common public error codes:** `refused`, `busy`, `unavailable`, `not_ready`.

### `assistant-cancel`

Cancel the active host assistant request.

`POST /api/assistant-cancel`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `request_id` | string: active local request handle | Required |

**Response:** JSON cancellation state. The provider may already have processed the prompt.

**Side effects:** Cancels local HTTP work only; it cannot retract provider-side processing.

**Common public error codes:** `refused`.

### `assistant-note-size`

Count the characters of a listed note's saved transcript, shown when the note is attached to a built-in assistant question.

`POST /api/assistant-note-size`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: note UUID from the owner's notes list | Required |

**Response:** JSON with characters, paragraphs and complete (false after 2,000 paragraphs). The assistant reports no context use or limit.

**Side effects:** Cloud reads of the transcript pages, served from the server-side cache while fresh.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `assistant-conversations`

List saved built-in QwenNote conversations for explicit restoration.

`POST /api/assistant-conversations`

Request body must be an empty JSON object.

**Response:** JSON with `conversations[]` containing opaque `handle`, `title` and `date`, plus `provider` and `route`. Vendor session identifiers are never returned.

**Side effects:** Performs one bounded read-only application request for at most 100 deduplicated conversations and replaces the process-local handle map. No prompt replay, note write, device command or upstream deletion.

**Common public error codes:** `refused`, `busy`, `unavailable`, `not_ready`.

### `assistant-select-conversation`

Restore one listed built-in conversation and display its current history.

`POST /api/assistant-select-conversation`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: opaque handle from assistant-conversations | Required |

**Response:** JSON with `restored:true`, `messages[]` containing `query`, `text` and `complete`, plus latest `text`, `provider` and `route`. Session and task identifiers are not returned.

**Side effects:** Resolves the process-local handle, sets the backend session and performs one read-only history request. Replaces stale local recovery state and exposes recovery only when restored history reports an interrupted task. No prompt replay, note write, device command or upstream deletion.

**Common public error codes:** `refused`, `busy`, `unavailable`, `not_ready`.

### `assistant-history`

Read the active built-in QwenNote conversation, including prompts and reply completion state.

`POST /api/assistant-history`

Request body must be an empty JSON object.

**Response:** JSON with `messages[]` containing `query`, `text` and `complete`, plus latest `text`, `provider` and `route`. Session and task identifiers are not returned.

**Side effects:** Uses the backend-held session ID for one read-only application history request. No prompt replay, note write, device command or upstream deletion.

**Common public error codes:** `refused`, `busy`, `unavailable`, `not_ready`.

### `assistant-new`

Start a fresh built-in conversation on the next prompt.

`POST /api/assistant-new`

Request body must be an empty JSON object.

**Response:** JSON with `new_conversation:true` and `upstream_deleted:false`.

**Side effects:** Clears only backend memory for the current session and recovery handle. Existing vendor conversation history is preserved; no upstream request.

**Common public error codes:** `busy`.

### `assistant-recover`

Recover an interrupted built-in reply by polling its existing vendor task result.

`POST /api/assistant-recover`

Request body must be an empty JSON object.

**Response:** JSON with `recovered:true`, `text`, `provider` and `route`, or a public error while recovery remains available.

**Side effects:** Uses the backend-held task ID for bounded application `querySummaryResult` polling. Never resends the prompt or creates a note/device operation.

**Common public error codes:** `refused`, `busy`, `unavailable`, `not_ready`.

### `slicing-save`

Turn smart slicing of long recordings on or off for the account.

`POST /api/slicing-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `enabled` | boolean | Required |

**Response:** The account settings after a fresh read.

**Side effects:** One updateCustomerSetting write (smart_slice / enable_slice), journaled first and never retried. Requires armed controls.

**Common public error codes:** `refused`, `busy`, `not_ready`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `voiceprint-save`

Turn voiceprint speaker recognition on or off.

`POST /api/voiceprint-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `enabled` | boolean | Required |

**Response:** The account settings after a fresh read.

**Side effects:** One toggleVoicePrint write with shouldClearVoicePrint false, journaled first and never retried. Requires armed controls. Enrolling a voiceprint needs the official app and Bluetooth.

**Common public error codes:** `refused`, `busy`, `not_ready`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `voiceprint-clear`

Delete the enrolled voiceprint. Dangerous action.

`POST /api/voiceprint-clear`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `confirm` | string: clear | Required |

**Response:** The account settings after a fresh read.

**Side effects:** One clearVoicePrint write, journaled first and never retried. Requires armed controls and dangerous actions.

**Common public error codes:** `refused`, `busy`, `not_ready`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `audio-memos`

Read recent saved audio memos.

`POST /api/audio-memos`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `limit` | integer: 1 to 100, default 20 | Optional |

**Response:** JSON with items containing text, created_at_ms and duration_ms, plus has_more.

**Side effects:** Read-only application request with a 30-second cache. No media download or device attribution.

**Common public error codes:** `refused`, `unavailable`.

### `ble-pairing-file`

Download the Bluetooth pairing file for tools/a2-ble. Dangerous action.

`POST /api/ble-pairing-file`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `confirm` | string: pair | Required |

**Response:** Attachment, not JSON: a2-pairing.json with sn, device_secret, did and corp_id.

**Side effects:** Reads the A2 serial and device secret from the bound-device record already in memory, and a stable client id kept in .private/ble-client.json. Requires dangerous actions; the values are never shown on the page or logged.

**Common public error codes:** `refused`, `unavailable`.

### `cache-clear`

Delete every saved cloud read, so the next reads go to the cloud.

`POST /api/cache-clear`

Send an empty JSON object: `{}`.

**Response:** cleared true.

**Side effects:** Deletes the cache files in protected storage. Journals, settings and credentials are untouched.

**Common public error codes:** none.

### `capture-start`

Start bounded private diagnostics on the existing native receive loop.

`POST /api/capture-start`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `seconds` | integer: 1 to 300, default 120 | Optional |

**Response:** Aggregate capture status with elapsed seconds, byte/row counters and limits.

**Side effects:** Writes protected diagnostic data only. Does not start recording, arm controls or call the vendor.

**Common public error codes:** `refused`, `busy`.

### `capture-stop`

Finish the active diagnostic capture.

`POST /api/capture-stop`

Send an empty JSON object: `{}`.

**Response:** Final aggregate capture status; raw capture remains protected.

**Side effects:** Stops diagnostics only. Never stops a recording.

**Common public error codes:** `refused`.

### `confirm-stopped`

Manually close the open recording after seeing the A2 stop, when a device check cannot reach the cloud.

`POST /api/confirm-stopped`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `physically_stopped` | boolean: must be true | Required |

**Response:** Updated recording ownership state.

**Side effects:** Clears the owned-test journal only after explicit confirmation. This is not a Stop command.

**Common public error codes:** `refused`, `busy`.

### `dangerous`

Turn the dangerous-actions switch on or off.

`POST /api/dangerous`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `enabled` | boolean | Required |
| `confirm` | boolean: must be true to enable | Optional |

**Response:** dangerous_enabled.

**Side effects:** Saves the switch in .private/dangerous.json; no cloud call.

**Common public error codes:** `refused`.

### `device-check`

Check the A2 now: online state, battery and, when it reports a current recording, that recording task.

`POST /api/device-check`

Send an empty JSON object: `{}`.

**Response:** device_check: state (recording, paused, idle, offline, unknown), online, checked_at, battery_percent, in_progress.

**Side effects:** One bound-device read and at most one recording-task read. Opens the recording when the A2 is recording (including one started with its button) and closes it when the A2 is idle or offline. Runs automatically every 30 s while a recording is open and every 5 minutes otherwise.

**Common public error codes:** `busy`, `unavailable`.

### `device-unbind`

Unbind the A2 from the account on the server. Dangerous action.

`POST /api/device-unbind`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `confirm` | string: unbind | Required |

**Response:** unbound true.

**Side effects:** One DeviceI.unbindV3 write, journaled first and never retried. The console then loses the device. Binding again needs the official app and Bluetooth (activation reads a cipher from the A2). Requires dangerous actions to be enabled.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `device-info`

Product information and the phones linked to the A2.

`POST /api/device-info`

Send an empty JSON object: `{}`.

**Response:** product (productName, deviceType, connectType, bindType, oneDevice) and phones[] (name, state: allowed, blocked or connected).

**Side effects:** Two reads (getProductConfig, queryDeviceConnections). The connection request and update calls take the A2 over from the owner's phone and are not offered.

**Common public error codes:** `unavailable`.

### `device-identity`

Read the cached public identity projection for the selected bound device.

`POST /api/device-identity`

Send an empty JSON object: `{}`.

**Response:** JSON with name, sn, firmware_version, read_at, stale, revision, pending and restore availability.

**Side effects:** No native request. Returns owner-only cached name/SN and opaque revision; firmware is null when unavailable.

**Common public error codes:** `refused`, `unavailable`.

### `device-name`

Refresh device identity and reconcile any pending rename journal.

`POST /api/device-name`

Send an empty JSON object: `{}`.

**Response:** Refreshed device-identity object and readback state.

**Side effects:** Read-only native reconciliation. Never sends or replays a rename.

**Common public error codes:** `refused`, `unavailable`, `outcome_unknown`.

### `device-name-restore`

Restore the journaled original device name.

`POST /api/device-name-restore`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `expected_revision` | string: current opaque device-name revision | Required |

**Response:** Device identity with verified readback or pending uncertainty.

**Side effects:** One explicit restore write while the requested name is still current. Never automatic.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `device-name-save`

Rename the selected bound device through the application path.

`POST /api/device-name-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `name` | string: trimmed device name, at most 32 weighted units | Required |
| `expected_revision` | string: current opaque device-name revision | Required |

**Response:** Device identity with verified readback or pending uncertainty.

**Side effects:** Single-shot rename with a protected original-name journal and strict readback. Maximum 32 weighted units.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `disarm`

Disarm recording and RTC controls.

`POST /api/disarm`

Send an empty JSON object: `{}`.

**Response:** Updated readiness state.

**Side effects:** Saves the disarmed state. Does not stop an active recording; Stop stays available while a recording is open.

**Common public error codes:** `refused`.

### `documents`

Invoke one of the 14 read-only QwenNote MCP tools through the host adapter.

`POST /api/documents`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `tool` | string: one of the documented read-only MCP tool names | Required |
| `arguments` | object: schema for the selected tool | Required |

**Response:** JSON MCP result envelope. The default template decodes its structured content for notes, summaries, transcripts and extras.

**Side effects:** Read-only MCP call with a 60-second total deadline and no automatic retry. Generic write tools are not exposed.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `generation`

Save summary-generation mode, minimum duration and maximum duration as one batch.

`POST /api/generation`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `values` | object: complete auto_generate_minutes, auto_generate_minutes_duration and auto_generate_minutes_duration_limit values | Required |
| `persistent_override` | boolean: true only, required for a new persistent override | Optional |

**Response:** Updated generation-group descriptor and restoration state.

**Side effects:** Device-setting write requiring readiness and a before-write journal. Persistent inherited overrides require confirmation.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `mcp-login-start`

Start owner consent for MCP read or write access, returning to this console's own port.

`POST /api/mcp-login-start`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `access` | string: read or write | Required |
| `return_to` | string: console (default; the vendor returns to /mcp/callback on this port) or registered (the address the existing client was registered with; then paste the final address) | Optional |

**Response:** url to open in a new tab, and return_to.

**Side effects:** Reads the public authorization-server metadata and keeps a one-time PKCE login in backend memory for 15 minutes. Reuses the existing client registration; registers nothing.

**Common public error codes:** `refused`, `unavailable`.

### `mcp-login-finish`

Finish an MCP login with the address the browser ended on, when it could not return to the console by itself.

`POST /api/mcp-login-finish`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `url` | string: the full address from the browser bar after approving | Required |

**Response:** granted true and access.

**Side effects:** Exchanges the one-time code (PKCE) at the token endpoint and stores the tokens in the reader's format: the read store for read access, .private/write for write access. GET /mcp/callback does the same when the vendor returns to the console.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `memo-update`

Edit a voice memo's text.

`POST /api/memo-update`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: memo handle from audio-memos | Required |
| `text` | string: at most 4000 characters | Required |

**Response:** saved true.

**Side effects:** One DingerAiAssistantI.updateMission (content), as the app edits memos; journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `memo-delete`

Delete a voice memo. Dangerous action.

`POST /api/memo-delete`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: memo handle | Required |

**Response:** deleted true.

**Side effects:** One deleteMission; requires dangerous actions; journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-delete`

Delete a note permanently. Dangerous action; notes have no recycle bin.

`POST /api/note-delete`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `confirm` | string: delete | Required |

**Response:** deleted true.

**Side effects:** One PortalMinutesI.deleteTaskV2 write after a fresh identity read, journaled first and never retried. Requires dangerous actions to be enabled.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-export`

Download an original, translated or bilingual note transcript or default summary in an application-supported format.

`POST /api/note-export`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `kind` | string: transcript or summary | Required |
| `target_language` | string: language key returned by note-translate-options; omit for the original | Optional |
| `bilingual` | boolean: include source text with the translation; requires target_language, default false | Optional |
| `file_format` | string: txt, markdown, docx or pdf; transcript also allows srt; default txt | Optional |

**Response:** Attachment, not JSON: text/plain, text/markdown, DOCX or PDF. Request with download handling and create a Blob.

**Side effects:** Target/format validation, application export and bounded host download with text decoding or DOCX/PDF signature checks. Vendor Markdown transport may be `text/plain` or `text/markdown`; binary responses may omit `Content-Disposition`, but the host response always uses `attachment`. No note edit or saved-language change; 8 MiB response limit and no automatic retry.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `note-favorite`

Favorite or unfavorite a listed note.

`POST /api/note-favorite`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `collected` | boolean: desired favorite state | Required |
| `expected_collected` | boolean: state shown before the click | Required |

**Response:** JSON with collected, cloud acknowledgement, readback and verification state.

**Side effects:** One application-path collectMinutes write after a fresh expected-state check and protected journal.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-favorite-status`

Read favorite state and reconcile a matching pending journal.

`POST /api/note-favorite-status`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with collected, verified true and changed false.

**Side effects:** Native read only. Cannot clear a journal belonging to another note.

**Common public error codes:** `refused`, `unavailable`.

### `note-markers`

List a note's markers, or add a marker at a time.

`POST /api/note-markers`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `add_ms` | integer: milliseconds from the start; adds a marker first | Optional |

**Response:** markers[] with kind (marker, image, text), time_ms and text.

**Side effects:** MinutesNoteI.queryNoteList; with add_ms, one createNote (type 0) first, journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-mcp-write`

Change a note or the personal hot words through an MCP write tool, as an alternative to the native application path.

`POST /api/note-mcp-write`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `tool` | string: update_minutes_title, update_minutes_summary, replace_minutes_text, replace_speaker, add_personal_hot_word or delete_personal_hotword | Required |
| `arguments` | object: taskUuid (listed note) plus title, summaryText, originalText and replacedText, or speakerNick and targetNickName; hotWordList (1 to 50 words) for the hot-word tools | Required |

**Response:** tool, applied true and the MCP result envelope.

**Side effects:** One MCP tools/call on the separate owner-consented write grant (artifacts/mcp-reader/.private/write, scope qwennote:read qwennote:write). The intent is journaled first; an unknown outcome is never retried. Drops the note's saved cloud reads. Available only when status.mcp_write_available is true and the server advertises the tool to that grant.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-media-download`

Save a cloud-stored original audio recording into protected host storage.

`POST /api/note-media-download`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with saved, random local filename, byte count, SHA-256, MIME, source, expected duration and ffprobe audio metadata. Signed URLs and absolute host paths are excluded.

**Side effects:** Reads `minutesDetailV2`, `queryPlayInfo` and `queryDownloadUrl`, then downloads once to `.private/media` with directory mode `0700` and file mode `0600`. A rejected or empty download-URL reply falls back to the official mobile/PC play URL; detail/play failures and missing URLs refuse the save. Requires audio `mediaType="audio"`, application non-incognito semantics (`isIncognitoMode` other than `true`), current cloud storage `0` and current incognito mode `0`. The host enforces an HTTPS origin allowlist, public-DNS pinning, disabled redirects, a 512 MiB cap, a 300-second transfer bound, ffprobe audio validation and partial-file cleanup. The protected `note-media.json` journal excludes URLs and paths. Note content and settings stay unchanged; retries require another explicit click.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `note-regenerate`

Regenerate the default Markdown summary through the two-stage application flow.

`POST /api/note-regenerate`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `expected_revision` | string: current opaque summary revision | Required |

**Response:** JSON with accepted/pending state and, when complete, summary text and revision.

**Side effects:** Starts manualRegenerateSummary then generateMinutesSummary. Completion is asynchronous; refresh reconciles without replay.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-replace-preview`

Preview literal, case-sensitive transcript replacement.

`POST /api/note-replace-preview`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `find` | string: nonblank literal text | Required |
| `replace` | string: literal replacement, may be empty | Required |

**Response:** JSON with matches, changed paragraph count, before/after rows and an opaque revision.

**Side effects:** Read-only full native transcript snapshot. Invalidated when find or replace input changes.

**Common public error codes:** `refused`, `unavailable`.

### `note-replace-save`

Apply a fresh transcript replacement preview.

`POST /api/note-replace-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `find` | string: previewed literal text | Required |
| `replace` | string: previewed replacement | Required |
| `expected_revision` | string: opaque preview revision | Required |

**Response:** JSON with verified readback or pending uncertainty.

**Side effects:** One bounded updateParagraph batch, at most 20 changed paragraphs and 1000 matches. Marked paragraphs are refused.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-share`

Read private/public note-sharing state and preserved options.

`POST /api/note-share`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with access, enabled options, sharing flag, revision and pending state. Passwords, role codes and subresource IDs are not exposed.

**Side effects:** Native application read only; also reconciles exact original/requested journal states without a write.

**Common public error codes:** `refused`, `unavailable`.

### `note-share-save`

Set note scope to private or public.

`POST /api/note-share-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `access` | string: private or public | Required |
| `expected_revision` | string: current opaque sharing revision | Required |
| `password` | string: 4 to 16 letters and digits with public access; empty string removes the password | Optional |

**Response:** Updated sharing state with strict readback or pending uncertainty.

**Side effects:** Explicit scope write with protected journal. Public mutation is prohibited by the current project constraint.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-share-url`

Read the application-provided HTTPS share URL for explicit clipboard copying in the current private or public scope.

`POST /api/note-share-url`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with `url`, `access` and `sharing_enabled`. The URL is sensitive and returned only to this explicit request.

**Side effects:** Two native application reads. No scope write, URL opening, external transmission or journal mutation.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `note-speaker-rename`

Rename a speaker across a note through the application path.

`POST /api/note-speaker-rename`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `from` | string: current speaker name, as shown | Required |
| `to` | string: new speaker name, at most 64 characters | Required |

**Response:** renamed: number of paragraphs.

**Side effects:** Reads the whole transcript, picks the paragraphs of that speaker (as the app's 'all' choice does) and sends one batchUpdateParagraphNickName; journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-summary`

Read the complete default Markdown summary.

`POST /api/note-summary`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with text, revision, verified and pending.

**Side effects:** Native read and read-only journal reconciliation for a recently listed own note.

**Common public error codes:** `refused`, `unavailable`.

### `note-summary-save`

Replace the default Markdown summary.

`POST /api/note-summary-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `text` | string: complete default Markdown summary | Required |
| `expected_revision` | string: current opaque summary revision | Required |

**Response:** Updated summary text/revision with verified readback or pending uncertainty.

**Side effects:** One editFullTextSummary write with fresh revision check and original journal. Maximum 48 KiB.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-tag-create`

Create an account tag with automatic matching off.

`POST /api/note-tag-create`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `name` | string: trimmed, at most 15 UTF-16 units | Required |
| `expected_revision` | string: current opaque tag revision | Required |

**Response:** Updated tag options and revision. The new tag is not assigned automatically.

**Side effects:** One addTag write with fresh catalog, protected journal, acknowledged-ID readback and no automatic retry.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-tag-delete`

Delete a tag definition that is not assigned to the open note.

`POST /api/note-tag-delete`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `handle` | string: opaque tag option handle | Required |
| `expected_revision` | string: current opaque tag revision | Required |

**Response:** Updated tag options and revision.

**Side effects:** One deleteTag write after fresh membership checks. Existing assignments must be preserved.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-tag-rename`

Rename a tag and optionally change its automatic-matching flag.

`POST /api/note-tag-rename`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `handle` | string: opaque tag option handle | Required |
| `name` | string: trimmed, at most 15 UTF-16 units | Required |
| `expected_revision` | string: current opaque tag revision | Required |
| `auto_match` | boolean: desired automatic-matching flag | Optional |

**Response:** Updated tag options and revision.

**Side effects:** One updateTag write. Hidden description is preserved; omitted auto_match preserves the current flag.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-tags`

Read account tag options and memberships for the open note.

`POST /api/note-tags`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with options, selected_handles, revision, verified and pending.

**Side effects:** Native catalog and membership reads. Opaque handles keep raw tag IDs in the backend.

**Common public error codes:** `refused`, `unavailable`.

### `note-tags-save`

Replace the complete set of tags assigned to the open note.

`POST /api/note-tags-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `selected_handles` | array of strings: complete desired opaque-handle set | Required |
| `expected_revision` | string: current opaque tag revision | Required |

**Response:** Updated selected_handles, options and revision with readback state.

**Side effects:** One complete membership replacement with original journal. Does not edit tag definitions.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `note-transcript`

Read one native saved-transcript page.

`POST /api/note-transcript`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `next_token` | string or null: null starts a fresh page; otherwise use the issued token | Required |

**Response:** JSON with rows containing text/speaker/timing, next_token and note identity.

**Side effects:** Read-only application paging, 200 rows per page. Continuation tokens expire after 15 minutes and reject stale notes.

**Common public error codes:** `refused`, `unavailable`.

### `note-translate`

Read a translated title, default summary, brief sections and one transcript page.

`POST /api/note-translate`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |
| `target_language` | string: target key from note-translate-options; use for a new view | Optional |
| `next_token` | string: issued continuation token; use instead of target_language | Optional |

**Response:** JSON with title, summary, brief_items, rows and next_token.

**Side effects:** Separate translated view only. Never changes the original note or saved language preference.

**Common public error codes:** `refused`, `unavailable`.

### `note-translate-options`

List supported translation targets and detect the note source language.

`POST /api/note-translate-options`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `task_uuid` | string: listed note UUID | Required |

**Response:** JSON with source_language and targets containing key/label.

**Side effects:** Native language-catalog read only. No preference write.

**Common public error codes:** `refused`, `unavailable`.

### `notification`

Save task-completion alert method and optionally its organization.

`POST /api/notification`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `method` | string: 0 for voice or 1 for vibration | Required |
| `organization_handle` | string: opaque option handle; omit to preserve current organization | Optional |
| `persistent_override` | boolean: true only when confirming an inherited override | Optional |

**Response:** Updated notification-group descriptor and restoration state.

**Side effects:** Device-setting write requiring readiness. Organization handles are revalidated; unchanged values cause no write.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `notification-options`

Load alert and organization option descriptors.

`POST /api/notification-options`

Send an empty JSON object: `{}`.

**Response:** Notification-group object with method choices, organization options and opaque handles.

**Side effects:** Read-only listActiveOrganizations request. No physical readiness required.

**Common public error codes:** `refused`, `unavailable`.

### `quotas`

Read membership and remaining account/device quotas, allowance detail and 4G usage.

`POST /api/quotas`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `organization_handle` | string: current opaque organization option; omit for the default personal scope | Optional |

**Response:** JSON with queried_at, selected opaque organization handle/label and account, device, detail, traffic sections. Each section has available and data.

**Side effects:** Four read-only native application requests for the selected scope, with per-section availability, caching and no device mutation. Raw organization IDs stay backend-only.

**Common public error codes:** `refused`, `unavailable`.

### `ready`

Arm the recording and RTC controls until Disarm.

`POST /api/ready`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `confirm` | boolean: must be true | Required |

**Response:** armed true, ready_until (far future) and server_time.

**Side effects:** Saves the armed state in protected storage, so it survives restarts. Sends no device command. The earlier body with physical_ready, device_idle and disposable_only all true is still accepted.

**Common public error codes:** `refused`, `unavailable`.

### `reconnect`

Restart a stopped native connection worker.

`POST /api/reconnect`

Send an empty JSON object: `{}`.

**Response:** Connection/worker state.

**Side effects:** Creates no second refresh owner. Refused while the existing worker runs.

**Common public error codes:** `refused`, `not_ready`.

### `record`

Send a Record, Pause, Resume or Stop command to the A2.

`POST /api/record`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `action` | string: start, pause, resume or stop | Required |

**Response:** JSON command acknowledgement and physical_result unverified; the device check about 8 s later shows the result.

**Side effects:** Start first runs a device check and is refused when the A2 is offline or already recording. Pause, resume and stop need an open recording (started here or found by a device check). Ambiguous commands are never retried.

**Common public error codes:** `refused`, `busy`, `not_ready`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `record-task`

Refresh the last observed account/device-matched cloud recording task.

`POST /api/record-task`

Send an empty JSON object: `{}`.

**Response:** Public record_task summary with cloud status, timestamps, real_time_asr and physical_state_verified false.

**Side effects:** Explicit native read. Does not accept IDs, change ownership or mutate the device.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `recordings`

List the A2's recordings stored in the cloud, including ones that are not notes yet.

`POST /api/recordings`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `next_token` | string: issued continuation token for the next page | Optional |

**Response:** recordings[] with handle, name, duration_ms, bytes, created_at_ms, note_state (not-transcribed, transcribing, transcribed, failed), incognito, in_recycle_bin, playable_from_cloud; has_more and next_token.

**Side effects:** One DeviceFileI.queryFileList read. File ids stay in the backend; the page gets opaque handles.

**Common public error codes:** `refused`, `busy`, `unavailable`.

### `recording-audio`

Save one recording's original audio in protected storage and download it.

`POST /api/recording-audio`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: handle from recordings | Required |
| `format` | string: wav only (default; the A2 uploads WAV) | Optional |

**Response:** Attachment, not JSON: the audio file.

**Side effects:** DeviceFileI.queryFileDownloadInfo, then a bounded, origin-checked download with the cloud-supplied headers into .private/media (0600), checked with ffprobe. Works for incognito recordings only until transcription finishes, when the cloud deletes their audio.

**Common public error codes:** `refused`, `busy`, `unavailable`, `cloud_rejected`.

### `recording-audio-latest`

Save the WAV of the recording the A2 last reported, for Recorder → Download original audio after Stop.

`POST /api/recording-audio-latest`

Send an empty JSON object: `{}`.

**Response:** Attachment, not JSON: the WAV file; or JSON {ready: false} while the cloud does not list or have the file yet.

**Side effects:** One DeviceFileI.queryFileList read and, once the recording with the fid from the last device check is listed, the same queryFileDownloadInfo and bounded download as recording-audio. A read only, so the page repeats it.

**Common public error codes:** `refused`, `busy`, `unavailable`, `cloud_rejected`.

### `recording-make-note`

Ask the cloud to turn a recording into a note.

`POST /api/recording-make-note`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: handle from recordings | Required |

**Response:** applied true.

**Side effects:** One DeviceTaskI.createGenerateMinutesTask write, journaled first and never retried. The note appears in the list when the cloud finishes.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-recover`

Restore a recording from the recycle bin.

`POST /api/recording-recover`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: handle from recordings | Required |

**Response:** applied true.

**Side effects:** One DeviceFileI.recoverFiles write, journaled first and never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-delete`

Move a recording to the recycle bin, or delete it permanently. Dangerous action.

`POST /api/recording-delete`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: handle from recordings | Required |
| `permanent` | boolean: true deletes permanently; default false moves to the recycle bin | Optional |
| `confirm` | string: delete, required when permanent is true | Optional |

**Response:** applied true.

**Side effects:** One DeviceFileI.deleteFiles write (state 3 recycle bin, state 1 permanent), journaled first and never retried. Requires dangerous actions to be enabled.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-markers`

List the markers and text notes of a recording.

`POST /api/recording-markers`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: handle from recordings | Required |

**Response:** markers[] with handle, kind, time_ms and text.

**Side effects:** One DeviceNoteI.queryNoteList read by the recording's fid.

**Common public error codes:** `refused`, `unavailable`.

### `recording-marker-save`

Add a marker or text note to a recording, or edit a text note.

`POST /api/recording-marker-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: recording handle (to add) | Optional |
| `time_ms` | integer: milliseconds from the start (to add) | Optional |
| `text` | string: note text, at most 2000 characters; without it a plain marker is added | Optional |
| `marker` | string: marker handle (to edit its text) | Optional |

**Response:** saved true.

**Side effects:** One DeviceNoteI.createNote (noteType 0 or 2) or updateNote; journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-marker-delete`

Delete a recording marker or text note.

`POST /api/recording-marker-delete`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `marker` | string: marker handle | Required |

**Response:** deleted true.

**Side effects:** One DeviceNoteI.deleteNote; journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-groups`

List the recording groups with file counts.

`POST /api/recording-groups`

Send an empty JSON object: `{}`.

**Response:** groups[] with handle, name and files; counts (total, ungrouped, recycle_bin).

**Side effects:** One DeviceFileI.queryFileGroupList read.

**Common public error codes:** `unavailable`.

### `recording-group-save`

Create a recording group, or rename one.

`POST /api/recording-group-save`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `name` | string: at most 60 characters | Required |
| `group` | string: group handle to rename | Optional |

**Response:** The groups after the change.

**Side effects:** One createFileGroup or updateFileGroup (a full replace with the other fields unchanged); journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-group-delete`

Delete a recording group. Dangerous action.

`POST /api/recording-group-delete`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `group` | string: group handle | Required |
| `confirm` | string: delete | Required |

**Response:** The groups after the change.

**Side effects:** One deleteFileGroups; what happens to the group's recordings is not shown by the app. Requires dangerous actions.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `recording-move`

Move a recording into a group.

`POST /api/recording-move`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `handle` | string: recording handle | Required |
| `group` | string: group handle from a recording or the group list | Required |

**Response:** moved true.

**Side effects:** One batchUpdateFileToGroup; journaled, never retried.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `refresh`

Refresh native discovery, reported settings and generation configuration.

`POST /api/refresh`

Send an empty JSON object: `{}`.

**Response:** Refreshed public status object.

**Side effects:** Read-only native refresh. Does not arm controls or change settings.

**Common public error codes:** `refused`, `unavailable`.

### `restore`

Restore an existing individual device-setting override from its journal.

`POST /api/restore`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `key` | string: supported reported setting key | Required |

**Response:** Updated setting descriptor and restoration state.

**Side effects:** One restoration write. Cannot claim to restore unsupported inheritance.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `restore-generation`

Restore the complete original summary-generation group.

`POST /api/restore-generation`

Send an empty JSON object: `{}`.

**Response:** Updated generation-group descriptor.

**Side effects:** Restores only an exactly restorable device-scope journal; no partial inheritance claim.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `restore-notification`

Restore the original notification method and organization.

`POST /api/restore-notification`

Send an empty JSON object: `{}`.

**Response:** Updated notification-group descriptor.

**Side effects:** Restores only an exactly restorable journal; the old organization must remain available.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `notes-browse`

Search all notes, list favorites or a tag, or list the tags to filter by.

`POST /api/notes-browse`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `mode` | string: search, favorites, tag or tags | Required |
| `query` | string: search text (search) | Optional |
| `tag` | string: tag handle from mode tags (tag) | Optional |

**Response:** items[] in the notes-list shape (uuid, title, gmtCreate, durationMillis, collected), or tags[] with handle and name.

**Side effects:** One read (searchA1Minutes, queryCollectedMinutes, fetchMinutesListByTag or getHomeTabList). Listed notes become usable for note actions.

**Common public error codes:** `refused`, `unavailable`.

### `rtc-options`

Read realtime language and voice options for a scene.

`POST /api/rtc-options`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `scene` | string: a1_transcribe (default) or a1_face2face | Optional |

**Response:** JSON with languages and nested voices, each using labels and opaque handles.

**Side effects:** Read-only native configuration with a five-minute cache. No conference or subscription is created.

**Common public error codes:** `refused`, `unavailable`.

### `rtc-start`

Start a realtime transcription session: the recorder Live transcript mode (until Stop, up to 4 hours) or a short Dev test.

`POST /api/rtc-start`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `seconds` | integer: 1 to 14400, default 20; capped just before the session renewal | Optional |
| `receive_audio` | boolean: default false | Optional |

**Response:** Accepted RTC state; later status carries completion and aggregate receive counters.

**Side effects:** Creates an experimental recording/conference after ownership checks. Optional audio is receive-only, not played or saved.

**Common public error codes:** `refused`, `busy`, `not_ready`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

### `rtc-stop`

Stop the owned RTC task and complete conference cleanup.

`POST /api/rtc-stop`

Send an empty JSON object: `{}`.

**Response:** RTC stopping/completion state.

**Side effects:** Cancels the owned task and performs stop/close/readback even after readiness expiry.

**Common public error codes:** `refused`, `busy`.

### `setting`

Change one supported individual device setting.

`POST /api/setting`

| Field | Type and constraint | Presence |
| --- | --- | --- |
| `key` | string: supported reported setting key | Required |
| `value` | string: one of the current setting choices | Required |
| `persistent_override` | boolean: true only when confirming a new persistent override | Optional |

**Response:** Updated setting descriptor and restoration state.

**Side effects:** Requires readiness, idle test state, scope/choice validation and a before-write journal.

**Common public error codes:** `refused`, `busy`, `unavailable`, `outcome_unknown`, `cloud_rejected`.

## Frontend safety rules

- Build DOM with `textContent`, `createElement` and safe attributes. Do not inject note, assistant or diagnostic text as HTML.
- Keep one action of each name in flight in the browser. Disable its initiating control and expose pending state.
- Refresh revision-bearing data before mutation. A failed or uncertain write keeps its journal; refresh reconciles instead of replaying.
- `note-export` is the only non-JSON success response. Use the response Blob and revoke its object URL after download.
- The tool names and argument schemas accepted through `documents` come from the live MCP tool inventory (see `../mcp-reader/README.md`). MCP remains read-only.
- Backend capabilities and readiness remain authoritative. A hidden or disabled control is not a substitute for server validation.
