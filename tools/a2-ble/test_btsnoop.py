"""btsnoop analysis against a synthetic Android HCI log built from this client's own frames."""
import json
import struct
import unittest

from a2ble import btsnoop, commands, framing
from a2ble.pairing import pairing_from_dict

try:
    from a2ble.aes import aes_encrypt
    aes_encrypt('00' * 16, 'x' * 16)
    HAVE_AES = True
except Exception:   # pycryptodome missing
    HAVE_AES = False

SECRET = '0f' * 16   # synthetic


def acl(connection, payload, start=True):
    flags = (0x2 if start else 0x1) << 12
    return bytes([0x02]) + struct.pack('<HH', connection | flags, len(payload)) + payload


def att(opcode, handle, value):
    body = bytes([opcode]) + struct.pack('<H', handle) + value
    return struct.pack('<HH', len(body), btsnoop.ATT_CID) + body


def log(packets):
    out = bytearray(b'btsnoop\x00' + struct.pack('>II', 1, 1002))
    for sent, packet in packets:
        out += struct.pack('>IIIIq', len(packet), len(packet), 0 if sent else 1, 0, 0) + packet
    return bytes(out)


class BtsnoopTests(unittest.TestCase):
    def frame(self, magic, cmd, msg_id, body):
        return framing.encode_frame(magic, cmd, msg_id, json.dumps(body))

    def test_frames_are_found_reassembled_and_values_hidden(self):
        random = 'r' * 16
        request = att(btsnoop.ATT_WRITE_REQ, 0x0021, self.frame(framing.MAGIC_REQUEST, commands.GET_RANDOM, 1, {'sn': 'SN-SYNTH', 'did': 'a' * 32}))
        reply = att(btsnoop.ATT_NOTIFY, 0x0024, self.frame(framing.MAGIC_RESPONSE, commands.GET_RANDOM, 1, {'random': random}))
        first, rest = request[:10], request[10:]   # one write split over two ACL fragments
        data = log([(True, acl(0x40, first)), (True, acl(0x40, rest, start=False)), (False, acl(0x40, reply)),
                    (True, acl(0x40, att(btsnoop.ATT_WRITE_CMD, 0x0021, b'\x99not-a-frame')))])
        report = btsnoop.analyze(data)
        self.assertEqual([(row['direction'], row['cmd']) for row in report.frames], [('phone->A2', 8), ('A2->phone', 8)])
        text = btsnoop.format_report(report)
        self.assertNotIn('SN-SYNTH', text);self.assertIn("keys=['did', 'sn']", text)

    @unittest.skipUnless(HAVE_AES, 'pycryptodome not installed')
    def test_token_check_reports_match_with_the_right_secret(self):
        random = 'abcdefghijklmnop'
        token = aes_encrypt(SECRET, random)
        frames = [(False, att(btsnoop.ATT_NOTIFY, 0x24, self.frame(framing.MAGIC_RESPONSE, commands.GET_RANDOM, 1, {'random': random}))),
                  (True, att(btsnoop.ATT_WRITE_REQ, 0x21, self.frame(framing.MAGIC_REQUEST, commands.CONNECT_DEVICE, 2, {'token': token})))]
        data = log([(sent, acl(0x40, packet)) for sent, packet in frames])
        good = pairing_from_dict({'sn': 'SN', 'device_secret': SECRET})
        wrong = pairing_from_dict({'sn': 'SN', 'device_secret': '1e' * 16})
        self.assertEqual(btsnoop.analyze(data, good).values[0]['result'], 'match')
        self.assertEqual(btsnoop.analyze(data, wrong).values[0]['result'], 'no match')

    def test_other_logs_are_refused(self):
        with self.assertRaises(ValueError):
            list(btsnoop.records(b'not a log'))
        with self.assertRaises(ValueError):
            list(btsnoop.records(b'btsnoop\x00' + struct.pack('>II', 1, 2001)))


if __name__ == '__main__':
    unittest.main()
