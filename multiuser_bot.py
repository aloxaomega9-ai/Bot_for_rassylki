"""Multi-user Telegram broadcast bot.

Account authorization uses phone-code login with optional 2FA. One-time
credentials are used only in memory and are never persisted or logged.
"""
import asyncio
import logging
import os

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram.errors import FloodWait, SessionPasswordNeeded
from sqlalchemy import select

from multiuser.db import dispose_engine, get_session_factory
from multiuser.models import AuthorizedChat, ConnectedAccount, User
from multiuser.service import MultiUserService
from multiuser.telegram import new_login_client, new_user_client


NOTICE_VERSION = "2026-11-17"
NOTICE = (
    "<b>Перед началом</b>\n\n"
    "Сервис хранит ваш Telegram ID и зашифрованную сессию подключённого аккаунта. "
    "Сервер сможет использовать эту сессию, пока аккаунт подключён. Шифрование "
    "защищает данные в базе, но не скрывает их от работающего приложения.\n\n"
    "Подключение проходит по номеру телефона и коду Telegram; при включённой "
    "двухэтапной проверке понадобится пароль. Код и пароль используются только "
    "во время входа и не сохраняются в базе. Вы можете отключить аккаунт "
    "командой /disconnect.\n\n"
    "Отправка разрешена только в группы, где у вас есть разрешение и участники "
    "ожидают сообщения. Не используйте сервис для нежелательных рассылок. "
    "Одна задача — не более 50 групп; при FloodWait отправка остановится.\n\n"
    "Нажимая «Принимаю», вы подтверждаете, что прочитали это уведомление."
)

pending_logins: dict[int, dict] = {}
pending_sends: dict[int, tuple[int, list[int], str]] = {}
active_jobs: dict[int, asyncio.Task] = {}


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📨 Авторассылка"), KeyboardButton(text="📝 Текст сообщения")],
            [KeyboardButton(text="👥 Настройка групп"), KeyboardButton(text="⏱ Интервал")],
            [KeyboardButton(text="👤 Профили"), KeyboardButton(text="📖 Инструкция")],
            [KeyboardButton(text="⏹ Остановить"), KeyboardButton(text="❔ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


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
            .join(ConnectedAccount, AuthorizedChat.account_id == ConnectedAccount.id)
            .where(
                AuthorizedChat.account_id == account_id,
                ConnectedAccount.telegram_user_id == user_id,
                ConnectedAccount.is_active.is_(True),
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
            "✅ Уведомление принято. Теперь можно пользоваться главным меню."
        )
        await callback.message.answer("Главное меню:", reply_markup=main_keyboard())
        await callback.answer("Сохранено")

    @dp.callback_query(F.data == "notice:decline")
    async def decline_notice(callback: CallbackQuery) -> None:
        await callback.message.edit_text("Уведомление не принято. Функции подключения и отправки недоступны.")
        await callback.answer()

    @dp.message(Command("connect"))
    async def connect(message: Message) -> None:
        uid = message.from_user.id
        if message.chat.type != "private":
            return await message.answer("Подключение аккаунта доступно только в личном чате с ботом.")
        if not await accepted(service, uid):
            return await message.answer("Сначала отправьте /start и примите уведомление.")
        if await service.accounts(uid):
            return await message.answer("У вас уже подключён один аккаунт. Сначала используйте /disconnect.")
        if uid in pending_logins:
            return await message.answer("Вход уже начат. Продолжите по инструкции или используйте /cancel.")
        await message.answer(
            "Отправьте номер телефона в международном формате, например +7XXXXXXXXXX.\n"
            "Telegram пришлёт код в приложение Telegram или другим доступным способом. "
            "Никому не пересылайте этот код. Бот использует его только для подключения вашего аккаунта."
        )
        pending_logins[uid] = {"stage": "phone"}

    @dp.message(Command("cancel"))
    async def cancel_login(message: Message) -> None:
        state = pending_logins.pop(message.from_user.id, None)
        if state:
            client = state.get("client")
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    logging.exception("temporary login disconnect failed")
            await message.answer("Подключение отменено.")
        else:
            await message.answer("Нет активного процесса подключения.")

    @dp.message(F.text & ~F.text.startswith("/") & ~F.text.in_({"📨 Авторассылка", "📝 Текст сообщения", "👥 Настройка групп", "⏱ Интервал", "👤 Профили", "📖 Инструкция", "⏹ Остановить", "❔ Помощь"}))
    async def process_login_input(message: Message) -> None:
        uid = message.from_user.id
        state = pending_logins.get(uid)
        if not state:
            return
        if message.chat.type != "private":
            return
        value = (message.text or "").strip()
        if value.startswith("/"):
            return
        stage = state["stage"]

        # Remove one-time codes and 2FA passwords from the visible chat history.
        if stage in {"phone", "code", "password"}:
            try:
                await message.delete()
            except Exception:
                logging.warning("Could not delete a sensitive login message from chat history")

        if stage == "phone":
            phone = value.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            if not phone.startswith("+") or not phone[1:].isdigit() or not 8 <= len(phone[1:]) <= 15:
                return await message.answer("Неверный формат. Отправьте номер в формате +7XXXXXXXXXX или используйте /cancel.")
            client = new_login_client(uid)
            try:
                await client.connect()
                sent_code = await client.send_code(phone)
                pending_logins[uid] = {
                    "stage": "code",
                    "client": client,
                    "phone": phone,
                    "phone_code_hash": sent_code.phone_code_hash,
                }
                await message.answer(
                    "Код отправлен Telegram. Введите его следующим сообщением. "
                    "Если передумали, используйте /cancel."
                )
            except Exception as exc:
                logging.warning("Could not request Telegram login code: %s", type(exc).__name__)
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
                pending_logins.pop(uid, None)
                await message.answer(f"Не удалось запросить код ({type(exc).__name__}). Проверьте номер и попробуйте /connect.")
            return

        client = state["client"]
        if stage == "code":
            if not value.isdigit() or not 4 <= len(value) <= 8:
                return await message.answer("Введите только цифры кода из Telegram или используйте /cancel.")
            try:
                user = await client.sign_in(state["phone"], state["phone_code_hash"], value)
            except SessionPasswordNeeded:
                state["stage"] = "password"
                await message.answer(
                    "На аккаунте включена двухэтапная проверка. Введите пароль Telegram. "
                    "Сообщение с паролем будет удалено после получения; /cancel отменяет вход."
                )
                return
            except Exception as exc:
                logging.warning("Telegram phone-code sign-in failed: %s", type(exc).__name__)
                pending_logins.pop(uid, None)
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
                await message.answer("Код не принят или срок его действия истёк. Начните заново командой /connect.")
                return
        elif stage == "password":
            try:
                user = await client.check_password(value)
            except Exception as exc:
                logging.warning("Telegram 2FA check failed: %s", type(exc).__name__)
                pending_logins.pop(uid, None)
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
                await message.answer("Пароль не принят. Подключение отменено; запустите /connect заново.")
                return
        else:
            pending_logins.pop(uid, None)
            return

        try:
            session_string = await client.export_session_string()
            await client.disconnect()
            account_id = await service.connect_account(uid, session_string, getattr(user, "first_name", None))
            await message.answer(
                f"✅ Аккаунт подключён (ID записи: {account_id}).\n"
                "Теперь выполните /chats и вручную разрешите только нужные группы."
            )
        except Exception as exc:
            logging.exception("Could not save encrypted Telegram session")
            await message.answer(f"Вход выполнен, но сохранить сессию не удалось ({type(exc).__name__}).")
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            pending_logins.pop(uid, None)

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

    async def execute_broadcast(uid: int, account_id: int, chat_ids: list[int], text_to_send: str, status_message):
        job_id = None
        client = None
        sent = 0
        skipped = 0
        try:
            job_id = await service.create_broadcast_job(uid, account_id, status_message.message_id, chat_ids)
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
                    await status_message.answer(
                        f"Telegram ограничил отправку (FloodWait {exc.value} сек.). "
                        "Задача остановлена; обходить ограничение не будем."
                    )
                    return
            await service.change_job_status(uid, job_id, "completed")
            await status_message.answer(f"Готово. Отправлено: {sent}; пропущено после отзыва разрешения: {skipped}.")
        except asyncio.CancelledError:
            if job_id is not None:
                try:
                    await service.change_job_status(uid, job_id, "cancelled")
                except Exception:
                    pass
            await status_message.answer(f"Отправка остановлена. Успешно отправлено до остановки: {sent}.")
            raise
        except Exception as exc:
            logging.exception("broadcast job failed")
            if job_id is not None:
                try:
                    await service.change_job_status(uid, job_id, "failed")
                except Exception:
                    pass
            await status_message.answer(f"Задача остановлена из-за ошибки ({type(exc).__name__}).")
        finally:
            if client is not None:
                try:
                    await client.stop()
                except Exception:
                    pass
            active_jobs.pop(uid, None)

    @dp.callback_query(F.data == "send:confirm")
    async def confirm_send(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        pending = pending_sends.pop(uid, None)
        if not pending:
            return await callback.answer("Задача устарела. Подготовьте /send заново.", show_alert=True)
        if uid in active_jobs:
            return await callback.answer("У вас уже выполняется отправка.", show_alert=True)
        account_id, chat_ids, text_to_send = pending
        await callback.message.edit_text("⏳ Отправка запущена. Для остановки используйте /stop.")
        task = asyncio.create_task(execute_broadcast(uid, account_id, chat_ids, text_to_send, callback.message))
        active_jobs[uid] = task
        await callback.answer("Запущено")

    @dp.message(Command("stop"))
    async def stop(message: Message) -> None:
        task = active_jobs.get(message.from_user.id)
        if task is None:
            return await message.answer("Активной отправки нет.")
        task.cancel()
        await message.answer("Запрос на остановку отправлен.")

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
        uid = message.from_user.id
        task = active_jobs.get(uid)
        if task is not None:
            task.cancel()
        for account in await service.accounts(uid):
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
                logging.exception("could not revoke Telegram session during profile deletion")
            await service.disconnect(uid, account.id)
        await service.delete_user(uid)
        await message.answer("Данные профиля удалены. Подключённые сессии отозваны, если Telegram был доступен.")


    @dp.message(F.text == "📨 Авторассылка")
    async def menu_broadcast(message: Message) -> None:
        await message.answer("Подготовить отправку: /send ваш текст. Бот запросит подтверждение перед началом.")

    @dp.message(F.text == "📝 Текст сообщения")
    async def menu_text(message: Message) -> None:
        await message.answer("Введите текст командой: /send ваш текст сообщения")

    @dp.message(F.text == "👥 Настройка групп")
    async def menu_chats(message: Message) -> None:
        await message.answer("Откройте список групп: /chats. Нажмите на группу, чтобы разрешить или запретить отправку.")

    @dp.message(F.text == "⏱ Интервал")
    async def menu_interval(message: Message) -> None:
        await message.answer("Настройка интервала пока не реализована. Кнопка оставлена как пункт будущего обновления.")

    @dp.message(F.text == "👤 Профили")
    async def menu_profiles(message: Message) -> None:
        accounts = await service.accounts(message.from_user.id)
        if accounts:
            labels = [account.label or f"Аккаунт #{account.id}" for account in accounts]
            await message.answer("Подключённый профиль:\n" + "\n".join(labels) + "\nОтключить: /disconnect")
        else:
            await message.answer("Аккаунт ещё не подключён. Для подключения: /connect")

    @dp.message(F.text == "⏹ Остановить")
    async def menu_stop(message: Message) -> None:
        task = active_jobs.get(message.from_user.id)
        if task is None:
            await message.answer("Активной отправки нет.")
        else:
            task.cancel()
            await message.answer("Запрос на остановку отправлен.")

    @dp.message(F.text.in_({"📖 Инструкция", "❔ Помощь"}))
    async def menu_help(message: Message) -> None:
        await message.answer("Подключение: /connect\nГруппы: /chats\nОтправка: /send текст\nОтмена входа: /cancel\nОтключение: /disconnect\nСтатус: /status", reply_markup=main_keyboard())

    @dp.message(Command("menu"))
    async def show_menu(message: Message) -> None:
        await message.answer("Главное меню:", reply_markup=main_keyboard())

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "/start — регистрация и уведомление\n"
            "/connect — подключение по номеру и коду\n/cancel — отменить подключение\n"
            "/chats — найти группы и разрешить отправку\n"
            "/send текст — подготовить и подтвердить отправку\n"
            "/disconnect — отозвать подключённую сессию\n"
            "/status — статус профиля"
        )

    logging.basicConfig(level=logging.INFO)
    if os.getenv("ALLOW_SCHEMA_CREATE") == "1":
        from multiuser.schema import create_schema
        await create_schema()

    health_app = web.Application()
    health_app.router.add_get("/", lambda _request: web.Response(text="ok"))
    health_app.router.add_get("/health", lambda _request: web.Response(text="ok"))
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

    async def keep_alive() -> None:
        """Send inbound HTTP traffic through Render's public proxy periodically."""
        public_url = os.getenv("RENDER_EXTERNAL_URL")
        if not public_url:
            logging.info("RENDER_EXTERNAL_URL is not set; keep-alive is disabled")
            return

        health_url = f"{public_url.rstrip('/')}/health"
        timeout = ClientTimeout(total=15)
        async with ClientSession(timeout=timeout) as http:
            while True:
                await asyncio.sleep(9 * 60)
                try:
                    async with http.get(health_url) as response:
                        await response.read()
                        if response.status == 200:
                            logging.info("Render keep-alive ping succeeded")
                        else:
                            logging.warning(
                                "Render keep-alive ping returned HTTP %s",
                                response.status,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.warning("Render keep-alive ping failed", exc_info=True)

    keep_alive_task = asyncio.create_task(keep_alive(), name="render-keep-alive")
    try:
        await dp.start_polling(bot)
    finally:
        keep_alive_task.cancel()
        try:
            await keep_alive_task
        except asyncio.CancelledError:
            pass
        await runner.cleanup()
        for state in list(pending_logins.values()):
            client = state.get("client")
            try:
                if client is not None and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
        pending_logins.clear()
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
