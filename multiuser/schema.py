"""Explicit one-time schema creation for a fresh database.

This is a bootstrap helper, not a schema migration system. Run only against a
new/empty database after reviewing the models. Future schema changes should use
Alembic migrations rather than relying on create_all to alter existing tables.
"""
import asyncio
import os

from multiuser.db import dispose_engine, get_engine
from multiuser.models import Base


async def create_schema() -> None:
    if os.getenv("ALLOW_SCHEMA_CREATE") != "1":
        raise RuntimeError("Set ALLOW_SCHEMA_CREATE=1 to confirm schema creation")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def main() -> None:
    try:
        await create_schema()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
