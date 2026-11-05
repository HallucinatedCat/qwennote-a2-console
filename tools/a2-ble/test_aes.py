import unittest

try:
    import Crypto  # noqa: F401
    HAVE_PYCRYPTODOME = True
except ImportError:
    HAVE_PYCRYPTODOME = False

from a2ble.aes import AesError, aes_decrypt, aes_encrypt, key_iv_from_secret

SYNTHETIC_SECRET = "00112233445566778899aabbccddeeff"[:32]  # 32 hex chars = 16 bytes
SYNTHETIC_RANDOM_16 = "ABCDEF0123456789"  # exactly 16 bytes, no padding needed


@unittest.skipUnless(HAVE_PYCRYPTODOME, "pycryptodome not installed")
class AesTests(unittest.TestCase):
    def test_key_iv_from_secret_length(self):
        key = key_iv_from_secret(SYNTHETIC_SECRET)
        self.assertEqual(len(key), 16)

    def test_key_iv_rejects_non_hex(self):
        with self.assertRaises(AesError):
            key_iv_from_secret("not-hex-at-all!!")

    def test_key_iv_rejects_wrong_length(self):
        with self.assertRaises(AesError):
            key_iv_from_secret("aa")  # decodes to 1 byte, not 16

    def test_encrypt_decrypt_round_trip(self):
        cipher_hex = aes_encrypt(SYNTHETIC_SECRET, SYNTHETIC_RANDOM_16)
        recovered = aes_decrypt(SYNTHETIC_SECRET, cipher_hex)
        self.assertEqual(recovered, SYNTHETIC_RANDOM_16)

    def test_encrypt_is_deterministic_for_same_inputs(self):
        # AES-CBC with a fixed IV (this module's key==iv hypothesis) must be
        # deterministic for the same key/plaintext -- a sanity check on the
        # implementation, not a claim about the real device's output.
        a = aes_encrypt(SYNTHETIC_SECRET, SYNTHETIC_RANDOM_16)
        b = aes_encrypt(SYNTHETIC_SECRET, SYNTHETIC_RANDOM_16)
        self.assertEqual(a, b)

    def test_encrypt_rejects_non_block_aligned_plaintext(self):
        with self.assertRaises(AesError):
            aes_encrypt(SYNTHETIC_SECRET, "not16bytes")

    def test_decrypt_rejects_non_hex_ciphertext(self):
        with self.assertRaises(AesError):
            aes_decrypt(SYNTHETIC_SECRET, "zz-not-hex")

    def test_different_secret_produces_different_ciphertext(self):
        other_secret = "ffeeddccbbaa99887766554433221100"[:32]
        a = aes_encrypt(SYNTHETIC_SECRET, SYNTHETIC_RANDOM_16)
        b = aes_encrypt(other_secret, SYNTHETIC_RANDOM_16)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
