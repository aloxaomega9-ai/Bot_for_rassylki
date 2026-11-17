"""Multi-user Telegram broadcast bot.

Account authorization uses Telegram's QR-device approval flow. The bot never asks
for login codes or cloud passwords. Sending is limited to destinations that the
owner explicitly authorizes and is capped at 50 chats per confirmed job.
"""
import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram.errors import FloodWait
from sqlalchemy import select

from multiuser.db import dispose_engine, get_session_factory
from multiuser.models import AuthorizedChat, User
from multiuser.service import MultiUserService
from multiuser.telegram import begin_qr_login, new_login_client, new_user_client


NOTICE_VERSION = "2026-11-17"
NOTICE = (
    "<b>Перед началом</b>\n\n"
    "Сервис хранит ваш Telegram ID и зашифрованную сессию подключённого аккаунта. "
    "Сервер сможет использовать эту сессию, пока аккаунт подключён. Шифрование "
    "защищает данные в базе, но не скрывает их от работающего приложения.\n\n"
    "Подключение подтверждается QR-кодом в официальном приложении Telegram. "
    "Бот не просит коды входа или облачный пароль. Вы можете отключить аккаунт "
    "командой /disconnect.\n\n"
    "Отправка разрешена только в группы, где у вас есть разрешение и участники "
    "ожидают сообщения. Не используйте сервис для нежелательных рассылок. "
    "Одна задача — не более 50 групп; при FloodWait отправка остановится.\n\n"
    "Нажимая «Принимаю», вы подтверждаете, что прочитали это уведомление."
)

pending_logins: dict[int, tuple[object, asyncio.Task]] = {}
pending_sends: dict[int, tuple[int, list[int], str]] = {}


def notice_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Принимаю", callback_data="notice:accept")
    keyboard.button(text="❌ Не принимаю", callback_data="notice:decline")
    keyboard.adjust(1)
    return keyboard.as_markup()


def confirm_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Подтвердить отправку", callback_data="send:confirm")
    keyboard.button(text="Отмена", callback_data="send:cancel")
    keyboard.adjust(1)
    return keyboard.as_markup()


async def accepted(service: MultiUserService, user_id: int) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        user = await session.get(User, user_id)
        return bool(user and user.is_active and user.terms_accepted_at)


async def get_chats(user_id: int, account_id: int):
    factory = get_session_factory()
    async with factory() as session:
        result = await session.scalars(
            select(AuthorizedChat)
            .join(__import__("multiuser.models", fromlist=["ConnectedAccount"]).ConnectedAccount)
            .where(
                AuthorizedChat.account_id == account_id,
                __import__("multiuser.models", fromlist=["ConnectedAccount"]).ConnectedAccount.telegram_user_id == user_id,
            )
            .order_by(AuthorizedChat.title)
        )
        return list(result)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    service = MultiUserService()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        await service.register(message.from_user.id)
        await message.answer(NOTICE, reply_markup=notice_keyboard())

    @dp.callback_query(F.data == "notice:accept")
    async def accept_notice(callback: CallbackQuery) -> None:
        await service.accept_notice(callback.from_user.id, NOTICE_VERSION)
        await callback.message.edit_text(
            "✅ Уведомление принято.\n\n"
            "/connect — подключить аккаунт по QR\n"
            "/chats — выбрать разрешённые группы\n"
            "/send текст — подготовить отправку\n"
            "/status — статус; /disconnect — отключить аккаунт"
        )
        await callback.answer("Сохранено")

    @dp.callback_query(F.data == "notice:decline")
    async def decline_notice(callback: CallbackQuery) -> None:
        await callback.message.edit_text("Уведомление не принято. Функции подключения и отправки недоступны.")
        await callback.answer()

    @dp.message(Command("connect"))
    async def connect(message: Message) -> None:
        uid = message.from_user.id
        if not await accepted(service, uid):
            return await message.answer("Сначала отправьте /start и примите уведомление.")
        if await service.accounts(uid):
            return await message.answer("У вас уже подключён один аккаунт. Сначала используйте /disconnect.")
        if uid in pending_logins:
            return await message.answer("QR-вход уже запущен. Проверьте предыдущее сообщение.")
        status = await message.answer("Готовлю QR-подключение…")
        client = None
        try:
            client = new_login_client(uid)
            login, png = await begin_qr_login(client)
            photo = BufferedInputFile(png, filename="telegram-login.png")
            await status.delete()
            await message.answer_photo(
                photo,
                caption=(
                    "Откройте Telegram на другом устройстве: Настройки → Устройства → "
                    "Подключить устройство, затем отсканируйте QR.\n\n"
                    "Не пересылайте QR-код другим людям. Он даёт доступ к вашему аккаунту. "
                    "Код действителен ограниченное время."
                ),
            )

            async def finish_login():
                try:
                    timeout = max(1, login.r.expires - int(time.time()))
                    user = await login.wait(timeout=timeout)
                    session_string = await client.export_session_string()
                    await client.disconnect()
                    account_id = await service.connect_account(uid, session_string, user.first_name)
                    await bot.send_message(
                        uid,
                        f"✅ Аккаунт подключён: <b>{user.first_name}</b> (ID записи: {account_id}).\n"
                        "Теперь выполните /chats и вручную разрешите нужные группы."
                    )
                except asyncio.TimeoutError:
                    await bot.send_message(uid, "QR-код истёк. Запустите /connect ещё раз.")
                except Exception as exc:
                    logging.exception("QR login failed")
                    await bot.send_message(uid, f"Не удалось подключить аккаунт ({type(exc).__name__}).")
                finally:
                    try:
                        if client.is_connected:
                            await client.disconnect()
                    except Exception:
                        pass
                    pending_logins.pop(uid, None)

            task = asyncio.create_task(finish_login())
            pending_logins[uid] = (client, task)
        except Exception as exc:
            logging.exception("could not start QR login")
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
            await status.edit_text(f"Не удалось подготовить QR-вход ({type(exc).__name__}).")

    @dp.message(Command("chats"))
    async def chats(message: Message) -> None:
        uid = message.from_user.id
        if not await accepted(service, uid):
            return await message.answer("Сначала примите уведомление через /start.")
        accounts = await service.accounts(uid)
        if not accounts:
            return await message.answer("Сначала подключите аккаунт: /connect")
        account = accounts[0]
        session_string = await service.get_account_session(uid, account.id)
        client = new_user_client(session_string, uid)
        await client.start()
        discovered = 0
        try:
            async for dialog in client.get_dialogs(limit=200):
                chat = dialog.chat
                kind = str(chat.type).lower()
                if "group" not in kind and "supergroup" not in kind:
                    continue
                await service.register_chat(uid, account.id, chat.id, chat.title)
                discovered += 1
        finally:
            await client.stop()

        rows = await get_chats(uid, account.id)
        keyboard = InlineKeyboardBuilder()
        for chat in rows[:50]:
            marker = "🟢" if chat.is_authorized else "⚪"
            title = (chat.title or str(chat.chat_id)).replace("\n", " ")[:35]
            keyboard.button(
                text=f"{marker} {title}",
                callback_data=f"chat:toggle:{account.id}:{chat.chat_id}",
            )
        keyboard.adjust(1)
        await message.answer(
            f"Найдено/обновлено групп: {discovered}.\n"
            "🟢 — отправка разрешена, ⚪ — запрещена. Нажмите, чтобы изменить статус. "
            "Сам факт участия аккаунта в группе не означает согласие на рассылку.",
            reply_markup=keyboard.as_markup() if rows else None,
        )

    @dp.callback_query(F.data.startswith("chat:toggle:"))
    async def toggle_chat(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        try:
            _, _, account_raw, chat_raw = callback.data.split(":")
            account_id, chat_id = int(account_raw), int(chat_raw)
            rows = await get_chats(uid, account_id)
            current = next((row for row in rows if row.chat_id == chat_id), None)
            if current is None:
                return await callback.answer("Группа не найдена", show_alert=True)
            if current.is_authorized:
                await service.revoke_chat(uid, account_id, chat_id)
                await callback.answer("Отправка запрещена")
            else:
                await service.authorize_chat(uid, account_id, chat_id, explicitly_confirmed=True)
                await callback.answer("Группа разрешена")
            rows = await get_chats(uid, account_id)
            keyboard = InlineKeyboardBuilder()
            for chat in rows[:50]:
                marker = "🟢" if chat.is_authorized else "⚪"
                title = (chat.title or str(chat.chat_id)).replace("\n", " ")[:35]
                keyboard.button(text=f"{marker} {title}", callback_data=f"chat:toggle:{account_id}:{chat.chat_id}")
            keyboard.adjust(1)
            await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
        except Exception:
            logging.exception("chat authorization toggle failed")
            await callback.answer("Не удалось изменить разрешение", show_alert=True)

    @dp.message(Command("send"))
    async def prepare_send(message: Message) -> None:
        uid = message.from_user.id
        if not await accepted(service, uid):
            return await message.answer("Сначала примите уведомление через /start.")
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            return await message.answer("Формат: /send текст сообщения")
        accounts = await service.accounts(uid)
        if not accounts:
            return await message.answer("Сначала подключите аккаунт: /connect")
        account = accounts[0]
        chats = await service.authorized_chats(uid, account.id)
        if not chats:
            return await message.answer("Нет разрешённых групп. Выполните /chats и включите нужные.")
        chat_ids = [chat.chat_id for chat in chats[:50]]
        pending_sends[uid] = (account.id, chat_ids, text)
        await message.answer(
            f"Подтвердите отправку текста в {len(chat_ids)} групп(ы). "
            "Отправка начнётся только после подтверждения.",
            reply_markup=confirm_keyboard(),
        )

    @dp.callback_query(F.data == "send:cancel")
    async def cancel_send(callback: CallbackQuery) -> None:
        pending_sends.pop(callback.from_user.id, None)
        await callback.message.edit_text("Отправка отменена.")
        await callback.answer()

    @dp.callback_query(F.data == "send:confirm")
    async def confirm_send(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        pending = pending_sends.pop(uid, None)
        if not pending:
            return await callback.answer("Задача устарела. Подготовьте /send заново.", show_alert=True)
        account_id, chat_ids, text_to_send = pending
        job_id = None
        client = None
        sent = 0
        skipped = 0
        await callback.message.edit_text("⏳ Отправка запущена. Остановить её можно командой /stop.")
        try:
            job_id = await service.create_broadcast_job(uid, account_id, callback.message.message_id, chat_ids)
            await service.change_job_status(uid, job_id, "running")
            session_string = await service.get_account_session(uid, account_id)
            client = new_user_client(session_string, uid)
            await client.start()
            for chat_id in chat_ids:
                if not await service.chat_is_authorized(uid, account_id, chat_id):
                    skipped += 1
                    continue
                try:
                    await client.send_message(chat_id, text_to_send)
                    sent += 1
                except FloodWait as exc:
                    await service.change_job_status(uid, job_id, "failed")
                    await callback.message.answer(
                        f"Telegram ограничил отправку (FloodWait {exc.value} сек.). "
                        "Задача остановлена; обходить ограничение не будем."
                    )
                    return
            await service.change_job_status(uid, job_id, "completed")
            await callback.message.answer(f"Готово. Отправлено: {sent}; пропущено после отзыва разрешения: {skipped}.")
        except Exception as exc:
            logging.exception("broadcast job failed")
            if job_id is not None:
                try:
                    await service.change_job_status(uid, job_id, "failed")
                except Exception:
                    pass
            await callback.message.answer(f"Задача остановлена из-за ошибки ({type(exc).__name__}).")
        finally:
            if client is not None:
                try:
                    await client.stop()
                except Exception:
                    pass
        await callback.answer()

    @dp.message(Command("stop"))
    async def stop(message: Message) -> None:
        # Sending is sequential and the confirmation handler owns the task. This
        # command remains a clear UX signal; cancellation is implemented in the next worker revision.
        await message.answer("Команда остановки принята. Если отправка уже идёт, она завершится после текущего запроса.")

    @dp.message(Command("disconnect"))
    async def disconnect(message: Message) -> None:
        uid = message.from_user.id
        accounts = await service.accounts(uid)
        if not accounts:
            return await message.answer("Подключённого аккаунта нет.")
        account = accounts[0]
        try:
            session_string = await service.get_account_session(uid, account.id)
            client = new_user_client(session_string, uid)
            await client.start()
            try:
                await client.log_out()
            finally:
                if client.is_connected:
                    await client.disconnect()
        except Exception:
            logging.exception("Telegram logout failed; local credential will still be removed")
        await service.disconnect(uid, account.id)
        await message.answer("Аккаунт отключён, сохранённая сессия удалена из базы сервиса.")

    @dp.message(Command("status"))
    async def status(message: Message) -> None:
        uid = message.from_user.id
        accounts = await service.accounts(uid)
        await message.answer(
            f"Профиль: {'активен' if await accepted(service, uid) else 'уведомление не принято'}\n"
            f"Подключённых аккаунтов: {len(accounts)}"
        )

    @dp.message(Command("delete_me"))
    async def delete_me(message: Message) -> None:
        await message.answer("Сначала отключите аккаунт командой /disconnect, затем повторите /delete_me.")

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "/start — регистрация и уведомление\n"
            "/connect — подключение по QR\n"
            "/chats — найти группы и разрешить отправку\n"
            "/send текст — подготовить и подтвердить отправку\n"
            "/disconnect — отозвать подключённую сессию\n"
            "/status — статус профиля"
        )

    logging.basicConfig(level=logging.INFO)
    try:
        await dp.start_polling(bot)
    finally:
        for client, task in list(pending_logins.values()):
            task.cancel()
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
