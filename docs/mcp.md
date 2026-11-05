# Official MCP document service

QwenNote runs an official [Model Context Protocol](https://modelcontextprotocol.io/) server for notes, transcripts, summaries and memos. This project uses it in two places: the command-line [MCP reader](../artifacts/mcp-reader/README.md) and the console's document features (log in from **Settings**). It is separate from the native session in [credentials.md](credentials.md); the two sets of credentials are never mixed.

## Endpoints

QwenNote's documentation site has no MCP page. The values below come from the server's own public discovery metadata, which any MCP client reads before logging in:

- [`/.well-known/oauth-protected-resource/mcp`](https://minutes.qwennote.cn/.well-known/oauth-protected-resource/mcp) (RFC 9728)
- [`/.well-known/oauth-authorization-server`](https://minutes.qwennote.cn/.well-known/oauth-authorization-server) (RFC 8414)

| Item | Value |
| --- | --- |
| MCP server (resource) | `https://minutes.qwennote.cn/mcp` |
| Transport | Streamable HTTP; bearer token in the `Authorization` header |
| Authorization server (issuer) | `https://minutes.qwennote.cn` |
| Authorization endpoint | `https://minutes.qwennote.cn/authorize` |
| Token endpoint | `https://minutes.qwennote.cn/token` |
| Dynamic client registration | `https://minutes.qwennote.cn/register` |
| Token revocation | `https://minutes.qwennote.cn/revoke` |
| Scopes | `qwennote:read`, `qwennote:write` |
| Grant types | `authorization_code`, `refresh_token` |
| PKCE | `S256` (required) |
| Client authentication | `none`: public client with PKCE |

Observed while testing; not part of the published metadata and may change: server name `QwenNote` 1.0.0, MCP protocol version `2025-11-25`, access tokens valid for 3600 seconds and renewed with the refresh token.

Sign-in happens on the vendor's own page with your phone number and an SMS code. Neither the reader nor the console ever asks for them.

## Read tools (`qwennote:read`)

These 14 tools are what the server lists for the read scope. The reader calls a tool only if it is on its allowlist **and** in the live tool list, and it takes argument schemas from the live list, which is authoritative.

| Family | Tools |
| --- | --- |
| Browse and tags | `list_my_minutes`, `list_minutes_tags`, `list_minutes_by_tag` |
| Metadata | `get_minutes_basic_info`, `batch_get_minutes_details` |
| Text and output | `get_minutes_transcription`, `get_minutes_ai_summary`, `get_minutes_keywords`, `list_minutes_todos` |
| Media availability | `get_minutes_media_url` |
| Vocabulary | `list_hot_words` |
| Memos | `list_audio_memos` |
| Templates | `list_custom_templates`, `list_system_templates` |

Notes on arguments, as observed:

- `list_my_minutes`: `pageSize` 1–20. Optional time filters are epoch milliseconds and must stay the same on every `nextToken` page.
- `get_minutes_transcription`: `taskUuid`, `maxResults` 1–500 (default 200), `direction` 0 or 1. Keep the same `direction` when following `nextToken`. Paragraphs carry text, speaker and time.
- `batch_get_minutes_details`: 1–50 UUIDs. Notes you cannot see are skipped as `OUT_OF_SCOPE` or `NOT_FOUND`.
- `get_minutes_ai_summary`: `hasSummary: false` can also mean the summary is still being generated.
- `get_minutes_media_url`: may return `available: false` or `INCOGNITO_MODE`. Text access does not imply audio access. The console saves original audio through the Recordings view instead.
- `list_audio_memos`: `pageSize` 1–1000, integer millisecond cursor, optional RFC 3339 time bounds.
- `list_custom_templates`: `maxResults` 1–10. `list_system_templates`: `maxResults` 1–50; keep the locale on every cursor page.

## Write tools (`qwennote:write`, opt-in)

`update_minutes_title`, `update_minutes_summary`, `replace_minutes_text`, `replace_speaker`, `add_personal_hot_word`, `delete_personal_hotword`.

Writes need a separate grant. In the reader that means `tools --login --write` with its own `--state-dir`; in the console it is a separate consent from Settings. The read-only grant stays narrow. The console's write path is built but still waits for a check against a real account (see [capabilities.md](capabilities.md)).

## How this project uses it

- OAuth 2.1 authorization code with PKCE S256, dynamic registration, refresh, all through the official Python MCP SDK.
- Loopback only: the callback is `http://127.0.0.1:<port>/callback` (reader default port 18765), or the console's own `/mcp/callback`.
- Credentials live in `.private/` (0700/0600). One process lock owns refresh for each store.
- Every non-interactive call has one 60-second budget, covering discovery, connection, initialization, listing and the call itself. Timeouts are reported and never retried automatically.

## Official and related documents

| Document | What it covers |
| --- | --- |
| [QwenNote A2 user guide](https://docs.qwennote.cn/zh/a2) | Device setup, recording, voice key, volume, firmware, unbinding (Chinese) |
| [QwenNote app user guide](https://docs.qwennote.cn/zh/app) | Notes, summaries, export, favorites, hot words, translation, voiceprint, smart slicing |
| [QwenNote downloads](https://www.qwennote.cn/download/) | Official app downloads |
| [MCP specification 2025-11-25: Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) | The OAuth flow the server follows |
| [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) · [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) · [RFC 7591](https://www.rfc-editor.org/rfc/rfc7591) · [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) | Resource metadata, server metadata, dynamic registration, PKCE |
| [DingTalk Workspace CLI](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli) | DingTalk's own minutes tools through a **different** gateway. Some names differ (`query_minutes_audio_url` and `get_minutes_media_url`, `query_user_tag_list` and `list_minutes_tags`). Its grants and schemas do not apply here. |
