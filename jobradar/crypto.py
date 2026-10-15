"""AES-256-GCM encryption for everything JobRadar stores in a public GitHub repository.

Format: b"JR1" + 12-byte nonce + ciphertext+tag (AAD = b"JR1"). The browser decrypts the same bytes with WebCrypto.
The nonce is derived from HMAC(key, plaintext), so identical files encrypt identically (git stores them once) and a
nonce is never reused for different data. The key (32 random bytes, base64url) lives only in the JOBRADAR_KEY
secret and in your private dashboard link – never in the repository.
Needs the `cryptography` package (installed by the cloud workflow).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

MAGIC = b"JR1"


def new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def parse_key(k: str | None = None) -> bytes:
    k = (k if k is not None else os.environ.get("JOBRADAR_KEY", "")).strip()
    if not k:
        raise RuntimeError("JOBRADAR_KEY is not set")
    raw = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))
    if len(raw) != 32:
        raise RuntimeError("JOBRADAR_KEY must be 32 bytes (base64url) – create one with cloud_setup.bat")
    return raw


def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = hmac.new(key, data, hashlib.sha256).digest()[:12]
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] != MAGIC:
        raise ValueError("not a JobRadar encrypted file")
    return AESGCM(key).decrypt(blob[3:15], blob[15:], MAGIC)


def token(name: str, key: bytes) -> str:
    """Opaque file name for a published file (so names like '<you>_<company>_<role>.pdf' never appear in public)."""
    return hmac.new(key, name.encode(), hashlib.sha256).hexdigest()[:24]
