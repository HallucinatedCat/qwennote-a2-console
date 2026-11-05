"""Read an Android Bluetooth HCI snoop log and check this client against real A2 traffic.

Turn on Developer options -> "Enable Bluetooth HCI snoop log" on your phone, toggle
Bluetooth, let the official app connect to the A2 once, then copy btsnoop_hci.log (on most
phones: adb bugreport, FS/data/misc/bluetooth/logs/btsnoop_hci.log). This module extracts the
ATT writes and notifications that carry A2 frames (first byte 0x13, 0x14 or 0x31) and reports:

- the frame header bytes and lengths, to confirm or correct framing.py;
- whether framing.decode_frame accepts each frame;
- with your pairing file, whether aes.aes_encrypt(device_secret, random) reproduces the
  token the official app sent in CONNECT_DEVICE.

By default only lengths, header bytes, command ids, JSON key names and match/no-match are
printed; values (serials, tokens) stay out of the output unless --show-values is given.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

from . import framing

MAGICS = {framing.MAGIC_REQUEST: 'request', framing.MAGIC_NOTIFY: 'notify', framing.MAGIC_RESPONSE: 'response'}
ATT_CID = 0x0004
ATT_WRITE_REQ, ATT_WRITE_CMD, ATT_NOTIFY, ATT_INDICATE = 0x12, 0x52, 0x1B, 0x1D


@dataclass
class AttValue:
    time_us: int
    sent: bool        # True: phone -> A2 (write); False: A2 -> phone (notification)
    handle: int
    value: bytes


@dataclass
class Report:
    values: list = field(default_factory=list)
    frames: list = field(default_factory=list)


def records(data: bytes):
    """(flags, timestamp, packet) for each btsnoop record. H4 (1002) logs only."""
    if data[:8] != b'btsnoop\x00':
        raise ValueError('Not a btsnoop log')
    version, datalink = struct.unpack_from('>II', data, 8)
    if version != 1 or datalink != 1002:
        raise ValueError(f'Unsupported btsnoop log (version {version}, datalink {datalink}); expected HCI UART (1002)')
    offset = 16
    while offset + 24 <= len(data):
        _, included, flags, _, stamp = struct.unpack_from('>IIIIq', data, offset)
        offset += 24
        yield flags, stamp, data[offset:offset + included]
        offset += included


def att_values(data: bytes):
    """ATT writes and notifications, with ACL fragments reassembled per connection and direction."""
    partial = {}
    for flags, stamp, packet in records(data):
        if not packet or packet[0] != 0x02 or len(packet) < 5:   # H4 ACL data only
            continue
        sent = not (flags & 1)
        header, length = struct.unpack_from('<HH', packet, 1)
        connection, boundary = header & 0x0FFF, (header >> 12) & 0x3
        payload = packet[5:5 + length]
        key = (connection, sent)
        if boundary == 0x1:                                       # continuation fragment
            if key not in partial:
                continue
            partial[key][1] += payload
        else:
            if len(payload) < 4:
                continue
            total, _ = struct.unpack_from('<HH', payload, 0)
            partial[key] = [total, bytearray(payload)]
        total, buffer = partial[key]
        if len(buffer) < 4 + total:
            continue
        del partial[key]
        l2cap_len, cid = struct.unpack_from('<HH', buffer, 0)
        att = bytes(buffer[4:4 + l2cap_len])
        if cid != ATT_CID or len(att) < 3:
            continue
        opcode, handle = att[0], struct.unpack_from('<H', att, 1)[0]
        if sent and opcode in (ATT_WRITE_REQ, ATT_WRITE_CMD):
            yield AttValue(stamp, True, handle, att[3:])
        elif not sent and opcode in (ATT_NOTIFY, ATT_INDICATE):
            yield AttValue(stamp, False, handle, att[3:])


def analyze(data: bytes, pairing=None, show_values: bool = False) -> Report:
    report = Report()
    decoded = []
    for value in att_values(data):
        if not value.value or value.value[0] not in MAGICS:
            continue
        row = {'direction': 'phone->A2' if value.sent else 'A2->phone', 'handle': value.handle, 'length': len(value.value),
               'header_hex': value.value[:9].hex(' '), 'kind': MAGICS[value.value[0]], 'decodes': False}
        try:
            frame = framing.decode_frame(value.value)
            body = json.loads(frame.body.decode('utf-8')) if frame.body else {}
            row.update(decodes=True, cmd=frame.cmd, msg_id=frame.msg_id, keys=sorted(body) if isinstance(body, dict) else None)
            if show_values:
                row['body'] = body
            decoded.append((frame, body))
        except (framing.FrameError, ValueError, UnicodeDecodeError):
            pass
        report.frames.append(row)
    if pairing is not None:
        report.values.append(check_token(decoded, pairing))
    return report


def check_token(decoded, pairing) -> dict:
    """Does aes_encrypt(device_secret, random) give the token the official app sent?"""
    from .aes import aes_encrypt
    from . import commands
    random = next((body.get('random') for frame, body in decoded
                   if frame.cmd == commands.GET_RANDOM and isinstance(body, dict) and body.get('random')), None)
    token = next((body.get('token') for frame, body in decoded
                  if frame.cmd == commands.CONNECT_DEVICE and isinstance(body, dict) and body.get('token')), None)
    if random is None or token is None:
        return {'check': 'token', 'result': 'not found in capture (needs decodable GET_RANDOM reply and CONNECT_DEVICE)'}
    try:
        ours = aes_encrypt(pairing.device_secret, random)
    except Exception as error:   # the guessed recipe itself refused the input
        return {'check': 'token', 'result': f'our AES recipe failed: {type(error).__name__}'}
    return {'check': 'token', 'result': 'match' if str(ours).lower() == str(token).lower() else 'no match'}


def format_report(report: Report) -> str:
    lines = [f"{len(report.frames)} A2 frames found"]
    for row in report.frames:
        detail = f"cmd={row['cmd']} msg_id={row['msg_id']} keys={row['keys']}" if row['decodes'] else 'does not decode with framing.py'
        lines.append(f"{row['direction']:9} {row['kind']:8} handle={row['handle']:#06x} len={row['length']:4}  [{row['header_hex']}]  {detail}")
        if 'body' in row:
            lines.append('          ' + json.dumps(row['body'], ensure_ascii=False))
    for value in report.values:
        lines.append(f"{value['check']}: {value['result']}")
    return '\n'.join(lines)
