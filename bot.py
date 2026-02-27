import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from max_manager import MaxSessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден. Добавь его в .env")

CHAT_PAGE_SIZE = 8
HISTORY_PAGE_SIZE = 10
CHAT_CACHE_TTL_SECONDS = 30
TOKEN_REGEX = re.compile(r"An_[A-Za-z0-9._\\-]+")

HISTORY_ANCHORS: dict[tuple[int, int], int] = {}
CHAT_CACHE: dict[int, tuple[float, list["ChatEntry"]]] = {}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
session_manager = MaxSessionManager(db_path="sessions/users.db")


class UserFlow(StatesGroup):
    waiting_for_token = State()
    waiting_for_chat_message = State()


@dataclass
class ChatEntry:
    chat_id: int
    title: str
    chat_type: str
    last_event_time: int


def esc(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_int(raw: str, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def normalize_token_input(raw: str) -> str | None:
    source = (raw or "").strip()
    if not source:
        return None

    if source.lower().startswith("bearer "):
        source = source[7:].strip()

    if len(source) >= 2 and source[0] == source[-1] and source[0] in {"'", '"', "`"}:
        source = source[1:-1].strip()

    try:
        payload = json.loads(source)
        if isinstance(payload, dict):
            for key in ("token", "user_token", "auth_token", "access_token"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    source = value.strip()
                    break
    except json.JSONDecodeError:
        pass

    kv_match = re.search(
        r"(?:token|user_token|auth_token|access_token)\s*[:=]\s*[\"']?([^\"'\s,}]+)",
        source,
        flags=re.IGNORECASE,
    )
    if kv_match:
        source = kv_match.group(1).strip()

    token_match = TOKEN_REGEX.search(source)
    if token_match:
        return token_match.group(0).strip()

    return source.strip() or None


def normalize_chat_type(chat_type: Any) -> str:
    value = getattr(chat_type, "value", chat_type)
    return str(value or "CHAT")


def chat_type_icon(chat_type: str) -> str:
    upper = chat_type.upper()
    if upper == "DIALOG":
        return "👤"
    if upper == "CHANNEL":
        return "📣"
    return "👥"


def user_display_name(user: Any, fallback: str) -> str:
    if not user:
        return fallback

    names = getattr(user, "names", None) or []
    for name in names:
        plain = getattr(name, "name", None)
        if plain:
            return str(plain)
        first = getattr(name, "first_name", None)
        last = getattr(name, "last_name", None)
        composed = " ".join(part for part in [first, last] if part).strip()
        if composed:
            return composed

    return fallback


def time_label(unix_ms: int | None) -> str:
    if not unix_ms:
        return "--:--"
    return datetime.fromtimestamp(unix_ms / 1000).strftime("%d.%m %H:%M")


def short_title(title: str, max_len: int = 32) -> str:
    clean = title.replace("\n", " ").strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def main_menu_keyboard(has_token: bool) -> InlineKeyboardMarkup:
    token_text = "🔑 Обновить MAX токен" if has_token else "🔑 Подключить MAX токен"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=token_text, callback_data="token:set")],
        [InlineKeyboardButton(text="💬 Мои чаты", callback_data="chats:0")],
    ]
    if has_token:
        rows.insert(0, [InlineKeyboardButton(text="✅ MAX подключен", callback_data="token:info")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def token_help_text() -> str:
    return (
        "<b>Подключение MAX</b>\n"
        "\n"
        "1. Открой web-версию MAX в браузере.\n"
        "2. Нажми <b>F12</b> и перейди в <b>Application → Local Storage</b>.\n"
        "3. Найди значение токена (<code>token</code> или <code>user_token</code>).\n"
        "4. Отправь токен сюда одним сообщением.\n"
        "\n"
        "<i>Токен сохранится только для твоего Telegram ID.</i>"
    )


def build_chats_keyboard(entries: list[ChatEntry], page: int) -> tuple[InlineKeyboardMarkup, int, int]:
    total_pages = max(1, (len(entries) + CHAT_PAGE_SIZE - 1) // CHAT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CHAT_PAGE_SIZE
    page_items = entries[start : start + CHAT_PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for item in page_items:
        label = f"{chat_type_icon(item.chat_type)} {short_title(item.title)}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"chat:{item.chat_id}:0:{page}",
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"chats:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"chats:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), page, total_pages


def build_history_keyboard(
    chat_id: int,
    offset: int,
    chat_page: int,
    has_more: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if has_more:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Ещё 10",
                    callback_data=f"chat:{chat_id}:{offset + HISTORY_PAGE_SIZE}:{chat_page}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="✍️ Написать в чат", callback_data=f"write:{chat_id}:{chat_page}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к чатам", callback_data=f"chats:{chat_page}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit_message(
    message: types.Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.answer(text=text, reply_markup=reply_markup)


def remember_user(user: types.User) -> None:
    session_manager.register_telegram_user(
        tg_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def get_chat_entries(
    tg_user_id: int,
    client: Any,
    force_refresh: bool = False,
) -> list[ChatEntry]:
    cached = CHAT_CACHE.get(tg_user_id)
    if (
        not force_refresh
        and cached
        and time.time() - cached[0] <= CHAT_CACHE_TTL_SECONDS
    ):
        return cached[1]

    raw_map: dict[int, dict[str, Any]] = {}

    for dialog in client.dialogs:
        participant_ids: list[int] = []
        for key in (dialog.participants or {}).keys():
            try:
                participant_ids.append(int(key))
            except (TypeError, ValueError):
                continue

        raw_map[dialog.id] = {
            "chat_id": dialog.id,
            "title": None,
            "chat_type": normalize_chat_type(dialog.type),
            "last_event_time": int(getattr(dialog, "last_event_time", 0) or 0),
            "participants": participant_ids,
        }

    for chat in [*client.chats, *client.channels]:
        raw_map[chat.id] = {
            "chat_id": chat.id,
            "title": chat.title,
            "chat_type": normalize_chat_type(chat.type),
            "last_event_time": int(getattr(chat, "last_event_time", 0) or 0),
            "participants": list((chat.participants or {}).keys()),
        }

    try:
        for chat in await client.fetch_chats():
            raw_map[chat.id] = {
                "chat_id": chat.id,
                "title": chat.title,
                "chat_type": normalize_chat_type(chat.type),
                "last_event_time": int(getattr(chat, "last_event_time", 0) or 0),
                "participants": list((chat.participants or {}).keys()),
            }
    except Exception as exc:
        logger.warning("Could not refresh chat list for %s: %s", tg_user_id, exc)

    me_id = client.me.id if client.me else None
    need_user_ids: set[int] = set()

    for item in raw_map.values():
        title = item.get("title")
        if title:
            continue

        peer_id = None
        for pid in item.get("participants") or []:
            if me_id is None or pid != me_id:
                peer_id = pid
                break
        item["peer_id"] = peer_id
        if peer_id is not None:
            need_user_ids.add(peer_id)

    users_map: dict[int, Any] = {}
    if need_user_ids:
        try:
            users = await client.get_users(sorted(need_user_ids))
            users_map = {user.id: user for user in users}
        except Exception as exc:
            logger.warning("Failed to resolve dialog names for %s: %s", tg_user_id, exc)

    entries: list[ChatEntry] = []
    for item in raw_map.values():
        title = item.get("title")
        if not title:
            peer_id = item.get("peer_id")
            if peer_id is not None:
                title = user_display_name(users_map.get(peer_id), f"Пользователь {peer_id}")
            else:
                title = f"Чат {item['chat_id']}"

        entries.append(
            ChatEntry(
                chat_id=item["chat_id"],
                title=str(title),
                chat_type=str(item.get("chat_type") or "CHAT"),
                last_event_time=int(item.get("last_event_time") or 0),
            )
        )

    entries.sort(key=lambda entry: entry.last_event_time, reverse=True)
    CHAT_CACHE[tg_user_id] = (time.time(), entries)
    return entries


def resolve_chat_title(chat_id: int, entries: list[ChatEntry]) -> str:
    for item in entries:
        if item.chat_id == chat_id:
            return item.title
    return f"Чат {chat_id}"


async def build_history_text(
    tg_user_id: int,
    client: Any,
    chat_id: int,
    offset: int,
    chat_page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    anchor_key = (tg_user_id, chat_id)
    if offset == 0 or anchor_key not in HISTORY_ANCHORS:
        HISTORY_ANCHORS[anchor_key] = now_ms()
    anchor = HISTORY_ANCHORS[anchor_key]

    requested = offset + HISTORY_PAGE_SIZE + 1
    history = await client.fetch_history(chat_id=chat_id, from_time=anchor, backward=requested)
    messages = history or []

    unique_messages = {msg.id: msg for msg in messages}
    ordered = sorted(unique_messages.values(), key=lambda msg: msg.time, reverse=True)

    page_messages = ordered[offset : offset + HISTORY_PAGE_SIZE]
    has_more = len(ordered) > offset + HISTORY_PAGE_SIZE

    entries = await get_chat_entries(tg_user_id, client)
    chat_title = resolve_chat_title(chat_id, entries)

    sender_ids: set[int] = set()
    for msg in page_messages:
        if msg.sender is not None:
            sender_ids.add(int(msg.sender))
        if msg.link and msg.link.message and msg.link.message.sender is not None:
            sender_ids.add(int(msg.link.message.sender))

    users_map: dict[int, Any] = {}
    if sender_ids:
        try:
            users = await client.get_users(sorted(sender_ids))
            users_map = {user.id: user for user in users}
        except Exception as exc:
            logger.warning("Could not load senders for chat %s: %s", chat_id, exc)

    rendered_blocks: list[str] = []
    separator = "\n\n────────────\n\n"

    for msg in page_messages:
        sender_name = user_display_name(users_map.get(msg.sender), f"Пользователь {msg.sender}")

        body = (msg.text or "").strip()
        if not body and msg.attaches:
            body = "[Вложение]"
        if not body:
            body = "[Пустое сообщение]"

        forward_line = ""
        if msg.link and msg.link.message:
            linked_sender_id = msg.link.message.sender
            linked_sender = user_display_name(
                users_map.get(linked_sender_id),
                f"Пользователь {linked_sender_id}",
            )
            link_type = (msg.link.type or "").upper()
            if "FORWARD" in link_type:
                forward_line = f"\n↪️ <i>Переслано от {esc(linked_sender)}</i>"
            elif link_type:
                forward_line = f"\n↪️ <i>{esc(link_type)} от {esc(linked_sender)}</i>"

        block = (
            f"<b>{esc(sender_name)}</b> <code>{time_label(msg.time)}</code>\n"
            f"{esc(body)}{forward_line}"
        )
        rendered_blocks.append(block)

    body_parts: list[str] = []
    body_length = 0
    for block in rendered_blocks:
        extra = len(block) + (len(separator) if body_parts else 0)
        if body_length + extra > 3600:
            body_parts.append("<i>… часть длинного текста скрыта</i>")
            break
        if body_parts:
            body_parts.append(separator)
            body_length += len(separator)
        body_parts.append(block)
        body_length += len(block)

    if not page_messages:
        content = (
            f"💬 <b>{esc(chat_title)}</b>\n"
            f"<i>В этом чате пока нет сообщений.</i>"
        )
    else:
        start_no = offset + 1
        end_no = offset + len(page_messages)
        content = (
            f"💬 <b>{esc(chat_title)}</b>\n"
            f"<i>Сообщения {start_no}-{end_no}</i>\n\n"
            f"{''.join(body_parts)}"
        )

    keyboard = build_history_keyboard(
        chat_id=chat_id,
        offset=offset,
        chat_page=chat_page,
        has_more=has_more,
    )
    return content, keyboard


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()

    has_token = session_manager.has_token(message.from_user.id)
    name = message.from_user.first_name or "друг"
    token_status = "подключен ✅" if has_token else "не подключен ❌"

    text = (
        f"<b>MAX Bridge</b>\n"
        f"Привет, <b>{esc(name)}</b>.\n\n"
        f"Текущий статус MAX: <b>{token_status}</b>.\n"
        f"Выбери действие в меню ниже."
    )
    await message.answer(text, reply_markup=main_menu_keyboard(has_token))


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()
    has_token = session_manager.has_token(message.from_user.id)
    await message.answer("<b>Главное меню</b>", reply_markup=main_menu_keyboard(has_token))


@dp.message(Command("login"))
async def cmd_login(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.set_state(UserFlow.waiting_for_token)
    await message.answer(token_help_text())


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()
    has_token = session_manager.has_token(message.from_user.id)
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard(has_token))


@dp.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await state.clear()
    has_token = session_manager.has_token(callback.from_user.id)
    await safe_edit_message(callback.message, "<b>Главное меню</b>", main_menu_keyboard(has_token))
    await callback.answer()


@dp.callback_query(F.data == "token:info")
async def cb_token_info(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    has_token = session_manager.has_token(callback.from_user.id)
    text = "MAX токен подключен ✅" if has_token else "MAX токен не подключен ❌"
    await callback.answer(text=text, show_alert=True)


@dp.callback_query(F.data == "token:set")
async def cb_token_set(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await state.set_state(UserFlow.waiting_for_token)
    await callback.message.answer(token_help_text())
    await callback.answer()


@dp.message(UserFlow.waiting_for_token, F.text)
async def input_token(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    token = normalize_token_input(message.text or "")

    if not token or len(token) < 20:
        await message.answer("Не получилось распознать токен. Отправь только значение токена одной строкой.")
        return

    wait_message = await message.answer("Проверяю токен и подключаюсь к MAX…")

    try:
        await session_manager.validate_and_save_token(message.from_user.id, token)
        CHAT_CACHE.pop(message.from_user.id, None)
        await state.clear()
        await wait_message.edit_text(
            "✅ MAX токен сохранен. Теперь можно открыть список чатов.",
            reply_markup=main_menu_keyboard(True),
        )
    except Exception as exc:
        logger.warning("Token save failed for user %s: %s", message.from_user.id, exc)
        await wait_message.edit_text(
            f"❌ {esc(exc)}\n\n"
            "Проверь формат токена и попробуй снова, или используй /cancel.",
        )


@dp.callback_query(F.data.startswith("chats:"))
async def cb_chats(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    page = parse_int((callback.data or "").split(":", maxsplit=1)[1], default=0)

    if not session_manager.has_token(callback.from_user.id):
        await callback.answer("Сначала подключи MAX токен", show_alert=True)
        await callback.message.answer(token_help_text())
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        entries = await get_chat_entries(
            callback.from_user.id,
            client,
            force_refresh=(page == 0),
        )

        if not entries:
            await safe_edit_message(
                callback.message,
                "💬 Чаты пока не найдены.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
                    ]
                ),
            )
            await callback.answer()
            return

        keyboard, current_page, total_pages = build_chats_keyboard(entries, page)
        text = (
            "<b>Твои чаты в MAX</b>\n"
            f"Страница <b>{current_page + 1}/{total_pages}</b>\n"
            "Нажми на чат, чтобы открыть последние сообщения."
        )
        await safe_edit_message(callback.message, text, keyboard)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to load chats for user %s", callback.from_user.id)
        await callback.answer("Ошибка загрузки чатов", show_alert=True)
        await callback.message.answer(f"Не удалось загрузить чаты: <code>{esc(exc)}</code>")


@dp.callback_query(F.data.startswith("chat:"))
async def cb_chat(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[1], default=0)
    offset = max(0, parse_int(parts[2], default=0))
    chat_page = max(0, parse_int(parts[3], default=0))

    if chat_id == 0:
        await callback.answer("Чат не найден", show_alert=True)
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        text, keyboard = await build_history_text(
            tg_user_id=callback.from_user.id,
            client=client,
            chat_id=chat_id,
            offset=offset,
            chat_page=chat_page,
        )
        await safe_edit_message(callback.message, text, keyboard)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open chat %s for user %s", chat_id, callback.from_user.id)
        await callback.answer("Ошибка загрузки сообщений", show_alert=True)
        await callback.message.answer(f"Не удалось получить сообщения: <code>{esc(exc)}</code>")


@dp.callback_query(F.data.startswith("write:"))
async def cb_write(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[1], default=0)
    chat_page = max(0, parse_int(parts[2], default=0))

    if chat_id == 0:
        await callback.answer("Чат не найден", show_alert=True)
        return

    await state.set_state(UserFlow.waiting_for_chat_message)
    await state.update_data(chat_id=chat_id, chat_page=chat_page)

    await callback.message.answer(
        "✍️ Отправь текст сообщения одним сообщением.\n"
        "Для отмены используй /cancel."
    )
    await callback.answer()


@dp.message(UserFlow.waiting_for_chat_message, F.text)
async def send_message_to_chat(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    data = await state.get_data()

    chat_id = parse_int(str(data.get("chat_id", "0")), default=0)
    chat_page = max(0, parse_int(str(data.get("chat_page", "0")), default=0))

    if chat_id == 0:
        await state.clear()
        await message.answer("Не удалось определить чат. Открой чат заново через меню.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение отправить нельзя.")
        return

    try:
        client = await session_manager.ensure_client(message.from_user.id)
        await client.send_message(text=text, chat_id=chat_id)
        await state.clear()

        HISTORY_ANCHORS.pop((message.from_user.id, chat_id), None)

        ack_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Обновить чат",
                        callback_data=f"chat:{chat_id}:0:{chat_page}",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ К чатам", callback_data=f"chats:{chat_page}")],
            ]
        )
        await message.answer("✅ Сообщение отправлено.", reply_markup=ack_keyboard)
    except Exception as exc:
        logger.exception("Failed to send message for user %s", message.from_user.id)
        await message.answer(
            f"❌ Не удалось отправить сообщение: <code>{esc(exc)}</code>\n"
            "Попробуй снова или /cancel."
        )


@dp.message(F.text & ~F.text.startswith("/"))
async def fallback_text(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    if await state.get_state() is not None:
        return

    has_token = session_manager.has_token(message.from_user.id)
    await message.answer(
        "Используй кнопки меню для управления чатами MAX.",
        reply_markup=main_menu_keyboard(has_token),
    )


async def main() -> None:
    logger.info("Bot is starting")
    os.makedirs("sessions", exist_ok=True)

    try:
        await dp.start_polling(bot)
    finally:
        await session_manager.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
