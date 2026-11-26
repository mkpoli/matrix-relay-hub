"""Shared test helpers for the WeCom bridge test suite."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wecom_bridge.crypto import PKCS7_BLOCK


def encrypt_file(plaintext: bytes, aes_key: str) -> bytes:
    """Encrypt bytes using WeCom's AES-256-CBC + 32-byte PKCS#7 scheme."""
    cleaned = aes_key.strip()
    key = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))
    iv = key[:16]
    pad_len = PKCS7_BLOCK - (len(plaintext) % PKCS7_BLOCK)
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()
