"""Database-level tests for tenant isolation and explicit chat authorization."""
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from multiuser.models import Base, BroadcastJobTarget, ConnectedAccount, User
from multiuser.repository import (
    AlreadyConnected,
    AuthorizationRequired,
    NotFound,
    RepositoryError,
    add_account,
    accept_privacy_notice,
    delete_user_data,
    add_chat,
    create_job,
    disconnect_account,
    get_owned_account,
    register_user,
    set_chat_authorization,
)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        async with db.begin():
            yield db

    await engine.dispose()


@pytest.mark.asyncio
async def test_records_are_scoped_to_their_owner(session):
    await register_user(session, 101)
    await register_user(session, 202)
    account = await add_account(session, 101, "encrypted-session-a", "primary")

    with pytest.raises(NotFound):
        await get_owned_account(session, 202, account.id)


@pytest.mark.asyncio
async def test_account_limit_and_explicit_chat_authorization(session):
    await register_user(session, 303)
    account = await add_account(session, 303, "encrypted-session-b")

    with pytest.raises(AlreadyConnected):
        await add_account(session, 303, "another-encrypted-session")

    chat = await add_chat(session, 303, account.id, -100123, "Opt-in community")

    with pytest.raises(AuthorizationRequired):
        await create_job(session, 303, account.id, 9001, [chat.chat_id])

    with pytest.raises(AuthorizationRequired):
        await set_chat_authorization(
            session, 303, account.id, chat.chat_id, True
        )

    await set_chat_authorization(
        session,
        303,
        account.id,
        chat.chat_id,
        True,
        explicitly_confirmed=True,
    )
    job = await create_job(session, 303, account.id, 9001, [chat.chat_id])
    targets = await session.scalars(
        select(BroadcastJobTarget).where(BroadcastJobTarget.job_id == job.id)
    )
    assert len(list(targets)) == 1


@pytest.mark.asyncio
async def test_disconnect_revokes_stored_credential(session):
    await register_user(session, 404)
    account = await add_account(session, 404, "encrypted-session-c")
    await disconnect_account(session, 404, account.id)

    revoked = await get_owned_account(session, 404, account.id, active_only=False)
    assert revoked.is_active is False
    assert revoked.encrypted_session is None


@pytest.mark.asyncio
async def test_job_allows_up_to_50_explicitly_authorized_chats(session):
    await register_user(session, 505)
    account = await add_account(session, 505, "encrypted-session-d")
    chat_ids = []
    for chat_id in range(-200000, -199950):
        chat = await add_chat(session, 505, account.id, chat_id, "Opt-in chat")
        await set_chat_authorization(
            session, 505, account.id, chat_id, True, explicitly_confirmed=True
        )
        chat_ids.append(chat.chat_id)

    job = await create_job(session, 505, account.id, 9002, chat_ids)
    targets = await session.scalars(
        select(BroadcastJobTarget).where(BroadcastJobTarget.job_id == job.id)
    )
    assert len(list(targets)) == 50


@pytest.mark.asyncio
async def test_job_rejects_more_than_50_chats(session):
    await register_user(session, 606)
    account = await add_account(session, 606, "encrypted-session-e")
    chat_ids = list(range(-300000, -299949))

    with pytest.raises(RepositoryError, match="50 chats"):
        await create_job(session, 606, account.id, 9003, chat_ids)


@pytest.mark.asyncio
async def test_privacy_notice_acceptance_is_recorded(session):
    await register_user(session, 707)
    user = await accept_privacy_notice(session, 707, "2026-11-17")
    assert user.terms_accepted_at is not None
    assert user.privacy_notice_version == "2026-11-17"


@pytest.mark.asyncio
async def test_user_deletion_removes_owned_service_records(session):
    await register_user(session, 808)
    account = await add_account(session, 808, "encrypted-session-f")
    chat = await add_chat(session, 808, account.id, -400123, "Opt-in group")
    await set_chat_authorization(
        session, 808, account.id, chat.chat_id, True, explicitly_confirmed=True
    )
    job = await create_job(session, 808, account.id, 9004, [chat.chat_id])

    await delete_user_data(session, 808)

    assert await session.get(User, 808) is None
    assert await session.get(ConnectedAccount, account.id) is None
    remaining_targets = await session.scalars(
        select(BroadcastJobTarget).where(BroadcastJobTarget.job_id == job.id)\n    )\n    assert list(remaining_targets) == []
