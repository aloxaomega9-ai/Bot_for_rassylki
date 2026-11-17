"""Public multi-user bot entrypoint.

The previous single-admin implementation is preserved in legacy_main.py.
This entrypoint runs the owner-isolated public bot using the same BOT_TOKEN.
"""
import asyncio

from multiuser_bot import main


if __name__ == "__main__":
    asyncio.run(main())
