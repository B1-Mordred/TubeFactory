from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from youtuber_api.config import get_settings


def encrypt_identity_secret(value: str) -> bytes:
    return Fernet(get_settings().identity_encryption_key.encode()).encrypt(value.encode())


def decrypt_identity_secret(value: bytes) -> str:
    try:
        return Fernet(get_settings().identity_encryption_key.encode()).decrypt(value).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("identity secret cannot be decrypted with the configured key") from exc


def stable_secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
