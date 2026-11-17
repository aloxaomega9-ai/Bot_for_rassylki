"""Owner-scoped database operations for the multi-user app.

Functions flush changes but never commit. Call them inside:
    async with session_scope() as session:
        async with session.begin():
            ...

Every lookup that touches a tenant-owned row includes the Telegram owner ID.
"""
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from multiuser.models import (
    AuthorizedChat,
    BroadcastJob,
    BroadcastJobTarget,
    ConnectedAccount,
    User,
)


class RepositoryError(RuntimeError):
    """Base class for expected repository failures."""


class NotFound(RepositoryError):
    """The requested resource does not exist for this user."""


class AlreadyConnected(RepositoryError):
    """The first-release limit of one active account per user was reached."""


class AuthorizationRequired(RepositoryError):
    """The selected chat has not been explicitly authorized."""


class ActiveJobLimit(RepositoryError):
    """This user already has an active job."""


MAX_TARGETS_PER_JOB = 20

_ALLOWED_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"paused", "completed", "cancelled", "failed"},
    "paused": {"running", "cancelled"},
    "completed": set(),
    "cancelled": set(),
    "failed": set(),
}


async def register_user(session: AsyncSession, telegram_user_id: int) -> User:
    if telegram_user_id <= 0:
        raise ValueError("telegram_user_id must be a positive integer")
    user = await session.get(User, telegram_user_id)
    if user is not None:
        return user

    # The savepoint handles two /start updates arriving at almost the same time.
    try:
        async with session.begin_nested():
            user = User(telegram_user_id=telegram_user_id, is_active=True)
            session.add(user)
            await session.flush()
    except IntegrityError:
        user = await session.get(User, telegram_user_id)
        if user is None:
            raise RepositoryError("could not register user")
    return user


async def list_accounts(session: AsyncSession, telegram_user_id: int) -> list[ConnectedAccount]:
    result = await session.scalars(
        select(ConnectedAccount)
        .where(ConnectedAccount.telegram_user_id == telegram_user_id)
        .order_by(ConnectedAccount.id)
    )
    return list(result)


async def add_account(
    session: AsyncSession,
    telegram_user_id: int,
    encrypted_session: str,
    label: str | None = None,
) -> ConnectedAccount:
    """Attach one encrypted account. A row lock serializes concurrent first-account attempts."""
    if not encrypted_session:
        raise ValueError("encrypted_session must not be empty")
    user_result = await session.scalars(
        select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
    )
    user = user_result.one_or_none()
    if user is None or not user.is_active:
        raise NotFound("active user not found")

    active_account = await session.scalar(
        select(ConnectedAccount.id).where(
            ConnectedAccount.telegram_user_id == telegram_user_id,
            ConnectedAccount.is_active.is_(True),
        ).limit(1)
    )
    if active_account is not None:
        raise AlreadyConnected("one active account per user is supported in this release")

    account = ConnectedAccount(
        telegram_user_id=telegram_user_id,
        encrypted_session=encrypted_session,
        label=(label or "")[:128] or None,
        is_active=True,
    )
    session.add(account)
    await session.flush()
    return account


async def get_owned_account(
    session: AsyncSession, telegram_user_id: int, account_id: int, active_only: bool = True
) -> ConnectedAccount:
    conditions = [
        ConnectedAccount.id == account_id,
        ConnectedAccount.telegram_user_id == telegram_user_id,
    ]
    if active_only:
        conditions.append(ConnectedAccount.is_active.is_(True))
    account = await session.scalar(select(ConnectedAccount).where(*conditions))
    if account is None:
        raise NotFound("account not found")
    return account


async def disconnect_account(
    session: AsyncSession, telegram_user_id: int, account_id: int
) -> None:
    account = await get_owned_account(session, telegram_user_id, account_id)
    account.is_active = False
    account.encrypted_session = None
    account.revoked_at = datetime.now(timezone.utc)

    jobs = await session.scalars(
        select(BroadcastJob).where(
            BroadcastJob.telegram_user_id == telegram_user_id,
            BroadcastJob.account_id == account_id,
            BroadcastJob.status.in_({"pending", "running", "paused"}),
        )
    )
    now = datetime.now(timezone.utc)
    for job in jobs:
        job.status = "cancelled"
        job.finished_at = now
    await session.flush()


async def add_chat(
    session: AsyncSession,
    telegram_user_id: int,
    account_id: int,
    chat_id: int,
    title: str | None = None,
) -> AuthorizedChat:
    """Register a possible destination as unauthorized by default."""
    await get_owned_account(session, telegram_user_id, account_id)
    chat = await session.scalar(
        select(AuthorizedChat).where(
            AuthorizedChat.account_id == account_id,
            AuthorizedChat.chat_id == chat_id,
        )
    )
    if chat is None:
        chat = AuthorizedChat(
            account_id=account_id,
            chat_id=chat_id,
            title=(title or "")[:255] or None,
            is_authorized=False,
        )
        session.add(chat)
    else:
        chat.title = (title or "")[:255] or chat.title
        # Re-discovery never silently re-authorizes a previously revoked chat.
    await session.flush()
    return chat


async def set_chat_authorization(
    session: AsyncSession,
    telegram_user_id: int,
    account_id: int,
    chat_id: int,
    authorized: bool,
    *,
    explicitly_confirmed: bool = False,
) -> AuthorizedChat:
    """Change consent state; enabling requires an explicit confirmation from the owner."""
    await get_owned_account(session, telegram_user_id, account_id)
    chat = await session.scalar(
        select(AuthorizedChat).where(
            AuthorizedChat.account_id == account_id,
            AuthorizedChat.chat_id == chat_id,
        )
    )
    if chat is None:
        raise NotFound("chat not found")
    if authorized and not explicitly_confirmed:
        raise AuthorizationRequired("explicit owner confirmation is required")
    chat.is_authorized = authorized
    if authorized:
        chat.authorized_at = datetime.now(timezone.utc)
        chat.revoked_at = None
    else:
        chat.revoked_at = datetime.now(timezone.utc)
    await session.flush()
    return chat


async def list_authorized_chats(
    session: AsyncSession, telegram_user_id: int, account_id: int
) -> list[AuthorizedChat]:
    await get_owned_account(session, telegram_user_id, account_id)
    result = await session.scalars(
        select(AuthorizedChat)
        .where(AuthorizedChat.account_id == account_id, AuthorizedChat.is_authorized.is_(True))
        .order_by(AuthorizedChat.id)
    )
    return list(result)


async def create_job(
    session: AsyncSession,
    telegram_user_id: int,
    account_id: int,
    source_message_id: int,
    target_chat_ids: Sequence[int],
) -> BroadcastJob:
    """Create a job only for explicitly authorized chats owned by this account."""
    # Locking the user row serializes concurrent job creation for this tenant.
    user = await session.scalar(
        select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
    )
    if user is None or not user.is_active:
        raise NotFound("active user not found")
    await get_owned_account(session, telegram_user_id, account_id)

    unique_ids = list(dict.fromkeys(target_chat_ids))
    if not unique_ids:
        raise AuthorizationRequired("at least one explicitly authorized chat is required")
    if len(unique_ids) > MAX_TARGETS_PER_JOB:
        raise RepositoryError(f"job target limit is {MAX_TARGETS_PER_JOB} chats")

    active_job_id = await session.scalar(
        select(BroadcastJob.id).where(
            BroadcastJob.telegram_user_id == telegram_user_id,
            BroadcastJob.status.in_({"pending", "running", "paused"}),
        ).limit(1)
    )
    if active_job_id is not None:
        raise ActiveJobLimit("one active job per user is supported in this release")

    targets = await session.scalars(
        select(AuthorizedChat).where(
            AuthorizedChat.account_id == account_id,
            AuthorizedChat.chat_id.in_(unique_ids),
            AuthorizedChat.is_authorized.is_(True),
        )
    )
    target_rows = list(targets)
    if {row.chat_id for row in target_rows} != set(unique_ids):
        raise AuthorizationRequired("one or more selected chats are not authorized for this account")

    job = BroadcastJob(
        telegram_user_id=telegram_user_id,
        account_id=account_id,
        source_message_id=source_message_id,
        status="pending",
    )
    session.add(job)
    await session.flush()
    for chat in target_rows:
        session.add(BroadcastJobTarget(job_id=job.id, authorized_chat_id=chat.id, status="pending"))
    await session.flush()
    return job


async def set_job_status(
    session: AsyncSession, telegram_user_id: int, job_id: int, new_status: str
) -> BroadcastJob:
    if new_status not in _ALLOWED_TRANSITIONS:
        raise ValueError("unknown job status")
    job = await session.scalar(
        select(BroadcastJob).where(
            BroadcastJob.id == job_id,
            BroadcastJob.telegram_user_id == telegram_user_id,
        ).with_for_update()
    )
    if job is None:
        raise NotFound("job not found")
    if new_status not in _ALLOWED_TRANSITIONS[job.status]:
        raise RepositoryError(f"invalid job transition: {job.status} -> {new_status}")
    job.status = new_status
    if new_status in {"completed", "cancelled", "failed"}:
        job.finished_at = datetime.now(timezone.utc)
    await session.flush()
    return job


async def list_jobs(session: AsyncSession, telegram_user_id: int) -> list[BroadcastJob]:
    result = await session.scalars(
        select(BroadcastJob)
        .where(BroadcastJob.telegram_user_id == telegram_user_id)
        .order_by(BroadcastJob.created_at.desc(), BroadcastJob.id.desc())
    )
    return list(result)
