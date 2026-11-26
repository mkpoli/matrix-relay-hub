import base64

import pytest

from helpers import encrypt_file
from wecom_bridge.crypto import DecryptError, decrypt_file


def _make_key(raw: bytes = bytes(range(32)), *, strip_pad: bool = True) -> str:
    encoded = base64.b64encode(raw).decode("ascii")
    return encoded.rstrip("=") if strip_pad else encoded


def test_round_trip_various_lengths_including_32_byte_multiple():
    key_43 = _make_key(strip_pad=True)
    key_44 = _make_key(strip_pad=False)
    for length in (0, 1, 15, 16, 17, 31, 32, 33, 64, 1025):
        payload = bytes((i * 37) % 256 for i in range(length))
        ciphertext = encrypt_file(payload, key_43)
        assert len(ciphertext) % 32 == 0
        assert decrypt_file(ciphertext, key_43) == payload
        assert decrypt_file(ciphertext, key_44) == payload


def test_rejects_bad_key_or_corrupt_padding():
    key = _make_key()
    other_key = _make_key(bytes(reversed(range(32))))
    ciphertext = encrypt_file(b"hello wecom", key)

    with pytest.raises(DecryptError):
        decrypt_file(ciphertext, other_key)
    with pytest.raises(DecryptError):
        decrypt_file(b"", key)
    with pytest.raises(DecryptError):
        decrypt_file(b"short", key)
    with pytest.raises(DecryptError):
        decrypt_file(ciphertext, "not-a-valid-32-byte-key")
