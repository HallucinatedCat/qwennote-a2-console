# Capabilities

What the console can do with a QwenNote A2 and its account, and how well each part is proven.

| Status | Meaning |
| --- | --- |
| ✅ **Working** | Built and checked end to end against a real A2 and account |
| 🔎 **Needs a check** | Built and covered by tests; still waiting for one confirmation on real hardware or real notes |
| ⛔ **Not supported** | Not in the console (reason given) |

The same evidence levels are kept in code in [`capabilities.py`](../artifacts/standalone-session/capabilities.py). Every route is documented in [host-api.md](host-api.md). The [live demo](https://qwennote-a2-console-demo.pages.dev/) shows each screen with invented data.

## Recorder

| Function | Status | Notes |
| --- | --- | --- |
| Start, pause, resume, stop | ✅ | Each command is followed by a device check |
| Download the original audio after Stop | ✅ | Recorder switch: after Stop the page fetches the recording's WAV as soon as the cloud has it, retrying for up to 15 minutes |
| Real recorder state | ✅ | Read from the device and its cloud task, not from the last command; picks up recordings started with the A2's button and a switch-off |
| Live transcript while recording | ✅ | Uses a realtime transcription session, the same route the app uses for a 4G A2 |
| Realtime session | ✅ | Join, keep and leave, receive audio (counted, never saved or played) |
| Passive diagnostics | ✅ | Bounded private capture of device pushes; reports counts only |

## Device and account

| Function | Status | Notes |
| --- | --- | --- |
| Device identity | ✅ | Name, serial, firmware, battery |
| Rename and restore name | ✅ | Restore journal kept privately |
| Recording, transcription, summary and alert settings | ✅ | Read, change and restore; changes are allowed while recording |
| Voice button mode | 🔎 | Written as a cloud setting the way the app writes it |
| Product details and linked phones | ✅ | Read-only; taking the A2 over from a phone is not offered |
| Smart slicing, voiceprint on/off | 🔎 | |
| Voiceprint clear | ✅ | Dangerous action: hidden behind a switch and a typed word |
| Membership, quotas, 4G data usage | ✅ | Fields the vendor does not report are shown as unknown |
| Native session renewal | ✅ | Proactive and on natural expiry |
| Credential health | ✅ | Dev tools; never shows a value |

## Notes

| Function | Status | Notes |
| --- | --- | --- |
| List and browse | ✅ | Via MCP and the application path |
| Search, Favorites and tag filters | 🔎 | |
| Saved transcript with speakers and times | ✅ | Including the end of long notes |
| Summary: read, edit (default Markdown), regenerate | ✅ | Other templates and rich summaries are out of scope |
| Find and replace in the transcript | ✅ | Preview first; original kept in a private journal |
| Tags: create, rename, auto-match, delete, assign | ✅ | |
| Favorite and remove | ✅ | |
| Translate (title, summary, key points, action items, transcript) | ✅ | Never changes the original or the saved language |
| Export: TXT, Markdown, DOCX, PDF, SRT; original, translated, bilingual | ✅ | |
| Sharing: private/public, password, link | ✅ | |
| Title, summary, find and replace, speaker names, hot words (MCP write grant) | ✅ | Needs the separate `qwennote:write` consent |
| Rename a speaker (application path) | 🔎 | |
| Delete a note | ✅ | Dangerous action: hidden behind a switch and a typed word |
| Keywords, to-dos, details | ✅ | MCP reads |

## Recordings and memos

| Function | Status | Notes |
| --- | --- | --- |
| Save a recording's original audio | ✅ | WAV only (the A2 uploads WAV). Save an incognito recording before transcription finishes, or turn automatic transcription off |
| Cloud recordings: list, make a note | 🔎 | |
| Delete a recording to the recycle bin, restore it | ✅ | Dangerous action |
| Groups, markers, move | 🔎 | |
| Voice memos: read, edit, delete, add to an assistant draft | 🔎 | |

## Assistant

| Function | Status | Notes |
| --- | --- | --- |
| Built-in assistant: general and note questions, continued conversations, history, recovery | ✅ | |
| Your own OpenAI-compatible provider | ✅ | Only text you submit is sent |

## Documents (official MCP)

| Function | Status | Notes |
| --- | --- | --- |
| OAuth login, refresh, all 14 read tools | ✅ | See [mcp.md](mcp.md) |
| 6 write tools on a separate grant | ✅ | |

## Not supported

| Function | Why |
| --- | --- |
| Firmware updates, factory reset | Deliberately excluded |
| Activating or binding a new device | Activation reads a secret from the A2 over Bluetooth first; the console only uses an existing binding |
| Unbinding | Offered only as a dangerous action; never automatic |
| Live translation, speaker playback on the A2 | The realtime session receives audio but nothing is sent back to the device |
| Voiceprint enrollment | Only status, on/off and clear are available |
| Scheduled recording, reminders, volume, audio source | These are Bluetooth-only device commands; the 4G cloud relay rejects them |
| Custom voice-assistant endpoint for the voice button | The device's voice input and return path is not available to the host |
| Direct Bluetooth or USB control | An experimental Windows Bluetooth client exists in [`tools/a2-ble`](../tools/a2-ble/README.md) and is paused |
| Working without the vendor cloud | Everything goes through your account's cloud session |
