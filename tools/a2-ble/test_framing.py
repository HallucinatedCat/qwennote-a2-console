import unittest

from a2ble.framing import (MAGIC_NOTIFY, MAGIC_REQUEST, MAGIC_RESPONSE, FrameError,
                            crc16_ccitt_false, decode_frame, encode_frame, split_for_mtu)


class FramingRoundTripTests(unittest.TestCase):
    def test_round_trip_request(self):
        body = '{"did":"synthetic-did","corp_id":"synthetic-corp"}'
        frame = encode_frame(MAGIC_REQUEST, cmd=8, msg_id=42, body=body)
        decoded = decode_frame(frame)
        self.assertEqual(decoded.magic, MAGIC_REQUEST)
        self.assertEqual(decoded.cmd, 8)
        self.assertEqual(decoded.msg_id, 42)
        self.assertEqual(decoded.body_text, body)

    def test_round_trip_bytes_body(self):
        frame = encode_frame(MAGIC_RESPONSE, cmd=307, msg_id=1, body=b'{"code":0}')
        decoded = decode_frame(frame)
        self.assertEqual(decoded.body, b'{"code":0}')

    def test_round_trip_notify(self):
        frame = encode_frame(MAGIC_NOTIFY, cmd=960010, msg_id=0, body="{}")
        decoded = decode_frame(frame)
        self.assertEqual(decoded.magic, MAGIC_NOTIFY)

    def test_msg_id_wraps_in_uint16(self):
        frame = encode_frame(MAGIC_REQUEST, cmd=9, msg_id=0x10001, body="{}")
        decoded = decode_frame(frame)
        self.assertEqual(decoded.msg_id, 1)

    def test_unknown_magic_rejected_on_encode(self):
        with self.assertRaises(FrameError):
            encode_frame(0x99, cmd=1, msg_id=1, body="{}")

    def test_unknown_magic_rejected_on_decode(self):
        frame = bytearray(encode_frame(MAGIC_REQUEST, cmd=1, msg_id=1, body="{}"))
        frame[0] = 0x99
        with self.assertRaises(FrameError):
            decode_frame(bytes(frame))

    def test_truncated_frame_rejected(self):
        with self.assertRaises(FrameError):
            decode_frame(b"\x13\x00")

    def test_crc_mismatch_detected(self):
        frame = bytearray(encode_frame(MAGIC_REQUEST, cmd=8, msg_id=1, body="{}"))
        frame[-1] ^= 0xFF
        with self.assertRaises(FrameError):
            decode_frame(bytes(frame))

    def test_body_length_lie_rejected(self):
        frame = bytearray(encode_frame(MAGIC_REQUEST, cmd=8, msg_id=1, body="{}"))
        # claim a much longer body than actually present
        frame[5] = 0xFF
        frame[6] = 0xFF
        with self.assertRaises(FrameError):
            decode_frame(bytes(frame))

    def test_crc_is_deterministic(self):
        self.assertEqual(crc16_ccitt_false(b"hello"), crc16_ccitt_false(b"hello"))
        self.assertNotEqual(crc16_ccitt_false(b"hello"), crc16_ccitt_false(b"hellp"))

    def test_split_for_mtu(self):
        data = bytes(range(20))
        chunks = list(split_for_mtu(data, 7))
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(len(c) <= 7 for c in chunks))

    def test_split_for_mtu_rejects_non_positive(self):
        with self.assertRaises(FrameError):
            list(split_for_mtu(b"abc", 0))


if __name__ == "__main__":
    unittest.main()
