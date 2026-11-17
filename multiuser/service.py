"""Application service layer for the multi-user bot."""
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from multiuser.db import get_session_factory
from multiuser.repository import (
    add_account, add_chat, accept_privacy_notice, delete_user_data,
    create_job, disconnect_account, get_owned_account, register_user,
    set_chat_authorization, set_job_status, list_accounts,
    list_authorized_chats,
)
from multiuser.security import decrypt_session, encrypt_session
from multiuser.models import UserSettings


class MultiUserService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None):
        self._session_factory = session_factory or get_session_factory()

    async def register(self, telegram_user_id: int) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                user = await register_user(session, telegram_user_id)
                return user.telegram_user_id

    async def connect_account(self, telegram_user_id: int, session_string: str, label: str | None = None) -> int:
        encrypted = encrypt_session(session_string)
        async with self._session_factory() as session:
            async with session.begin():
                await register_user(session, telegram_user_id)
                account = await add_account(session, telegram_user_id, encrypted, label)
                return account.id

    async def accounts(self, telegram_user_id: int):
        async with self._session_factory() as session:
            return await list_accounts(session, telegram_user_id)

    async def get_account_session(self, telegram_user_id: int, account_id: int) -> str:
        async with self._session_factory() as session:
            account = await get_owned_account(session, telegram_user_id, account_id)
            if not account.encrypted_session:
                raise RuntimeError("account session has been revoked")
            return decrypt_session(account.encrypted_session)

    async def disconnect(self, telegram_user_id: int, account_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await disconnect_account(session, telegram_user_id, account_id)

    async def register_chat(self, telegram_user_id: int, account_id: int, chat_id: int, title: str | None = None) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                chat = await add_chat(session, telegram_user_id, account_id, chat_id, title)
                return chat.id

    async def authorize_chat(self, telegram_user_id: int, account_id: int, chat_id: int, *, explicitly_confirmed: bool) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await set_chat_authorization(session, telegram_user_id, account_id, chat_id, True, explicitly_confirmed=explicitly_confirmed)

    async def revoke_chat(self, telegram_user_id: int, account_id: int, chat_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await set_chat_authorization(session, telegram_user_id, account_id, chat_id, False)

    async def authorized_chats(self, telegram_user_id: int, account_id: int):
        async with self._session_factory() as session:
            return await list_authorized_chats(session, telegram_user_id, account_id)

    async def chat_is_authorized(self, telegram_user_id: int, account_id: int, chat_id: int) -> bool:
        async with self._session_factory() as session:
            from sqlalchemy import select
            from multiuser.models import AuthorizedChat
            await get_owned_account(session, telegram_user_id, account_id)
            row = await session.scalar(select(AuthorizedChat.id).where(
                AuthorizedChat.account_id == account_id,
                AuthorizedChat.chat_id == chat_id,
                AuthorizedChat.is_authorized.is_(True),
            ))
            return row is not None

    async def create_broadcast_job(self, telegram_user_id: int, account_id: int, source_message_id: int, target_chat_ids: list[int]) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                job = await create_job(session, telegram_user_id, account_id, source_message_id, target_chat_ids)
                return job.id

    async def change_job_status(self, telegram_user_id: int, job_id: int, status: str) -> str:
        async with self._session_factory() as session:
            async with session.begin():
                job = await set_job_status(session, telegram_user_id, job_id, status)
                return job.status

    async def accept_notice(self, telegram_user_id: int, version: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await accept_privacy_notice(session, telegram_user_id, version)

    async def get_interval(self, telegram_user_id: int) -> int:
        async with self._session_factory() as session:
            settings = await session.get(UserSettings, telegram_user_id)
            return settings.interval_seconds if settings else 60

    async def set_interval(self, telegram_user_id: int, seconds: int) -> None:
        if seconds not in {30, 60, 120, 300}:
            raise ValueError("unsupported broadcast interval")
        async with self._session_factory() as session:
            async with session.begin():
                settings = await session.get(UserSettings, telegram_user_id)
                if settings is None:
                    settings = UserSettings(telegram_user_id=telegram_user_id, interval_seconds=seconds)
                    session.add(settings)
                else:
                    settings.interval_seconds = seconds

    async def delete_user(self, telegram_user_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await delete_user_data(session, telegram_user_id)
