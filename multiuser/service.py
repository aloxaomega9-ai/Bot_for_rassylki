"""Application service layer for the multi-user bot.

Call account connection methods only after a separate, secure Telegram
authorization flow has completed. Never pass login codes or cloud passwords
through this service or log plaintext session strings.
"""
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from multiuser.db import get_session_factory
from multiuser.repository import (
    add_account,
    add_chat,
    accept_privacy_notice,
    delete_user_data,
    create_job,
    disconnect_account,
    get_owned_account,
    register_user,
    set_chat_authorization,
    set_job_status,
)
from multiuser.security import decrypt_session, encrypt_session


class MultiUserService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def register(self, telegram_user_id: int) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                user = await register_user(session, telegram_user_id)
                return user.telegram_user_id

    async def connect_account(
        self,
        telegram_user_id: int,
        session_string: str,
        label: str | None = None,
    ) -> int:
        """Encrypt and save an already-authorized Telegram session."""
        encrypted = encrypt_session(session_string)
        async with self._session_factory() as session:
            async with session.begin():
                await register_user(session, telegram_user_id)
                account = await add_account(session, telegram_user_id, encrypted, label)
                return account.id

    async def get_account_session(self, telegram_user_id: int, account_id: int) -> str:
        """Return a decrypted session only to trusted worker code, never to a chat handler."""
        async with self._session_factory() as session:
            account = await get_owned_account(session, telegram_user_id, account_id)
            if not account.encrypted_session:
                raise RuntimeError("account session has been revoked")
            return decrypt_session(account.encrypted_session)

    async def disconnect(self, telegram_user_id: int, account_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await disconnect_account(session, telegram_user_id, account_id)

    async def register_chat(
        self,
        telegram_user_id: int,
        account_id: int,
        chat_id: int,
        title: str | None = None,
    ) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                chat = await add_chat(session, telegram_user_id, account_id, chat_id, title)
                return chat.id

    async def authorize_chat(
        self,
        telegram_user_id: int,
        account_id: int,
        chat_id: int,
        *,
        explicitly_confirmed: bool,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await set_chat_authorization(
                    session,
                    telegram_user_id,
                    account_id,
                    chat_id,
                    True,
                    explicitly_confirmed=explicitly_confirmed,
                )

    async def revoke_chat(
        self, telegram_user_id: int, account_id: int, chat_id: int
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await set_chat_authorization(
                    session, telegram_user_id, account_id, chat_id, False
                )

    async def create_broadcast_job(
        self,
        telegram_user_id: int,
        account_id: int,
        source_message_id: int,
        target_chat_ids: list[int],
    ) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                job = await create_job(
                    session,
                    telegram_user_id,
                    account_id,
                    source_message_id,
                    target_chat_ids,
                )
                return job.id

    async def change_job_status(
        self, telegram_user_id: int, job_id: int, status: str
    ) -> str:
        async with self._session_factory() as session:
            async with session.begin():
                job = await set_job_status(session, telegram_user_id, job_id, status)
                return job.status


    async def accept_notice(self, telegram_user_id: int, version: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await accept_privacy_notice(session, telegram_user_id, version)

    async def delete_user(self, telegram_user_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await delete_user_data(session, telegram_user_id)
