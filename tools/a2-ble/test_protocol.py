import asyncio
import json
import unittest

from a2ble import commands
from a2ble.framing import MAGIC_REQUEST, MAGIC_RESPONSE, decode_frame, encode_frame
from a2ble.pairing import Pairing
from a2ble.protocol import DeviceError, DeviceSession, ProtocolError

SYNTHETIC = Pairing(sn="SYNTH-SN", device_secret="00112233445566778899aabbccddeeff",
                     did="deadbeefdeadbeefdeadbeefdeadbeef", corp_id="synthetic-corp")

SYNTHETIC_RANDOM = "ABCDEF0123456789"  # 16 bytes, matches the AES NoPadding block size


class FakeDeviceTransport:
    """Stands in for BleTransport: decodes what the session wrote, and
    synthesizes a plausible device reply, entirely in memory -- no adapter,
    no device."""

    def __init__(self):
        self.sent_frames = []
        self._notify_cb = None
        self.response_overrides = {}  # cmd -> dict body to return instead of the default

    async def start_notify(self, callback):
        self._notify_cb = callback

    async def write(self, data: bytes, response: bool = False) -> None:
        frame = decode_frame(data)
        self.sent_frames.append(frame)
        body = json.loads(frame.body_text)
        reply = self.response_overrides.get(frame.cmd)
        if reply is None:
            reply = self._default_reply(frame.cmd, body)
        reply_frame = encode_frame(MAGIC_RESPONSE, frame.cmd, frame.msg_id, json.dumps(reply))
        self._notify_cb(reply_frame)

    async def disconnect(self):
        pass

    @staticmethod
    def _default_reply(cmd, body):
        if cmd == commands.GET_RANDOM:
            return {"code": 0, "random": SYNTHETIC_RANDOM}
        if cmd == commands.SYNC_DEV_STATUS:
            return {"code": 0, "online": 1, "battery_percent": 80}
        if cmd == commands.QUERY_FW_VERSION:
            return {"code": 0, "version": "1.2.3-synthetic"}
        if cmd == commands.SYS_CONTROL:
            return {"code": 0, "val": "80"}
        if cmd == commands.GET_FILE_LIST:
            return {"code": 0, "files": []}
        if cmd == commands.AUDIO_RECORD_OPT:
            return {"code": 0, "action": body.get("action")}
        return {"code": 0}


class DeviceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_default_is_get_random_then_connect_device_only(self):
        # Matches the official app's normal reconnect-to-a-bound-device
        # sequence: no AUTH.
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.connect()
        cmds = [f.cmd for f in transport.sent_frames]
        self.assertEqual(cmds, [commands.GET_RANDOM, commands.CONNECT_DEVICE])
        self.assertTrue(session.connected)

    async def test_connect_with_opt_in_auth_step(self):
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.connect(send_auth_step=True)
        cmds = [f.cmd for f in transport.sent_frames]
        self.assertEqual(cmds, [commands.GET_RANDOM, commands.AUTH,
                                 commands.GET_RANDOM, commands.CONNECT_DEVICE])

    async def test_auth_payload_is_aes_of_random_with_device_secret(self):
        from a2ble.aes import aes_encrypt
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.start()
        await session.authenticate()
        auth_frame = [f for f in transport.sent_frames if f.cmd == commands.AUTH][0]
        sent_body = json.loads(auth_frame.body_text)
        expected = aes_encrypt(SYNTHETIC.device_secret, SYNTHETIC_RANDOM)
        self.assertEqual(sent_body["payload"], expected)

    async def test_every_request_msg_id_is_unique(self):
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.connect()
        msg_ids = [f.msg_id for f in transport.sent_frames]
        self.assertEqual(len(msg_ids), len(set(msg_ids)))

    async def test_status_battery_firmware_files_record(self):
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.connect()

        status = await session.sync_dev_status()
        self.assertEqual(status["battery_percent"], 80)

        battery = await session.battery_percent()
        self.assertEqual(battery["val"], "80")

        fw = await session.query_fw_version()
        self.assertEqual(fw["version"], "1.2.3-synthetic")

        files = await session.get_file_list()
        self.assertEqual(files["files"], [])

        started = await session.record_start()
        self.assertEqual(started["action"], "start")
        stopped = await session.record_stop()
        self.assertEqual(stopped["action"], "stop")

    async def test_disconnect_device(self):
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.connect()
        await session.disconnect_device()
        self.assertFalse(session.connected)

    async def test_device_error_code_raises(self):
        transport = FakeDeviceTransport()
        transport.response_overrides[commands.SYNC_DEV_STATUS] = {"code": 7, "msg": "synthetic failure"}
        session = DeviceSession(transport, SYNTHETIC)
        await session.connect()
        with self.assertRaises(DeviceError) as ctx:
            await session.sync_dev_status()
        self.assertEqual(ctx.exception.code, 7)

    async def test_timeout_raises_protocol_error(self):
        class SilentTransport(FakeDeviceTransport):
            async def write(self, data, response=False):
                decode_frame(data)  # validate it, but never reply

        transport = SilentTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.start()
        with self.assertRaises(ProtocolError):
            await session._send(commands.SYNC_DEV_STATUS, commands.sync_dev_status_body(SYNTHETIC),
                                 timeout=0.05)

    async def test_unsolicited_notify_reaches_handler(self):
        transport = FakeDeviceTransport()
        session = DeviceSession(transport, SYNTHETIC)
        await session.start()
        received = []
        session.on_notify(lambda cmd, body: received.append((cmd, body)))
        from a2ble.framing import MAGIC_NOTIFY
        frame = encode_frame(MAGIC_NOTIFY, cmd=commands.SYNC_DEV_STATUS, msg_id=0,
                              body=json.dumps({"battery_percent": 50}))
        session._handle_raw(frame)
        self.assertEqual(received, [(commands.SYNC_DEV_STATUS, {"battery_percent": 50})])


if __name__ == "__main__":
    unittest.main()
