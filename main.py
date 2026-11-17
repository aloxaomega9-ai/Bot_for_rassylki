"""
Юзербот-авторассылка с управлением через Telegram-бота.

Как это устроено:
  * aiogram 3  — обычный бот (токен от @BotFather): меню, кнопки, настройки.
  * Pyrogram   — ваш личный аккаунт: именно он рассылает сообщения по группам.
  * Настройки хранятся в «Избранном» вашего аккаунта (одно служебное сообщение),
    поэтому на бесплатном Render (где диск стирается при перезапуске)
    ничего не теряется и рассылка сама продолжается после перезапуска.
  * Сессия аккаунта живёт в переменной окружения SESSION_STRING.
  * Мини-веб-сервер + самопинг не дают Render-сервису «засыпать».
"""

import asyncio
import html
import io
import json
import logging
import math
import os
import random
import re
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.state.context import FSMContext
from aiogram.state.state import State, StatesGroup
from aiogram.state.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiohttp import ClientSession, web
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.enums import ParseMode as PMode
from pyrogram.errors import (
    ApiIdInvalid,
    FloodWait,
    MessageIdInvalid,
    MessageNotModified,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    RPCError,
    SessionPasswordNeeded,
    SlowmodeWait,
)

try:  # локальный запуск: читаем .env, на Render он не нужен
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ──────────────────────────── НАСТРОЙКИ (из переменных окружения) ───────────
BOT_TOKEN = os.environ["BOT_TOKEN"]          # токен бота от @BotFather
API_ID = int(os.environ["API_ID"])           # с my.telegram.org
API_HASH = os.environ["API_HASH"]            # с my.telegram.org
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))   # ваш Telegram ID (узнать: команда /id)
SESSION_STRING = os.getenv("SESSION_STRING", "")  # появится после первого входа
PORT = int(os.getenv("PORT", "10000"))       # Render подставляет сам
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "")   # Render подставляет сам

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("userbot")

# ──────────────────────────── СОСТОЯНИЕ ──────────────────────────────────────
MARKER = "USERBOT_STATE_V1"  # по этой метке находим служебное сообщение в Избранном
S = {
    "groups": [],        # id выбранных групп
    "interval": 2.0,     # пауза между отправками, минуты
    "jitter": True,      # случайный разброс паузы (безопаснее для аккаунта)
    "autostop": 0,       # остановиться после N кругов (0 = бесконечно)
    "post_id": None,     # id сообщения-рассылки в Избранном
    "post_type": None,   # text / photo / video / document / animation
    "running": False,    # идёт ли рассылка (для автопродолжения после рестарта)
    "total_sent": 0,     # всего отправлено за всё время
}
runtime = {"sent": 0, "failed": 0, "cycles": 0, "errors": {}, "started": None}

userbot: Client | None = None
account_label = ""
state_msg_id: int | None = None
state_lock = asyncio.Lock()
stop_event = asyncio.Event()
mail_task: asyncio.Task | None = None
dialogs: list[tuple[int, str]] = []     # кэш групп аккаунта
titles: dict[int, str] = {}
login: dict = {}                        # данные текущего входа в аккаунт

TYPE_NAMES = {"text": "Текст", "photo": "Фото", "video": "Видео",
              "document": "Файл", "animation": "GIF"}

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
admin = Router()
public = Router()
admin.message.filter(F.from_user.id == ADMIN_ID)
admin.callback_query.filter(F.from_user.id == ADMIN_ID)


class Form(StatesGroup):
    text = State()
    interval = State()
    autostop = State()


class Auth(StatesGroup):
    phone = State()
    code = State()
    password = State()


# ──────────────────────────── ХРАНЕНИЕ НАСТРОЕК В «ИЗБРАННОМ» ────────────────
async def load_state():
    """Читаем настройки из служебного сообщения в Избранном."""
    global state_msg_id
    try:
        async for m in userbot.search_messages("me", query=MARKER, limit=5):
            if m.text and m.text.startswith(MARKER):
                S.update(json.loads(m.text[len(MARKER):]))
                state_msg_id = m.id
                log.info("Настройки загружены из Избранного")
                return
    except Exception as e:
        log.warning("Не удалось загрузить настройки: %s", e)


async def save_state():
    """Сохраняем настройки в Избранное (одно сообщение, редактируется на месте)."""
    global state_msg_id
    if not userbot:
        return
    text = MARKER + json.dumps(S, ensure_ascii=False, separators=(",", ":"))
    async with state_lock:
        try:
            if state_msg_id:
                await userbot.edit_message_text("me", state_msg_id, text, parse_mode=PMode.DISABLED)
                return
        except MessageNotModified:
            return
        except MessageIdInvalid:
            state_msg_id = None
        except Exception as e:
            log.warning("Ошибка сохранения: %s", e)
            return
        try:
            m = await userbot.send_message("me", text, parse_mode=PMode.DISABLED)
            state_msg_id = m.id
        except Exception as e:
            log.warning("Ошибка сохранения: %s", e)


# ──────────────────────────── КЛАВИАТУРЫ И ТЕКСТЫ ────────────────────────────
MENU = ["📨 Авторассылка", "📝 Текст сообщения", "⏱ Интервал", "👥 Настройка групп",
        "👤 Профили", "📖 Инструкция", "❓ Помощь"]


def main_menu():
    b = ReplyKeyboardBuilder()
    for t in MENU:
        b.button(text=t)
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def is_running() -> bool:
    return mail_task is not None and not mail_task.done()


def fmt_interval(v: float) -> str:
    return f"{v:g} мин"


def mailing_text() -> str:
    post = TYPE_NAMES.get(S["post_type"], "не настроено")
    auto = f"{S['autostop']} кр." if S["autostop"] else "∞"
    status = "🟢 идёт" if is_running() else "🔴 остановлена"
    acc = html.escape(account_label) if account_label else "не подключён"
    return (
        "📨 <b>Авторассылка</b>\n\n"
        f"Аккаунт: {acc}\n"
        f"Тип сообщения: {post}\n"
        f"Группы: {len(S['groups'])}\n"
        f"Интервал: {fmt_interval(S['interval'])}"
        f"{' (с разбросом)' if S['jitter'] else ''}\n"
        f"Автостоп: {auto}\n"
        f"Статус: {status}"
    )


def mailing_kb():
    b = InlineKeyboardBuilder()
    if is_running():
        b.button(text="⏹ Остановить", callback_data="m:stop")
    else:
        b.button(text="▶️ Запустить", callback_data="m:run")
    b.button(text="📊 Статистика", callback_data="m:stats")
    b.button(text="⏳ Автостоп", callback_data="m:autostop")
    b.button(text="🔄 Обновить", callback_data="m:refresh")
    b.button(text="✖️ Закрыть", callback_data="x")
    b.adjust(1, 2, 1, 1)
    return b.as_markup()


def stats_text() -> str:
    lines = [
        "📊 <b>Статистика</b>\n",
        f"За этот запуск: отправлено {runtime['sent']}, ошибок {runtime['failed']}, "
        f"кругов {runtime['cycles']}",
        f"За всё время отправлено: {S['total_sent']}",
    ]
    if runtime["started"]:
        mins = int((time.time() - runtime["started"]) / 60)
        lines.append(f"Работает: {mins // 60} ч {mins % 60} мин")
    if runtime["errors"]:
        lines.append("\n⚠️ <b>Проблемные группы</b> (последняя ошибка):")
        for cid, reason in list(runtime["errors"].items())[:15]:
            lines.append(f"• {html.escape(titles.get(cid, str(cid)))} — {html.escape(reason)}")
    return "\n".join(lines)


def numpad_kb():
    b = InlineKeyboardBuilder()
    for d in "123456789":
        b.button(text=d, callback_data=f"c:{d}")
    b.button(text="⌫", callback_data="c:del")
    b.button(text="0", callback_data="c:0")
    b.button(text="✅", callback_data="c:ok")
    b.button(text="❌ Отмена", callback_data="c:cancel")
    b.adjust(3, 3, 3, 3, 1)
    return b.as_markup()


def cancel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="c:cancel")
    return b.as_markup()


PAGE = 10


def groups_menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📋 Список групп", callback_data="g:l:0")
    b.button(text="✅ Выбрать все", callback_data="g:all")
    b.button(text="🗑 Сбросить выбор", callback_data="g:none")
    b.button(text="🔄 Обновить список", callback_data="g:reload")
    b.button(text="✖️ Закрыть", callback_data="x")
    b.adjust(1, 2, 1, 1)
    return b.as_markup()


def groups_list_kb(page: int):
    pages = max(1, math.ceil(len(dialogs) / PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = dialogs[page * PAGE:(page + 1) * PAGE]
    b = InlineKeyboardBuilder()
    for cid, title in chunk:
        mark = "✅" if cid in S["groups"] else "⬜"
        b.button(text=f"{mark} {title[:30]}", callback_data=f"g:t:{cid}:{page}")
    b.button(text="◀️", callback_data=f"g:l:{page - 1}")
    b.button(text=f"{page + 1}/{pages}", callback_data="noop")
    b.button(text="▶️", callback_data=f"g:l:{page + 1}")
    b.button(text="⬅️ Назад", callback_data="g:menu")
    b.adjust(*([1] * len(chunk)), 3, 1)
    return b.as_markup()


def interval_kb():
    b = InlineKeyboardBuilder()
    for v in (1, 2, 3, 5, 10, 15):
        b.button(text=f"{v} мин", callback_data=f"i:{v}")
    b.button(text="✏️ Своё значение", callback_data="i:custom")
    b.button(text=f"🎲 Разброс: {'вкл' if S['jitter'] else 'выкл'}", callback_data="i:jitter")
    b.button(text="✖️ Закрыть", callback_data="x")
    b.adjust(3, 3, 1, 1, 1)
    return b.as_markup()


def interval_text() -> str:
    return (
        "⏱ <b>Интервал</b>\n\n"
        f"Сейчас: <b>{fmt_interval(S['interval'])}</b> между отправками в разные чаты.\n"
        "Чем больше пауза — тем безопаснее для аккаунта. Рекомендуется от 2 минут.\n"
        "🎲 «Разброс» случайно меняет паузу на ±20% — так рассылка выглядит естественнее."
    )


def profile_kb():
    b = InlineKeyboardBuilder()
    if userbot:
        b.button(text="🚪 Выйти из аккаунта", callback_data="p:del")
    else:
        b.button(text="➕ Добавить аккаунт", callback_data="p:add")
    b.button(text="✖️ Закрыть", callback_data="x")
    b.adjust(1)
    return b.as_markup()


async def edit(cb: CallbackQuery, text: str, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ──────────────────────────── РАССЫЛКА ───────────────────────────────────────
async def pause(seconds: float) -> bool:
    """Ждём seconds секунд. Возвращает True, если за это время нажали «Стоп»."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def send_one(chat_id: int):
    """Отправка поста в один чат: copy_message сохраняет форматирование и медиа."""
    for _ in range(2):
        try:
            await userbot.copy_message(chat_id, "me", S["post_id"])
            runtime["sent"] += 1
            S["total_sent"] += 1
            runtime["errors"].pop(chat_id, None)
            return
        except FloodWait as e:
            log.warning("FloodWait %s сек", e.value)
            if await pause(e.value + 1):
                return
        except SlowmodeWait as e:
            runtime["failed"] += 1
            runtime["errors"][chat_id] = f"медленный режим {e.value} с"
            return
        except RPCError as e:
            runtime["failed"] += 1
            runtime["errors"][chat_id] = getattr(e, "ID", None) or type(e).__name__
            return
        except Exception as e:
            runtime["failed"] += 1
            runtime["errors"][chat_id] = type(e).__name__
            return
    runtime["failed"] += 1
    runtime["errors"][chat_id] = "FLOOD_WAIT"


async def notify(text: str):
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        log.warning("notify: %s", e)


async def mailing_loop():
    reason = "остановлена вручную"
    try:
        while not stop_event.is_set():
            targets = list(S["groups"])
            if not targets or not S["post_id"]:
                reason = "нет групп или текста сообщения"
                break
            for chat_id in targets:
                if stop_event.is_set():
                    break
                await send_one(chat_id)
                delay = S["interval"] * 60
                if S["jitter"]:
                    delay *= random.uniform(0.8, 1.2)
                if await pause(delay):
                    break
            else:  # круг завершён без остановки
                runtime["cycles"] += 1
                await save_state()
                await notify(f"🔁 Круг {runtime['cycles']} завершён. "
                             f"Отправлено: {runtime['sent']}, ошибок: {runtime['failed']}")
                if S["autostop"] and runtime["cycles"] >= S["autostop"]:
                    reason = f"выполнено {S['autostop']} кр. (автостоп)"
                    break
    except asyncio.CancelledError:
        raise  # перезапуск сервера: running остаётся True → рассылка продолжится сама
    except Exception as e:
        log.exception("Сбой рассылки")
        reason = f"ошибка: {type(e).__name__}"
    S["running"] = False
    await save_state()
    await notify(f"⏹ Рассылка завершена: {reason}.\n\n" + stats_text())


def start_mailing():
    global mail_task
    stop_event.clear()
    S["running"] = True
    runtime.update(sent=0, failed=0, cycles=0, errors={}, started=time.time())
    mail_task = asyncio.create_task(mailing_loop())


# ──────────────────────────── ПОДКЛЮЧЕНИЕ АККАУНТА ───────────────────────────
def make_client(session: str | None = None) -> Client:
    if session:
        return Client("userbot", api_id=API_ID, api_hash=API_HASH,
                      session_string=session, in_memory=True)
    return Client("login", api_id=API_ID, api_hash=API_HASH, in_memory=True)


async def refresh_dialogs():
    global dialogs
    res = []
    async for d in userbot.get_dialogs():
        c = d.chat
        if c.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            res.append((c.id, c.title or str(c.id)))
    dialogs = res
    titles.update(dict(res))


async def after_connect():
    """Выполняется после запуска клиента: имя аккаунта, настройки, автопродолжение."""
    global account_label
    me = await userbot.get_me()
    account_label = f"{me.first_name or ''} (+{me.phone_number})" if me.phone_number else (me.first_name or "аккаунт")
    await load_state()
    try:
        await refresh_dialogs()
    except Exception as e:
        log.warning("Не удалось получить диалоги: %s", e)
    if S["running"] and S["groups"] and S["post_id"]:
        start_mailing()
        await notify("♻️ Сервер перезапускался — рассылка продолжена автоматически.")
    else:
        S["running"] = False


async def drop_login():
    c = login.pop("client", None)
    login.clear()
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass


# ──────────────────────────── ХЕНДЛЕРЫ: ОБЩИЕ ────────────────────────────────
@public.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>\n"
                   "Впишите его в переменную ADMIN_ID.")


@admin.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Панель управления авторассылкой.\nВыберите раздел в меню ниже.",
                   reply_markup=main_menu())


@admin.callback_query(F.data == "x")
async def cb_close(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.delete()


@admin.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ──────────────────────────── ХЕНДЛЕРЫ: ПУНКТЫ МЕНЮ ──────────────────────────
@admin.message(F.text == "📨 Авторассылка")
async def menu_mailing(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(mailing_text(), reply_markup=mailing_kb())


@admin.message(F.text == "📝 Текст сообщения")
async def menu_text(m: Message, state: FSMContext):
    await state.clear()
    if not userbot:
        return await m.answer("Сначала подключите аккаунт: 👤 Профили → ➕ Добавить аккаунт.")
    cur = TYPE_NAMES.get(S["post_type"], "не задано")
    await state.set_state(Form.text)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="c:cancel")
    await m.answer(
        f"📝 <b>Текст сообщения</b>\nСейчас: {cur}\n\n"
        "Отправьте сюда новое сообщение — оно и будет рассылаться:\n"
        "текст, фото, видео, файл или GIF (с подписью). Форматирование "
        "(жирный, курсив, ссылки) сохранится.\n"
        "Готовый пост лежит у вас в Избранном.",
        reply_markup=b.as_markup(),
    )


@admin.message(F.text == "⏱ Интервал")
async def menu_interval(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(interval_text(), reply_markup=interval_kb())


@admin.message(F.text == "👥 Настройка групп")
async def menu_groups(m: Message, state: FSMContext):
    await state.clear()
    if not userbot:
        return await m.answer("Сначала подключите аккаунт: 👤 Профили → ➕ Добавить аккаунт.")
    await m.answer(f"👥 <b>Настройка групп</b>\nВыбрано: {len(S['groups'])} из {len(dialogs)}",
                   reply_markup=groups_menu_kb())


@admin.message(F.text == "👤 Профили")
async def menu_profile(m: Message, state: FSMContext):
    await state.clear()
    txt = f"👤 <b>Профиль</b>\n\nАккаунт: {html.escape(account_label)}" if userbot \
        else "👤 <b>Профиль</b>\n\nАккаунт не подключён."
    await m.answer(txt, reply_markup=profile_kb())


@admin.message(F.text == "📖 Инструкция")
async def menu_help(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "📖 <b>Инструкция</b>\n\n"
        "1. 👤 Профили → ➕ Добавить аккаунт (телефон, затем код кнопками).\n"
        "2. 📝 Текст сообщения — отправьте пост, который нужно рассылать.\n"
        "3. 👥 Настройка групп — отметьте нужные группы.\n"
        "4. ⏱ Интервал — задайте паузу между отправками (от 2 минут безопаснее).\n"
        "5. 📨 Авторассылка → ▶️ Запустить.\n\n"
        "⏳ <b>Автостоп</b> — завершить после N кругов по всем группам (0 = без конца).\n"
        "Рассылка продолжается сама после перезапуска сервера."
    )


@admin.message(F.text == "❓ Помощь")
async def menu_support(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "❓ <b>Помощь</b>\n\n"
        "• Не приходит код — проверьте приложение Telegram (чат «Telegram»), не вводите код в обычном сообщении: "
        "Telegram может его аннулировать. Используйте кнопки.\n"
        "• В группе ошибка — аккаунту запрещено писать (бан, режим «только админы»). "
        "Смотрите 📊 Статистику, проблемные группы можно убрать из выбора.\n"
        "• Аккаунт получил ограничения — увеличьте интервал и не рассылайте в чужие чаты без согласия: "
        "за спам Telegram может ограничить или заблокировать аккаунт."
    )


# ──────────────────────────── ХЕНДЛЕРЫ: ПАНЕЛЬ РАССЫЛКИ ──────────────────────
@admin.callback_query(F.data == "m:refresh")
async def cb_refresh(cb: CallbackQuery):
    await edit(cb, mailing_text(), mailing_kb())
    await cb.answer()


@admin.callback_query(F.data == "m:run")
async def cb_run(cb: CallbackQuery):
    if not userbot:
        return await cb.answer("Сначала подключите аккаунт", show_alert=True)
    if not S["post_id"]:
        return await cb.answer("Сначала задайте текст сообщения", show_alert=True)
    if not S["groups"]:
        return await cb.answer("Сначала выберите группы", show_alert=True)
    if not is_running():
        start_mailing()
        await save_state()
    await edit(cb, mailing_text(), mailing_kb())
    await cb.answer("Рассылка запущена")


@admin.callback_query(F.data == "m:stop")
async def cb_stop(cb: CallbackQuery):
    stop_event.set()
    await cb.answer("Останавливаю…")
    if mail_task:
        try:
            await asyncio.wait_for(asyncio.shield(mail_task), timeout=5)
        except Exception:
            pass
    await edit(cb, mailing_text(), mailing_kb())


@admin.callback_query(F.data == "m:stats")
async def cb_stats(cb: CallbackQuery):
    b = InlineKeyboardBuilder()
    if runtime["errors"]:
        b.button(text="🧹 Убрать проблемные группы", callback_data="m:clean")
    b.button(text="⬅️ Назад", callback_data="m:refresh")
    b.adjust(1)
    await edit(cb, stats_text(), b.as_markup())
    await cb.answer()


@admin.callback_query(F.data == "m:clean")
async def cb_clean(cb: CallbackQuery):
    bad = set(runtime["errors"])
    S["groups"] = [g for g in S["groups"] if g not in bad]
    runtime["errors"].clear()
    await save_state()
    await edit(cb, mailing_text(), mailing_kb())
    await cb.answer(f"Убрано групп: {len(bad)}")


@admin.callback_query(F.data == "m:autostop")
async def cb_autostop(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.autostop)
    await cb.message.answer(
        "⏳ Через сколько <b>кругов</b> по всем выбранным группам остановить рассылку?\n"
        "Отправьте число. 0 — без ограничения.",
        reply_markup=cancel_kb(),
    )
    await cb.answer()


@admin.message(Form.autostop)
async def got_autostop(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        return await m.answer("Нужно целое число, например 3 (или 0).")
    S["autostop"] = int(m.text.strip())
    await save_state()
    await state.clear()
    await m.answer("✅ Автостоп: " + (f"{S['autostop']} кр." if S["autostop"] else "выключен (∞)"))


# ──────────────────────────── ХЕНДЛЕРЫ: ТЕКСТ ПОСТА ──────────────────────────
async def download(file_obj) -> io.BytesIO:
    bio = io.BytesIO()
    await bot.download(file_obj, destination=bio)
    bio.seek(0)
    return bio


@admin.message(Form.text)
async def got_post(m: Message, state: FSMContext):
    """Принимаем пост от пользователя и кладём его в Избранное аккаунта."""
    if not userbot:
        return await m.answer("Аккаунт не подключён.")
    caption = m.html_text if (m.text or m.caption) else ""
    hp = PMode.HTML
    try:
        if m.photo:
            bio = await download(m.photo[-1])
            bio.name = "photo.jpg"
            sent = await userbot.send_photo("me", bio, caption=caption, parse_mode=hp)
            kind = "photo"
        elif m.video:
            bio = await download(m.video)
            bio.name = m.video.file_name or "video.mp4"
            sent = await userbot.send_video("me", bio, caption=caption, parse_mode=hp)
            kind = "video"
        elif m.animation:
            bio = await download(m.animation)
            bio.name = m.animation.file_name or "animation.mp4"
            sent = await userbot.send_animation("me", bio, caption=caption, parse_mode=hp)
            kind = "animation"
        elif m.document:
            bio = await download(m.document)
            bio.name = m.document.file_name or "file"
            sent = await userbot.send_document("me", bio, caption=caption, parse_mode=hp)
            kind = "document"
        elif m.text:
            sent = await userbot.send_message("me", caption, parse_mode=hp)
            kind = "text"
        else:
            return await m.answer("Этот тип сообщения не поддерживается. Пришлите текст, фото, видео, файл или GIF.")
    except TelegramBadRequest as e:
        return await m.answer(f"Не удалось получить файл ({e.message}). Лимит для ботов — 20 МБ.")
    except Exception as e:
        log.exception("save post")
        return await m.answer(f"Ошибка сохранения: {type(e).__name__}")

    old = S["post_id"]
    S["post_id"], S["post_type"] = sent.id, kind
    await save_state()
    if old:
        try:
            await userbot.delete_messages("me", old)
        except Exception:
            pass
    await state.clear()
    await m.answer(f"✅ Сообщение сохранено (тип: {TYPE_NAMES[kind]}). Копия лежит в Избранном.")


# ──────────────────────────── ХЕНДЛЕРЫ: ИНТЕРВАЛ ─────────────────────────────
@admin.callback_query(F.data.startswith("i:"))
async def cb_interval(cb: CallbackQuery, state: FSMContext):
    arg = cb.data[2:]
    if arg == "custom":
        await state.set_state(Form.interval)
        await cb.message.answer("Введите интервал в минутах (от 0.5 до 1440), например 2 или 3.5:",
                                reply_markup=cancel_kb())
        return await cb.answer()
    if arg == "jitter":
        S["jitter"] = not S["jitter"]
    else:
        S["interval"] = float(arg)
    await save_state()
    await edit(cb, interval_text(), interval_kb())
    await cb.answer("Сохранено")


@admin.message(Form.interval)
async def got_interval(m: Message, state: FSMContext):
    try:
        v = float((m.text or "").replace(",", ".").strip())
        assert 0.5 <= v <= 1440
    except Exception:
        return await m.answer("Введите число от 0.5 до 1440, например 2 или 3.5")
    S["interval"] = v
    await save_state()
    await state.clear()
    await m.answer(f"✅ Интервал: {fmt_interval(v)}")


# ──────────────────────────── ХЕНДЛЕРЫ: ГРУППЫ ───────────────────────────────
@admin.callback_query(F.data == "g:menu")
async def cb_g_menu(cb: CallbackQuery):
    await edit(cb, f"👥 <b>Настройка групп</b>\nВыбрано: {len(S['groups'])} из {len(dialogs)}",
               groups_menu_kb())
    await cb.answer()


@admin.callback_query(F.data == "g:reload")
async def cb_g_reload(cb: CallbackQuery):
    await cb.answer("Загружаю список…")
    await refresh_dialogs()
    await edit(cb, f"👥 <b>Настройка групп</b>\nВыбрано: {len(S['groups'])} из {len(dialogs)}",
               groups_menu_kb())


@admin.callback_query(F.data.startswith("g:l:"))
async def cb_g_list(cb: CallbackQuery):
    if not dialogs:
        await refresh_dialogs()
    if not dialogs:
        return await cb.answer("Аккаунт не состоит ни в одной группе", show_alert=True)
    page = int(cb.data.split(":")[2])
    await edit(cb, "👥 Нажмите на группу, чтобы выбрать или снять выбор:", groups_list_kb(page))
    await cb.answer()


@admin.callback_query(F.data.startswith("g:t:"))
async def cb_g_toggle(cb: CallbackQuery):
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if cid in S["groups"]:
        S["groups"].remove(cid)
    else:
        S["groups"].append(cid)
    await save_state()
    await edit(cb, "👥 Нажмите на группу, чтобы выбрать или снять выбор:", groups_list_kb(int(page)))
    await cb.answer()


@admin.callback_query(F.data == "g:all")
async def cb_g_all(cb: CallbackQuery):
    if not dialogs:
        await refresh_dialogs()
    S["groups"] = [cid for cid, _ in dialogs]
    await save_state()
    await edit(cb, f"👥 <b>Настройка групп</b>\nВыбрано: {len(S['groups'])} из {len(dialogs)}",
               groups_menu_kb())
    await cb.answer("Выбраны все группы")


@admin.callback_query(F.data == "g:none")
async def cb_g_none(cb: CallbackQuery):
    S["groups"] = []
    await save_state()
    await edit(cb, f"👥 <b>Настройка групп</b>\nВыбрано: 0 из {len(dialogs)}", groups_menu_kb())
    await cb.answer("Выбор сброшен")


# ──────────────────────────── ХЕНДЛЕРЫ: ВХОД В АККАУНТ ───────────────────────
@admin.callback_query(F.data == "c:cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await drop_login()
    await cb.message.edit_text("Отменено.")
    await cb.answer()


@admin.callback_query(F.data == "p:add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Auth.phone)
    await cb.message.answer(
        "🔐 Введите номер телефона аккаунта в международном формате, например <code>+79991234567</code>.\n\n"
        "⚠️ Рассылка идёт от вашего личного аккаунта. Бот не читает переписку.",
        reply_markup=cancel_kb(),
    )
    await cb.answer()


@admin.message(Auth.phone)
async def got_phone(m: Message, state: FSMContext):
    phone = re.sub(r"[^\d+]", "", m.text or "")
    if not re.fullmatch(r"\+?\d{9,15}", phone):
        return await m.answer("Неверный формат. Пример: +79991234567")
    if not phone.startswith("+"):
        phone = "+" + phone
    await drop_login()
    wait = await m.answer("⏳ Отправляю код, подождите…")
    c = make_client()
    try:
        await c.connect()
        sent = await c.send_code(phone)
    except PhoneNumberInvalid:
        await c.disconnect()
        return await wait.edit_text("Неверный номер телефона. Попробуйте ещё раз.")
    except ApiIdInvalid:
        await c.disconnect()
        await state.clear()
        return await wait.edit_text("API_ID / API_HASH указаны неверно.")
    except FloodWait as e:
        await c.disconnect()
        await state.clear()
        return await wait.edit_text(f"Слишком много попыток. Подождите {e.value} с.")
    except Exception as e:
        await c.disconnect()
        await state.clear()
        return await wait.edit_text(f"Ошибка: {type(e).__name__}")
    login.update(client=c, phone=phone, hash=sent.phone_code_hash, code="")
    await state.set_state(Auth.code)
    await wait.edit_text(
        "✅ Код отправлен в приложение Telegram (чат «Telegram»).\n"
        "Введите его кнопками ниже и нажмите ✅.\n\nКод: <b>—</b>",
        reply_markup=numpad_kb(),
    )


async def finish_login(m: Message, state: FSMContext):
    """Вход успешен: сохраняем сессию и запускаем рабочий клиент."""
    global userbot
    c = login["client"]
    session = await c.export_session_string()
    await drop_login()
    await state.clear()
    userbot = make_client(session)
    await userbot.start()
    await after_connect()
    await m.answer(f"✅ Аккаунт добавлен: {html.escape(account_label)}", reply_markup=main_menu())
    await m.answer(
        "🔑 <b>Важно для Render.</b> Чтобы после перезапуска сервера не входить заново, "
        "скопируйте строку ниже в переменную окружения <code>SESSION_STRING</code> "
        "(Render → Environment) и затем <b>удалите это сообщение</b>.\n"
        "Никому её не показывайте — это полный доступ к аккаунту.\n\n"
        f"<code>{session}</code>"
    )


async def try_sign_in(m: Message, state: FSMContext):
    c = login["client"]
    try:
        await c.sign_in(login["phone"], login["hash"], login["code"])
    except SessionPasswordNeeded:
        await state.set_state(Auth.password)
        return await m.answer("🔒 Включена двухфакторная защита. Введите облачный пароль Telegram "
                              "(сообщение с паролем бот удалит):", reply_markup=cancel_kb())
    except PhoneCodeInvalid:
        login["code"] = ""
        return await m.answer("❌ Неверный код. Введите ещё раз.\n\nКод: <b>—</b>", reply_markup=numpad_kb())
    except PhoneCodeExpired:
        await drop_login()
        await state.clear()
        return await m.answer("Код истёк. Начните заново: 👤 Профили → ➕ Добавить аккаунт.")
    except Exception as e:
        await drop_login()
        await state.clear()
        return await m.answer(f"Ошибка входа: {type(e).__name__}")
    await finish_login(m, state)


@admin.callback_query(Auth.code, F.data.startswith("c:"))
async def cb_code(cb: CallbackQuery, state: FSMContext):
    arg = cb.data[2:]
    if "client" not in login:
        await state.clear()
        return await cb.answer("Сессия входа устарела, начните заново", show_alert=True)
    if arg == "del":
        login["code"] = login["code"][:-1]
    elif arg == "ok":
        if len(login["code"]) < 5:
            return await cb.answer("Введите весь код", show_alert=True)
        await cb.message.edit_text("⏳ Проверяем код, подождите…")
        await cb.answer()
        return await try_sign_in(cb.message, state)
    elif arg.isdigit() and len(login["code"]) < 6:
        login["code"] += arg
    shown = " ".join("●" * len(login["code"])) or "—"
    await edit(cb, "Введите код из Telegram кнопками ниже и нажмите ✅.\n\n"
                   f"Код: <b>{shown}</b>", numpad_kb())
    await cb.answer()


@admin.message(Auth.code)
async def got_code_text(m: Message, state: FSMContext):
    """Запасной вариант: код текстом (лучше в формате 1.2.3.4.5 — так Telegram его не аннулирует)."""
    digits = re.sub(r"\D", "", m.text or "")
    if len(digits) < 5:
        return await m.answer("Введите код кнопками выше или текстом через точки: 1.2.3.4.5")
    login["code"] = digits
    await try_sign_in(m, state)


@admin.message(Auth.password)
async def got_password(m: Message, state: FSMContext):
    pw = m.text or ""
    try:
        await m.delete()
    except Exception:
        pass
    try:
        await login["client"].check_password(pw)
    except PasswordHashInvalid:
        return await m.answer("❌ Неверный пароль. Попробуйте ещё раз.")
    except Exception as e:
        await drop_login()
        await state.clear()
        return await m.answer(f"Ошибка входа: {type(e).__name__}")
    await finish_login(m, state)


@admin.callback_query(F.data == "p:del")
async def cb_del(cb: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="Да, выйти", callback_data="p:del_ok")
    b.button(text="Нет", callback_data="x")
    await edit(cb, "Выйти из аккаунта? Рассылка будет остановлена, сессия завершена.", b.as_markup())
    await cb.answer()


@admin.callback_query(F.data == "p:del_ok")
async def cb_del_ok(cb: CallbackQuery):
    global userbot, account_label
    stop_event.set()
    S["running"] = False
    await save_state()
    try:
        await userbot.log_out()
    except Exception:
        pass
    userbot, account_label = None, ""
    await edit(cb, "Вы вышли из аккаунта. Не забудьте очистить SESSION_STRING в Render.")
    await cb.answer()


# ──────────────────────────── ЗАПАСНОЙ ХЕНДЛЕР ДЛЯ ЧУЖИХ ─────────────────────
@public.message()
async def stranger(m: Message):
    if ADMIN_ID == 0:
        await m.answer(f"Бот ещё не настроен. Ваш ID: <code>{m.from_user.id}</code> — "
                       "впишите его в переменную ADMIN_ID.")
    else:
        await m.answer("⛔ Доступ закрыт.")


# ──────────────────────────── ВЕБ-СЕРВЕР ДЛЯ RENDER ──────────────────────────
async def health(_):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Веб-сервер на порту %s", PORT)


async def keep_alive():
    """Render Free усыпляет сервис через 15 мин без входящих запросов — пингуем сами себя."""
    if not SELF_URL:
        return
    async with ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                await s.get(SELF_URL + "/health", timeout=20)
            except Exception as e:
                log.warning("keep_alive: %s", e)


# ──────────────────────────── ЗАПУСК ─────────────────────────────────────────
async def main():
    global userbot
    dp.include_router(admin)
    dp.include_router(public)
    await start_web()
    asyncio.create_task(keep_alive())

    if SESSION_STRING:
        try:
            userbot = make_client(SESSION_STRING)
            await userbot.start()
            await after_connect()
        except Exception as e:
            log.error("Не удалось запустить аккаунт: %s", e)
            userbot = None
            await notify(f"⚠️ Не удалось запустить аккаунт ({type(e).__name__}). "
                         "Добавьте его заново: 👤 Профили.")
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
