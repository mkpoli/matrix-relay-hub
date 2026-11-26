"""AES-256-CBC media decryption for WeCom AI Bot callbacks.

Matches WecomTeam/aibot-node-sdk (src/crypto.ts):
- key = base64_decode(aeskey) (32 bytes; trailing '=' padding added if omitted)
- iv = key[:16]
- PKCS#7 padding to a 32-byte block (pad length 1..32)
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PKCS7_BLOCK = 32


class DecryptError(ValueError):
    """Raised when WeCom media bytes or key cannot be decrypted."""


def _decode_aes_key(aes_key: str) -> bytes:
    if not isinstance(aes_key, str) or not aes_key.strip():
        raise DecryptError("aeskey must be a non-empty string")
    cleaned = aes_key.strip()
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        key = base64.b64decode(padded, validate=True)
    except Exception as exc:
        raise DecryptError(f"invalid base64 aeskey: {exc}") from exc
    if len(key) != 32:
        raise DecryptError(f"decoded aeskey must be 32 bytes, got {len(key)}")
    return key


def decrypt_file(encrypted: bytes, aes_key: str) -> bytes:
    """Decrypt a WeCom AI Bot encrypted media blob using AES-256-CBC + 32-byte PKCS#7."""
    if not encrypted:
        raise DecryptError("encrypted payload is empty")
    if len(encrypted) % 16 != 0:
        raise DecryptError("encrypted payload length must be a multiple of 16 bytes")
    key = _decode_aes_key(aes_key)
    iv = key[:16]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    pad_len = decrypted[-1]
    if pad_len < 1 or pad_len > PKCS7_BLOCK or pad_len > len(decrypted):
        raise DecryptError(f"invalid PKCS#7 padding length: {pad_len}")
    if any(byte != pad_len for byte in decrypted[-pad_len:]):
        raise DecryptError("invalid PKCS#7 padding bytes")
    return decrypted[:-pad_len]

