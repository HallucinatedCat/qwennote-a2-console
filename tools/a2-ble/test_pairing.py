import json
import tempfile
import unittest
from pathlib import Path

from a2ble.pairing import (Pairing, PairingError, generate_client_id, load_pairing,
                            pairing_from_dict, save_generated_did)

SYNTHETIC = {
    "sn": "SYNTH-SN-0001",
    "device_secret": "00112233445566778899aabbccddeeff",
    "did": "deadbeefdeadbeefdeadbeefdeadbeef",
    "corp_id": "synthetic-corp-id",
    "name": "Synthetic A2",
}


class PairingFromDictTests(unittest.TestCase):
    def test_full_record(self):
        p = pairing_from_dict(SYNTHETIC)
        self.assertEqual(p.sn, SYNTHETIC["sn"])
        self.assertEqual(p.device_secret, SYNTHETIC["device_secret"])
        self.assertEqual(p.did, SYNTHETIC["did"])
        self.assertEqual(p.corp_id, SYNTHETIC["corp_id"])
        self.assertEqual(p.name, SYNTHETIC["name"])

    def test_missing_sn_rejected(self):
        data = dict(SYNTHETIC)
        del data["sn"]
        with self.assertRaises(PairingError):
            pairing_from_dict(data)

    def test_missing_secret_rejected(self):
        data = dict(SYNTHETIC)
        del data["device_secret"]
        with self.assertRaises(PairingError):
            pairing_from_dict(data)

    def test_did_is_generated_when_absent(self):
        data = dict(SYNTHETIC)
        del data["did"]
        p = pairing_from_dict(data)
        self.assertRegex(p.did, r"^[0-9a-f]{8,64}$")

    def test_corp_id_defaults_to_empty(self):
        data = dict(SYNTHETIC)
        del data["corp_id"]
        p = pairing_from_dict(data)
        self.assertEqual(p.corp_id, "")

    def test_bad_did_format_rejected(self):
        data = dict(SYNTHETIC)
        data["did"] = "not hex!"
        with self.assertRaises(PairingError):
            pairing_from_dict(data)

    def test_non_dict_rejected(self):
        with self.assertRaises(PairingError):
            pairing_from_dict(["not", "a", "dict"])

    def test_unknown_fields_preserved_in_extra(self):
        data = dict(SYNTHETIC)
        data["firmware_version"] = "1.2.3-test"
        p = pairing_from_dict(data)
        self.assertEqual(p.extra.get("firmware_version"), "1.2.3-test")

    def test_repr_never_shows_secret(self):
        p = pairing_from_dict(SYNTHETIC)
        self.assertNotIn(SYNTHETIC["device_secret"], repr(p))
        self.assertNotIn(SYNTHETIC["device_secret"], str(p))


class GenerateClientIdTests(unittest.TestCase):
    def test_format(self):
        did = generate_client_id()
        self.assertRegex(did, r"^[0-9a-f]{32}$")

    def test_unique_enough(self):
        self.assertNotEqual(generate_client_id(), generate_client_id())


class LoadAndPersistTests(unittest.TestCase):
    def test_load_pairing_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pairing.json"
            path.write_text(json.dumps(SYNTHETIC), encoding="utf-8")
            p = load_pairing(path)
            self.assertEqual(p.sn, SYNTHETIC["sn"])

    def test_load_missing_file_raises(self):
        with self.assertRaises(PairingError):
            load_pairing("/nonexistent/path/pairing.json")

    def test_load_bad_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pairing.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(PairingError):
                load_pairing(path)

    def test_save_generated_did_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pairing.json"
            data = dict(SYNTHETIC)
            del data["did"]
            path.write_text(json.dumps(data), encoding="utf-8")
            save_generated_did(path, "cafebabecafebabecafebabecafebabe")
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["did"], "cafebabecafebabecafebabecafebabe")
            self.assertEqual(reloaded["sn"], SYNTHETIC["sn"])


if __name__ == "__main__":
    unittest.main()
