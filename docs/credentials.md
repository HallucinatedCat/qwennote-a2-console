# Credentials

The console uses two separate sets of credentials. Keep them apart; each has its own private store.

| Set | Used for | How you get it |
| --- | --- | --- |
| **Document (MCP) login** | Notes, transcripts and memos through the vendor's official MCP document service | The vendor's own OAuth page (phone number and SMS code). Start it from the console's Settings or from the [MCP reader](../artifacts/mcp-reader/README.md). Nothing to prepare. |
| **Native session** | Recorder control, device settings, application-path note features, live transcript, assistant | **You provide it.** See below. |

## Notice

The native session is the same session the official mobile app holds after **you** sign in to **your own** account. This project does not include any tool, script or step-by-step guide for pulling it out of the vendor app, and no vendor keys or secrets are distributed here.

If you obtain these values yourself:

- use only your own account and your own bound device;
- check that doing so is lawful where you live and acceptable under the vendor's terms; this is your responsibility;
- treat every value as a password. Anyone with this file can control your recorder and read your notes.

## Native session file

`import_session.py` reads one JSON object. All fields are required unless marked optional.

| Field | Type | Meaning |
| --- | --- | --- |
| `endpoint` | string | The `wss://` long-connection endpoint the app uses. It must be on `qwennote.cn`, `qwennote.ai` or `dingtalk.com`, port 443. |
| `access_token` | string | The current access token of your signed-in session. |
| `expires_at` | number, optional | Access token expiry, in Unix seconds. |
| `refresh_token` | string | Refresh token of the same session. The backend renews the session with it. |
| `account_id` | string | Your account ID. |
| `account_type` | number | Account type as the app reports it. |
| `target_device_id` | number | The ID of your bound A2. |
| `app_key` | string | Application key the official app sends with each request. |
| `client_device_id` | string | The installation's device ID the app sends with each request. |
| `user_agent` | string | User agent the app sends. |
| `native_client_id` | string | Client ID used to sign session renewals. |
| `native_client_secret` | string | Client secret used to sign session renewals. |
| `native_auth_origin` | string | `https://minutes.qwennote.cn` or `https://minutes.qwennote.ai`. |

Shape (placeholders only):

```json
{
  "endpoint": "wss://<vendor-host>/<path>",
  "access_token": "<access token>",
  "expires_at": 1767225600,
  "refresh_token": "<refresh token>",
  "account_id": "<your account id>",
  "account_type": 0,
  "target_device_id": 0,
  "app_key": "<app key>",
  "client_device_id": "<installation id>",
  "user_agent": "<app user agent>",
  "native_client_id": "<client id>",
  "native_client_secret": "<client secret>",
  "native_auth_origin": "https://minutes.qwennote.cn"
}
```

## Import

Keep the file in a private directory, readable only by you:

```sh
install -d -m 700 ~/qwennote-handoff
chmod 600 ~/qwennote-handoff/native-session.json
cd artifacts/standalone-session
.venv/bin/python import_session.py --source ~/qwennote-handoff/native-session.json
```

The importer:

- refuses files or directories that others can read, symlinks, oversized or incomplete files;
- makes one live read and checks that the account and device in the file match before it saves anything;
- stores the session in `.private/native-session.json` (0600) and never prints values;
- refuses to overwrite an existing session unless you pass `--replace-same-account`, and never replaces it with a different account or device.

Stop the backend before importing. Then delete your handoff copy: after the first renewal it is stale, and using it in a second client would break the session.

Only one program may renew a given session. Do not import the same session into two backends or tools.

## Checking

The console's **Dev tools → Credentials** shows, without revealing any values, whether each credential is present, protected, valid, expiring or expired, and when it was last renewed.
