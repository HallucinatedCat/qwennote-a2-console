"""The BLE wire frame around each JSON command/response.

Confidence level: **low -- placeholder, not verified against a real frame.**

What is known from the official app:

- A request frame starts with magic byte ``0x13``, a notification (device
  initiated) with ``0x14``, and a response (reply to a request) with
  ``0x31``.
- The payload carried is JSON (command-specific fields, e.g. ``{"did":...}``).
- There is a per-command numeric id (``cmd``, e.g. 8/9/307/...) and the
  official app correlates requests and replies with an integer ``msgId`` that
  reappears in the parsed reply.

What is **not** confirmed, because no real frame has been captured: the
exact byte offsets/widths of ``cmd`` and ``msgId`` in the binary header, the
presence/algorithm of a trailing CRC (a CRC of some width is plausible but
its polynomial was not confirmed), and any length-prefix framing used to
split a JSON body across multiple BLE writes above the negotiated MTU.

This module implements one concrete, self-consistent guess so the rest of
the client has something to build and test against:

    [0]      magic        (1 byte)
    [1:3]    cmd           (uint16, little-endian)
    [3:5]    msg_id        (uint16, little-endian)
    [5:7]    body_len      (uint16, little-endian)
    [7:7+n]  body          (UTF-8 JSON, n = body_len bytes)
    [-2:]    crc16         (little-endian, CRC-16/CCITT-FALSE over bytes [0:-2])

Treat this exactly like a hypothesis: ``encode_frame``/``decode_frame`` round
trip against each other (and are unit-tested to), but neither has been
checked against one real frame written or notified by an actual QwenNote A2.
Before relying on this against a real device, capture one real exchange
(e.g. an Android ``btsnoop_hci.log`` from the official app, read only via
tools you already trust) and compare: if the magic byte lines up but the
rest does not, adjust ``HEADER_STRUCT`` below and re-run
``test_framing.py``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC_REQUEST = 0x13
MAGIC_NOTIFY = 0x14
MAGIC_RESPONSE = 0x31

_VALID_MAGIC = {MAGIC_REQUEST, MAGIC_NOTIFY, MAGIC_RESPONSE}

# magic(B) cmd(H) msg_id(H) body_len(H) -- little-endian, see module docstring.
HEADER_STRUCT = struct.Struct("<BHHH")


class FrameError(ValueError):
    pass


def crc16_ccitt_false(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect). A guess --
    see module docstring; swap this out if a real capture shows otherwise."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class Frame:
    magic: int
    cmd: int
    msg_id: int
    body: bytes

    @property
    def body_text(self) -> str:
        return self.body.decode("utf-8")


def encode_frame(magic: int, cmd: int, msg_id: int, body) -> bytes:
    if magic not in _VALID_MAGIC:
        raise FrameError("unknown magic byte %#x" % magic)
    if isinstance(body, str):
        body = body.encode("utf-8")
    if len(body) > 0xFFFF:
        raise FrameError("body too long: %d bytes" % len(body))
    header = HEADER_STRUCT.pack(magic, cmd & 0xFFFF, msg_id & 0xFFFF, len(body))
    without_crc = header + body
    crc = crc16_ccitt_false(without_crc)
    return without_crc + struct.pack("<H", crc)


def decode_frame(data: bytes) -> Frame:
    if len(data) < HEADER_STRUCT.size + 2:
        raise FrameError("frame too short: %d bytes" % len(data))
    magic, cmd, msg_id, body_len = HEADER_STRUCT.unpack_from(data, 0)
    if magic not in _VALID_MAGIC:
        raise FrameError("unknown magic byte %#x" % magic)
    start = HEADER_STRUCT.size
    end = start + body_len
    if end + 2 > len(data):
        raise FrameError("declared body_len %d does not fit in %d-byte frame" % (body_len, len(data)))
    body = data[start:end]
    crc_declared, = struct.unpack_from("<H", data, end)
    crc_actual = crc16_ccitt_false(data[:end])
    if crc_declared != crc_actual:
        raise FrameError("CRC mismatch: declared %#x, computed %#x" % (crc_declared, crc_actual))
    return Frame(magic=magic, cmd=cmd, msg_id=msg_id, body=body)


def split_for_mtu(data: bytes, mtu: int):
    """Yield chunks of at most `mtu` bytes. bleak's write_gatt_char already
    handles this for a single call under BLE's own ATT fragmentation in most
    cases, but some adapters need explicit chunking for long writes."""
    if mtu <= 0:
        raise FrameError("mtu must be positive")
    for i in range(0, len(data), mtu):
        yield data[i:i + mtu]
