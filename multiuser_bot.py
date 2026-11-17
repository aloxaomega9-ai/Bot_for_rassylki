"""Separate opt-in onboarding bot for the multi-user prototype.

This entry point only registers users and records privacy-notice acceptance.
It does not connect personal Telegram accounts or send broadcasts.
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from multiuser.db import dispose_engine, get_session_factory
from multiuser.models import User
from multiuser.service import MultiUserService


NOTICE_VERSION = "2026-11-17"
NOTICE = (
    "<b>Перед началом — важная информация</b>\n\n"
    "Это ранняя версия сервиса. Сейчас она умеет только регистрировать "
    "пользователей и хранить факт принятия уведомления. Подключения личного "
    "Telegram-аккаунта и отправки сообщений пока нет.\n\n"
    "Сервис сохраняет ваш Telegram ID и дату принятия уведомления. "
    "В будущих версиях подключение личного аккаунта будет отдельным "
    "добровольным действием: Telegram-сессия является секретом, а работающий "
    "сервер технически сможет использовать её. Шифрование базы не исключает "
    "доступа приложения к сессии во время работы.\n\n"
    "Не отправляйте сюда коды входа, пароли или строки сессии. "
    "Используйте сервис только для сообщений в сообществах, где у вас есть "
    "разрешение и участники ожидают такие сообщения. Массовые нежелательные "
    "сообщения и обход ограничений Telegram не поддерживаются.\n\n"
    "Команда /delete_me удалит сохранённые данные профиля."
)


def notice_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Принимаю уведомление", callback_data="notice:accept")
    keyboard.button(text="❌ Не принимаю", callback_data="notice:decline")
    keyboard.adjust(1)
    return keyboard.as_markup()


def delete_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Да, удалить мои данные", callback_data="delete:confirm")
    keyboard.button(text="Отмена", callback_data="delete:cancel")
    keyboard.adjust(1)
    return keyboard.as_markup()


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    service = MultiUserService()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if message.from_user is None:
            return
        await service.register(message.from_user.id)
        await message.answer(NOTICE, reply_markup=notice_keyboard())

    @dp.callback_query(F.data == "notice:accept")
    async def accept_notice(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        await service.accept_notice(callback.from_user.id, NOTICE_VERSION)
        await callback.message.edit_text(
            "✅ Уведомление принято. Профиль зарегистрирован.\n"
            "Подключение аккаунтов и отправка сообщений пока недоступны.\n\n"
            "Команды: /status — статус профиля; /delete_me — удалить данные."
        )
        await callback.answer("Сохранено")

    @dp.callback_query(F.data == "notice:decline")
    async def decline_notice(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                "Уведомление не принято. Вы можете удалить профиль командой /delete_me."
            )
        await callback.answer()

    @dp.message(Command("status"))
    async def status(message: Message) -> None:
        if message.from_user is None:
            return
        factory = get_session_factory()
        async with factory() as session:
            user = await session.scalar(
                select(User).where(User.telegram_user_id == message.from_user.id)
            )
        if user is None:
            await message.answer("Профиль не найден. Отправьте /start.")
            return
        accepted = bool(user.terms_accepted_at)
        await message.answer(
            "Профиль: активен\n"
            f"Уведомление: {'принято' if accepted else 'не принято'}\n"
            "Подключённых аккаунтов: 0 (подключение пока выключено)."
        )

    @dp.message(Command("delete_me"))
    async def delete_request(message: Message) -> None:
        await message.answer(
            "Удалить профиль и связанные с ним записи сервиса?",
            reply_markup=delete_keyboard(),
        )

    @dp.callback_query(F.data == "delete:confirm")
    async def delete_confirm(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        await service.delete_user(callback.from_user.id)
        await callback.message.edit_text(
            "Данные профиля удалены из базы сервиса. "
            "Если в будущем вы подключите Telegram-аккаунт, его сессию также "
            "нужно будет отозвать отдельно в Telegram."
        )
        await callback.answer("Удалено")

    @dp.callback_query(F.data == "delete:cancel")
    async def delete_cancel(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_text("Удаление отменено.")
        await callback.answer()

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "/start — регистрация и уведомление о конфиденциальности\n"
            "/status — статус профиля\n"
            "/delete_me — удалить данные профиля"
        )

    logging.basicConfig(level=logging.INFO)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
