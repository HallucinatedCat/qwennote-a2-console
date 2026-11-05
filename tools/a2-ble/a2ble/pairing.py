"""Loading your pairing file.

The pairing file is a small JSON document you create yourself, once,
from your own cloud account data (the same bound-device record the official
app reads). This tool never fetches it from any cloud service itself.

Required keys, following the official app's bound-device model:

- ``sn``: the recorder's serial number. Cloud field ``sn``.
- ``device_secret``: the per-device secret used as the AES key material for
  the GET_RANDOM/AUTH/CONNECT_DEVICE handshake. Cloud field ``deviceSecret``
  (persisted locally by the app). This is exactly the value the project's
  own host backend treats as private and never logs or forwards -- treat it
  the same way here.
- ``did``: a per-installation client identifier. In the official app this is
  *not* the recorder's serial and *not* a device secret -- it is a locally
  generated id (a UUID4 with dashes stripped) that the phone persists once
  and reuses for every outgoing command body (stamped onto every BLE command
  as the ``"did"`` JSON field). Any stable 32-hex-char
  string works; this tool generates one for you if you omit it.
- ``corp_id`` (optional): the account/corp id the device is bound under
  (``"corp_id"`` JSON field, sourced from the signed-in account, not the
  device). Required by the real protocol for GET_RANDOM/AUTH/CONNECT_DEVICE;
  if your account does not use multi-corp binding this may be blank -- the
  device is the authority on whether it accepts an empty value.

Nothing in this module prints, logs, or returns these values anywhere except
to the caller that explicitly asked for a loaded :class:`Pairing`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_HEX_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


class PairingError(ValueError):
    pass


@dataclass
class Pairing:
    sn: str
    device_secret: str
    did: str
    corp_id: str = ""
    name: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # never let a stray print leak the secret
        return "Pairing(sn=%r, did=%r, corp_id=%r, device_secret=<redacted>)" % (
            self.sn, self.did, self.corp_id)

    __str__ = __repr__


def default_pairing_path() -> Path:
    """A Windows-friendly default location: %LOCALAPPDATA%\\a2ble\\pairing.json,
    or ~/.a2ble/pairing.json elsewhere."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "a2ble" / "pairing.json"
    return Path.home() / ".a2ble" / "pairing.json"


def generate_client_id() -> str:
    return uuid.uuid4().hex


def load_pairing(path) -> Pairing:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PairingError("cannot read pairing file %s: %s" % (path, exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PairingError("pairing file %s is not valid JSON: %s" % (path, exc)) from exc
    return pairing_from_dict(data)


def pairing_from_dict(data: dict) -> Pairing:
    if not isinstance(data, dict):
        raise PairingError("pairing data must be a JSON object")
    sn = data.get("sn")
    secret = data.get("device_secret")
    if not sn or not isinstance(sn, str):
        raise PairingError("pairing file is missing a non-empty 'sn'")
    if not secret or not isinstance(secret, str):
        raise PairingError("pairing file is missing a non-empty 'device_secret'")
    did = data.get("did") or generate_client_id()
    if not isinstance(did, str) or not _HEX_ID_RE.match(did):
        raise PairingError("'did' must be a short hex string (8-64 chars) if given")
    corp_id = data.get("corp_id") or ""
    if not isinstance(corp_id, str):
        raise PairingError("'corp_id' must be a string if given")
    name = data.get("name")
    known = {"sn", "device_secret", "did", "corp_id", "name"}
    extra = {k: v for k, v in data.items() if k not in known}
    return Pairing(sn=sn, device_secret=secret, did=did, corp_id=corp_id, name=name, extra=extra)


def save_generated_did(path, did: str) -> None:
    """Persist a freshly generated client id back into the pairing file so the
    same one is reused next time (mirrors the app persisting it once)."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["did"] = did
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX permissions (Windows)
