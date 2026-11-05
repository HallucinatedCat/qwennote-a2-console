"""The session: framing + AES + commands, over a transport.

This talks to the device directly. This module is responsible for allocating
the per-request ``msg_id`` used to match a reply to its request; it is a
simple counter.

Confidence level: the command ids and JSON field names in
:mod:`a2ble.commands` are high-confidence (they match the official app). The byte
framing in :mod:`a2ble.framing` and the AES recipe in :mod:`a2ble.aes` are
best-effort reconstructions that still need verification against a real
device. See both modules' docstrings before trusting this against hardware.

Call sequence as the official app does it (not a device observation of this
client): a normal reconnect to an already-bound device is GET_RANDOM (8) then
CONNECT_DEVICE (307) -- no AUTH. AUTH (cmd 9) exists only on the official
app's unbind path, immediately before a cloud unbind call and RESET_DEVICE, neither of which
this package implements. `DeviceSession.connect()` therefore does not send
AUTH by default; see `authenticate()` and `connect()`'s docstrings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import time
from dataclasses import dataclass, field
from typing import Optional

from . import commands
from .aes import aes_encrypt
from .framing import MAGIC_REQUEST, MAGIC_RESPONSE, MAGIC_NOTIFY, Frame, decode_frame, encode_frame
from .pairing import Pairing

log = logging.getLogger("a2ble.protocol")

DEFAULT_SDK_VER = "a2ble-0.1.0"  # placeholder; the real app's GetSdkVer() value is unknown here
REQUEST_TIMEOUT_S = 10.0  # the official app's 10 s command timeout


class ProtocolError(RuntimeError):
    pass


class DeviceError(ProtocolError):
    """The device answered with a non-success `code` in its response body."""

    def __init__(self, cmd: int, code, message: str):
        super().__init__("cmd %s failed: code=%s msg=%s" % (cmd, code, message))
        self.cmd = cmd
        self.code = code


def _client_model_string() -> str:
    """Stand-in for the app's `Android_<brand>_<model>` string; this is a
    desktop client, so it identifies itself as one instead of pretending to
    be a phone."""
    return "Windows_a2ble_%s" % platform.release()


@dataclass
class _Pending:
    future: "asyncio.Future"
    cmd: int


class DeviceSession:
    """One authenticated session against one paired A2 over one transport."""

    def __init__(self, transport, pairing: Pairing, sdk_ver: str = DEFAULT_SDK_VER):
        self.transport = transport
        self.pairing = pairing
        self.sdk_ver = sdk_ver
        self._msg_id = 0
        self._pending: dict[int, _Pending] = {}
        self._notify_handlers = []
        self.connected = False

    def on_notify(self, handler):
        """Register a callback(cmd: int, body: dict) for unsolicited device
        notifications (MAGIC_NOTIFY frames), e.g. battery/status pushes."""
        self._notify_handlers.append(handler)

    def _next_msg_id(self) -> int:
        self._msg_id = (self._msg_id + 1) & 0xFFFF
        if self._msg_id == 0:
            self._msg_id = 1
        return self._msg_id

    # ------------------------------------------------------------------ wire
    def _handle_raw(self, data: bytes) -> None:
        try:
            frame = decode_frame(data)
        except Exception:
            log.warning("dropping unparseable frame (%d bytes)", len(data))
            return
        try:
            body = json.loads(frame.body_text) if frame.body else {}
        except ValueError:
            log.warning("cmd %s: non-JSON body", frame.cmd)
            body = {}
        if frame.magic == MAGIC_NOTIFY:
            for handler in self._notify_handlers:
                handler(frame.cmd, body)
            return
        if frame.magic != MAGIC_RESPONSE:
            log.warning("unexpected magic %#x for cmd %s", frame.magic, frame.cmd)
            return
        pending = self._pending.pop(frame.msg_id, None)
        if pending is None:
            log.warning("response for unknown msg_id %s (cmd %s)", frame.msg_id, frame.cmd)
            return
        if not pending.future.done():
            pending.future.set_result(body)

    async def _send(self, cmd: int, body: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        msg_id = self._next_msg_id()
        frame = encode_frame(MAGIC_REQUEST, cmd, msg_id, json.dumps(body))
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[msg_id] = _Pending(future=fut, cmd=cmd)
        await self.transport.write(frame)
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise ProtocolError("cmd %d timed out after %.1fs (msg_id=%d)" % (cmd, timeout, msg_id))
        if isinstance(reply.get("code"), int) and reply["code"] not in (0,):
            raise DeviceError(cmd, reply.get("code"), reply.get("msg") or reply.get("message") or "")
        return reply

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.transport.start_notify(self._handle_raw)

    async def _get_random(self) -> str:
        reply = await self._send(commands.GET_RANDOM, commands.get_random_body(self.pairing))
        random = reply.get("random")
        if not random:
            raise ProtocolError("GET_RANDOM reply had no 'random' field: %r" % reply)
        return random

    async def authenticate(self) -> dict:
        """GET_RANDOM then AUTH (cmd 9).

        Not used by `connect()` below: in the official app, AUTH (cmd 9) is
        sent only from the *unbind* flow, right before the app tells the cloud to unbind the device and then issues
        RESET_DEVICE. A normal reconnect to an already-bound device never
        calls it -- see `connect_device()`. This method is kept only for
        completeness/testing of the AUTH command shape; this package never
        calls it automatically, and nothing in this package implements the
        unbind/reset sequence it precedes."""
        random = await self._get_random()
        payload = aes_encrypt(self.pairing.device_secret, random)
        body = commands.auth_body(self.pairing, payload, self.sdk_ver)
        return await self._send(commands.AUTH, body)

    async def connect_device(self) -> dict:
        """GET_RANDOM then CONNECT_DEVICE (cmd 307).

        This is the reconnect sequence the official app uses: a normal
        reconnect to an already-bound device sends GET_RANDOM then
        CONNECT_DEVICE, with no AUTH (cmd 9) in between."""
        random = await self._get_random()
        token = aes_encrypt(self.pairing.device_secret, random)
        body = commands.connect_device_body(self.pairing, token, _client_model_string(), self.sdk_ver)
        reply = await self._send(commands.CONNECT_DEVICE, body)
        self.connected = True
        return reply

    async def connect(self, *, send_auth_step: bool = False) -> dict:
        """Run the handshake: start notifications, then CONNECT_DEVICE
        (GET_RANDOM + CONNECT_DEVICE), matching the official app's normal
        reconnect-to-a-bound-device sequence.

        `send_auth_step=True` additionally sends AUTH (cmd 9) first, for
        parity with the task's three-step auth-flow description. Source
        tracing found that step is really only used by the official app's
        unbind flow (see `authenticate()`'s docstring); it defaults to off
        so this client's normal connect matches what a reconnecting device
        actually sees."""
        await self.start()
        if send_auth_step:
            await self.authenticate()
        return await self.connect_device()

    async def disconnect_device(self) -> dict:
        reply = await self._send(commands.DISCONNECT_DEVICE, commands.disconnect_device_body(self.pairing))
        self.connected = False
        return reply

    # ---------------------------------------------------------------- queries
    async def sync_dev_status(self) -> dict:
        return await self._send(commands.SYNC_DEV_STATUS, commands.sync_dev_status_body(self.pairing))

    async def query_fw_version(self) -> dict:
        return await self._send(commands.QUERY_FW_VERSION, commands.query_fw_version_body(self.pairing))

    async def battery_percent(self) -> dict:
        return await self._send(commands.SYS_CONTROL, commands.sys_control_battery_body(self.pairing))

    async def get_file_list(self, s_fid: str = "", e_fid: str = "", recently: int = 0) -> dict:
        body = commands.get_file_list_body(self.pairing, s_fid, e_fid, recently)
        return await self._send(commands.GET_FILE_LIST, body)

    async def record_start(self) -> dict:
        return await self._audio_record_opt("start")

    async def record_stop(self) -> dict:
        return await self._audio_record_opt("stop")

    async def record_pause(self) -> dict:
        return await self._audio_record_opt("pause")

    async def record_resume(self) -> dict:
        return await self._audio_record_opt("resume")

    async def record_status(self) -> dict:
        return await self._audio_record_opt("get")

    async def _audio_record_opt(self, action: str) -> dict:
        body = commands.audio_record_opt_body(self.pairing, action)
        return await self._send(commands.AUDIO_RECORD_OPT, body)
