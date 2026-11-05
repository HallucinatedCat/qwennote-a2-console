"""AES helper for the device handshake (AES-128-CBC, NoPadding).

Confidence level: reconstructed by analogy, not verified against a real
device. Treat it as a strong hypothesis to test against one real captured
handshake, not as a confirmed fact.

Recipe used here:

- AES-128-CBC with NoPadding, key = IV = the 16 raw bytes you get by
  hex-decoding ``device_secret`` (a 32-hex-character string). This follows the
  one AES routine of the official app whose key/IV/padding recipe is plainly
  visible, applied to the device handshake.
- Consequently this module assumes ``device_secret`` is a 32-hex-character
  string (16 bytes). If the real value does not hex-decode to exactly 16
  bytes, construction raises instead of silently guessing.
- Plaintext must already be a multiple of 16 bytes for NoPadding; the GET
  RANDOM reply is expected to already be a fixed-length nonce. If it is not,
  this encrypts nothing and raises rather than inventing a padding scheme
  the device was never shown to use.

If your device capture shows different ciphertext than this module produces
for the same key/random, that confirms the guess is wrong, not that the
scheme used here is impossible to get right -- adjust ``key_iv_from_secret``
and the mode below once you know better from a real capture.
"""

from __future__ import annotations

import binascii

try:
    from Crypto.Cipher import AES as _AES
except ImportError:  # pragma: no cover - exercised when pycryptodome is absent
    _AES = None


class AesError(ValueError):
    pass


def _require_backend():
    if _AES is None:
        raise AesError("pycryptodome is required for AES; pip install -r requirements.txt")


def key_iv_from_secret(device_secret: str) -> bytes:
    """Hex-decode device_secret into the 16 raw bytes used as both key and IV."""
    try:
        raw = binascii.unhexlify(device_secret.strip())
    except (binascii.Error, ValueError) as exc:
        raise AesError("device_secret must be a hex string: %s" % exc) from exc
    if len(raw) != 16:
        raise AesError("device_secret must hex-decode to 16 bytes (AES-128), got %d" % len(raw))
    return raw


def aes_encrypt(device_secret: str, text: str) -> str:
    """AES-128-CBC encrypt text with the device secret -> hex string."""
    _require_backend()
    key = key_iv_from_secret(device_secret)
    data = text.encode("utf-8")
    if len(data) % 16:
        raise AesError(
            "plaintext length %d is not a multiple of 16 bytes (NoPadding); "
            "got %r" % (len(data), text))
    cipher = _AES.new(key, _AES.MODE_CBC, iv=key)
    return binascii.hexlify(cipher.encrypt(data)).decode("ascii")


def aes_decrypt(device_secret: str, hex_text: str) -> str:
    """AES-128-CBC decrypt a hex string with the device secret -> utf-8 string."""
    _require_backend()
    key = key_iv_from_secret(device_secret)
    try:
        data = binascii.unhexlify(hex_text.strip())
    except (binascii.Error, ValueError) as exc:
        raise AesError("ciphertext must be a hex string: %s" % exc) from exc
    if len(data) % 16:
        raise AesError("ciphertext length %d is not a multiple of 16 bytes" % len(data))
    cipher = _AES.new(key, _AES.MODE_CBC, iv=key)
    plain = cipher.decrypt(data)
    return plain.decode("utf-8", errors="strict")
