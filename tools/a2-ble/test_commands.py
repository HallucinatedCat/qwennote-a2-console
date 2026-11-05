import unittest

from a2ble import commands
from a2ble.pairing import Pairing

SYNTHETIC = Pairing(sn="SYNTH-SN", device_secret="00112233445566778899aabbccddeeff",
                     did="deadbeefdeadbeefdeadbeefdeadbeef", corp_id="synthetic-corp")


class CommandBodyTests(unittest.TestCase):
    def test_get_random_body(self):
        body = commands.get_random_body(SYNTHETIC)
        self.assertEqual(body, {"corp_id": "synthetic-corp", "did": SYNTHETIC.did})

    def test_auth_body_fields(self):
        body = commands.auth_body(SYNTHETIC, payload_hex="aabb", sdk_ver="1.0", timestamp_ms=1234)
        self.assertEqual(body, {"timestamp": "1234", "payload": "aabb", "sdk_ver": "1.0",
                                 "did": SYNTHETIC.did})

    def test_connect_device_body_fields(self):
        body = commands.connect_device_body(SYNTHETIC, token_hex="ccdd", model="Windows_test",
                                             sdk_ver="1.0", timestamp_s=5678)
        self.assertEqual(body, {"corp_id": "synthetic-corp", "token": "ccdd", "model": "Windows_test",
                                 "timestamp": "5678", "sdk_ver": "1.0", "did": SYNTHETIC.did})

    def test_disconnect_device_body(self):
        body = commands.disconnect_device_body(SYNTHETIC)
        self.assertEqual(body, {"corp_id": "synthetic-corp", "did": SYNTHETIC.did})

    def test_sync_dev_status_body(self):
        self.assertEqual(commands.sync_dev_status_body(SYNTHETIC), {"did": SYNTHETIC.did})

    def test_query_fw_version_body(self):
        self.assertEqual(commands.query_fw_version_body(SYNTHETIC), {"did": SYNTHETIC.did})

    def test_sys_control_battery_body(self):
        body = commands.sys_control_battery_body(SYNTHETIC)
        self.assertEqual(body, {"key": commands.SYS_CONTROL_KEY_GET_BATTERY_PERCENT, "did": SYNTHETIC.did})
        self.assertEqual(body["key"], 10001)

    def test_get_file_list_body_defaults(self):
        body = commands.get_file_list_body(SYNTHETIC)
        self.assertEqual(body, {"s_fid": "", "e_fid": "", "recently": 0, "did": SYNTHETIC.did})

    def test_get_file_list_body_custom(self):
        body = commands.get_file_list_body(SYNTHETIC, s_fid="10", e_fid="20", recently=1)
        self.assertEqual(body["s_fid"], "10")
        self.assertEqual(body["e_fid"], "20")
        self.assertEqual(body["recently"], 1)

    def test_audio_record_opt_valid_actions(self):
        for action in commands.AUDIO_RECORD_ACTIONS:
            body = commands.audio_record_opt_body(SYNTHETIC, action)
            self.assertEqual(body, {"action": action, "did": SYNTHETIC.did})

    def test_audio_record_opt_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            commands.audio_record_opt_body(SYNTHETIC, "reboot")

    def test_did_is_always_the_client_id_not_the_serial(self):
        # Every body's "did" must be the pairing's client id, never the sn --
        # see commands.py docstring.
        for body in (commands.get_random_body(SYNTHETIC),
                     commands.sync_dev_status_body(SYNTHETIC),
                     commands.disconnect_device_body(SYNTHETIC)):
            self.assertEqual(body["did"], SYNTHETIC.did)
            self.assertNotEqual(body["did"], SYNTHETIC.sn)

    def test_forbidden_commands_have_no_builder(self):
        forbidden_names = {
            "reset_device_body", "ota_body", "gray_switch_body",
            "active_device_body", "file_delete_body", "vendor_set_body",
            "sys_control_reboot_body", "sys_control_format_body",
            "sys_control_clear_ota_body",
        }
        for name in forbidden_names:
            self.assertFalse(hasattr(commands, name), "must not implement %s" % name)

    def test_forbidden_cmd_ids_documented(self):
        for cmd_id in (4, 311, 7, 275, 312):
            self.assertIn(cmd_id, commands._FORBIDDEN_CMDS)


if __name__ == "__main__":
    unittest.main()
