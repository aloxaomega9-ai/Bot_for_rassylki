"""Telegram client helpers for phone-code login and isolated user clients."""
import os

from pyrogram import Client


def _api_credentials() -> tuple[int, str]:
    try:
        api_id = int(os.environ["API_ID"])
        api_hash = os.environ["API_HASH"]
    except (KeyError, ValueError) as exc:
        raise RuntimeError("API_ID and API_HASH must be configured") from exc
    if not api_hash:
        raise RuntimeError("API_HASH must be configured")
    return api_id, api_hash


def new_login_client(owner_id: int) -> Client:
    """Create a temporary in-memory client for interactive phone-code login."""
    api_id, api_hash = _api_credentials()
    return Client(
        name=f"multiuser-login-{owner_id}",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
        no_updates=True,
    )


def new_user_client(session_string: str, owner_id: int) -> Client:
    api_id, api_hash = _api_credentials()
    return Client(
        name=f"multiuser-account-{owner_id}",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
