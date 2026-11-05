"""Command ids and JSON body builders.

Confidence level: **high** for the command ids and JSON field names -- these
match what the official app sends over BLE.

Two things every outgoing body gets, as the official app does it right before
every command is sent:

- ``"did"`` is **always** stamped to the *phone's own* locally-persisted
  client id (see ``a2ble.pairing`` docstring) -- not the recorder's serial.
  Any value callers pass for ``did`` elsewhere is overwritten by this, so
  this module always sets it uniformly instead of pretending otherwise.
- The recorder's serial (``sn``) is used only for BLE discovery/display
  (advertised name / GATT target); it is not a field in these JSON bodies.

Deliberately not implemented here -- no code
path in this package can produce these: RESET_DEVICE (4), any OTA command,
SYS_CONTROL reboot (1) / format (2) / clear-OTA (3), GRAY_SWITCH (311),
ACTIVE_DEVICE (7), FILE_DELETE (275), VENDOR_SET (312).
"""

from __future__ import annotations

import time
from typing import Optional

from .pairing import Pairing

# -- command ids ---------------------------------------------------------------
GET_RANDOM = 8
AUTH = 9
AUDIO_RECORD_OPT = 256
GET_FILE_LIST = 272
SYNC_DEV_STATUS = 306
CONNECT_DEVICE = 307
DISCONNECT_DEVICE = 308
QUERY_FW_VERSION = 309
SYS_CONTROL = 310

SYS_CONTROL_KEY_GET_BATTERY_PERCENT = 10001  # the only SYS_CONTROL key this client sends

AUDIO_RECORD_ACTIONS = ("start", "stop", "pause", "resume", "get")

# Commands this package will never build, named for anyone extending it.
_FORBIDDEN_CMDS = {
    4: "RESET_DEVICE",
    311: "GRAY_SWITCH",
    7: "ACTIVE_DEVICE",
    275: "FILE_DELETE",
    312: "VENDOR_SET",
    # OTA and SYS_CONTROL reboot/format/clear-OTA have no builder at all below.
}


def _base_body(pairing: Pairing) -> dict:
    return {"did": pairing.did}


def get_random_body(pairing: Pairing) -> dict:
    body = {"corp_id": pairing.corp_id}
    body.update(_base_body(pairing))
    return body


def auth_body(pairing: Pairing, payload_hex: str, sdk_ver: str, timestamp_ms: Optional[int] = None) -> dict:
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    body = {"timestamp": str(ts), "payload": payload_hex, "sdk_ver": sdk_ver}
    body.update(_base_body(pairing))
    return body


def connect_device_body(pairing: Pairing, token_hex: str, model: str, sdk_ver: str,
                         timestamp_s: Optional[int] = None) -> dict:
    ts = timestamp_s if timestamp_s is not None else int(time.time())
    body = {"corp_id": pairing.corp_id, "token": token_hex, "model": model,
            "timestamp": str(ts), "sdk_ver": sdk_ver}
    body.update(_base_body(pairing))
    return body


def disconnect_device_body(pairing: Pairing) -> dict:
    body = {"corp_id": pairing.corp_id}
    body.update(_base_body(pairing))
    return body


def sync_dev_status_body(pairing: Pairing) -> dict:
    return _base_body(pairing)


def query_fw_version_body(pairing: Pairing) -> dict:
    return _base_body(pairing)


def sys_control_battery_body(pairing: Pairing) -> dict:
    body = {"key": SYS_CONTROL_KEY_GET_BATTERY_PERCENT}
    body.update(_base_body(pairing))
    return body


def get_file_list_body(pairing: Pairing, s_fid: str = "", e_fid: str = "", recently: int = 0) -> dict:
    body = {"s_fid": s_fid, "e_fid": e_fid, "recently": recently}
    body.update(_base_body(pairing))
    return body


def audio_record_opt_body(pairing: Pairing, action: str) -> dict:
    if action not in AUDIO_RECORD_ACTIONS:
        raise ValueError("action must be one of %s, got %r" % (AUDIO_RECORD_ACTIONS, action))
    body = {"action": action}
    body.update(_base_body(pairing))
    return body
