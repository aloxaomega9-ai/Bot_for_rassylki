"""Encryption helpers for Telegram session credentials.

Set SESSION_ENCRYPTION_KEY to a Fernet key stored as a secret in the hosting
provider. Never commit the key or log plaintext sessions.
"""
import os

from cryptography.fernet import Fernet, InvalidToken


class SessionEncryptionError(RuntimeError):
    """Raised when the session encryption key is missing or a token is invalid."""


def _fernet() -> Fernet:
    key = os.getenv("SESSION_ENCRYPTION_KEY")
    if not key:
        raise SessionEncryptionError("SESSION_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SessionEncryptionError("SESSION_ENCRYPTION_KEY is invalid") from exc


def encrypt_session(session_string: str) -> str:
    if not session_string:
        raise ValueError("session_string must not be empty")
    return _fernet().encrypt(session_string.encode("utf-8")).decode("ascii")


def decrypt_session(encrypted_session: str) -> str:
    try:
        return _fernet().decrypt(encrypted_session.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise SessionEncryptionError("Could not decrypt the stored session") from exc
