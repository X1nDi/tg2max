import asyncio
import contextlib
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

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
MEMBERS_PAGE_SIZE = 8
CHAT_CACHE_TTL_SECONDS = 30
MEDIA_LINK_TTL_SECONDS = 1800
UPDATE_POLL_CHAT_LIMIT = 12
UPDATE_HISTORY_BACKWARD = 20
TOKEN_REGEX = re.compile(r"An_[A-Za-z0-9._\\-]+")
PHONE_INPUT_REGEX = re.compile(r"^\+?\d{10,15}$")
MEDIA_CMD_REGEX = re.compile(r"^/media_([A-Za-z0-9]+)$")

try:
    UPDATE_POLL_SECONDS = max(3, int(os.getenv("UPDATE_POLL_SECONDS", "10").strip()))
except Exception:
    UPDATE_POLL_SECONDS = 10

BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

HISTORY_ANCHORS: dict[tuple[int, int], int] = {}
CHAT_CACHE: dict[int, tuple[float, list["ChatEntry"]]] = {}
MEDIA_CACHE: dict[str, "MediaRequest"] = {}
UPDATE_LAST_SEEN: dict[tuple[int, int], int] = {}
UPDATE_TASK: asyncio.Task | None = None

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True),
)
dp = Dispatcher()
session_manager = MaxSessionManager(db_path="sessions/users.db")


class UserFlow(StatesGroup):
    waiting_for_token = State()
    waiting_for_auth_phone = State()
    waiting_for_auth_code = State()
    waiting_for_chat_message = State()


@dataclass
class ChatEntry:
    chat_id: int
    title: str
    chat_type: str
    last_event_time: int
    participants: list[int] = field(default_factory=list)


@dataclass
class MediaRequest:
    tg_user_id: int
    chat_id: int
    message_id: int
    kind: str
    file_id: int | None = None
    video_id: int | None = None
    url: str | None = None
    name: str | None = None
    created_at: float = field(default_factory=time.time)


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


def normalize_phone_input(raw: str) -> str | None:
    source = (raw or "").strip()
    if not source:
        return None

    digits = re.sub(r"\D", "", source)
    if digits.startswith("8") and len(digits) == 11:
        digits = f"7{digits[1:]}"

    phone = f"+{digits}" if digits else ""
    if not PHONE_INPUT_REGEX.match(phone):
        return None
    return phone


def mask_phone(phone: str) -> str:
    clean = (phone or "").strip()
    if len(clean) <= 5:
        return clean
    return f"{clean[:3]}***{clean[-2:]}"


def make_link(url: str, label: str) -> str:
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def cleanup_media_cache() -> None:
    now = time.time()
    stale_keys = [
        token
        for token, item in MEDIA_CACHE.items()
        if now - item.created_at > MEDIA_LINK_TTL_SECONDS
    ]
    for token in stale_keys:
        MEDIA_CACHE.pop(token, None)


def register_media_request(
    tg_user_id: int,
    chat_id: int,
    message_id: int,
    kind: str,
    *,
    file_id: int | None = None,
    video_id: int | None = None,
    url: str | None = None,
    name: str | None = None,
) -> str:
    cleanup_media_cache()
    token = secrets.token_urlsafe(8).replace("_", "").replace("-", "")[:12]
    MEDIA_CACHE[token] = MediaRequest(
        tg_user_id=tg_user_id,
        chat_id=chat_id,
        message_id=message_id,
        kind=kind,
        file_id=file_id,
        video_id=video_id,
        url=url,
        name=name,
    )
    return token


def media_command_markup(token: str) -> str:
    command = f"/media_{token}"
    if BOT_USERNAME:
        return make_link(f"https://t.me/{BOT_USERNAME}?start=media_{token}", "получить в боте")
    return f"<code>{esc(command)}</code>"


def _filename_from_url(url: str, fallback: str) -> str:
    try:
        path = urlparse(url).path or ""
        name = os.path.basename(path)
        if name:
            return name
    except Exception:
        pass
    return fallback


def _download_url_to_path(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=45) as response:
        response.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)


async def download_media_to_temp(url: str, filename_hint: str) -> str:
    temp_dir = os.path.join("sessions", "tmp_media")
    os.makedirs(temp_dir, exist_ok=True)
    safe_hint = re.sub(r"[^A-Za-z0-9._-]", "_", filename_hint).strip("._") or "media.bin"
    path = os.path.join(temp_dir, f"{int(time.time())}_{secrets.token_hex(4)}_{safe_hint}")
    await asyncio.to_thread(_download_url_to_path, url, path)
    return path


def is_telegram_url_fetch_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return "failed to get http url content" in lowered


async def ensure_bot_username() -> str:
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = (me.username or "").strip().lstrip("@")
    return BOT_USERNAME


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
        first = getattr(name, "first_name", None)
        last = getattr(name, "last_name", None)
        composed = " ".join(part for part in [first, last] if part).strip()
        if composed:
            return composed
        plain = getattr(name, "name", None)
        if plain:
            return str(plain)

    first = getattr(user, "first_name", None)
    last = getattr(user, "last_name", None)
    composed = " ".join(part for part in [first, last] if part).strip()
    if composed:
        return composed

    plain = getattr(user, "name", None)
    if plain:
        return str(plain)

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
        [InlineKeyboardButton(text="🔐 Выбрать способ входа", callback_data="auth:menu")],
        [InlineKeyboardButton(text="💬 Мои чаты", callback_data="chats:0")],
    ]
    if has_token:
        rows.insert(0, [InlineKeyboardButton(text="✅ MAX подключен", callback_data="token:info")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def auth_methods_keyboard(has_token: bool) -> InlineKeyboardMarkup:
    token_text = "🔑 Обновить MAX токен" if has_token else "🔑 Войти по токену"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=token_text, callback_data="auth:token")],
        [InlineKeyboardButton(text="📱 Войти по телефону", callback_data="auth:phone")],
        [InlineKeyboardButton(text="🧩 Войти через QR", callback_data="auth:qr")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_flow_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отменить", callback_data="flow:cancel")]]
    )


def dismiss_message_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👌 Окей", callback_data="msg:close")]]
    )


def auth_menu_text(has_token: bool) -> str:
    status = "подключен ✅" if has_token else "не подключен ❌"
    return (
        "<b>Авторизация MAX</b>\n"
        f"Текущий статус: <b>{status}</b>\n\n"
        "Выбери способ входа:\n"
        "• токен\n"
        "• телефон + код\n"
        "• QR-код"
    )


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


def phone_help_text() -> str:
    return (
        "<b>Вход по телефону</b>\n\n"
        "Отправь номер в международном формате:\n"
        "<code>+79991234567</code>\n\n"
        "<i>После этого бот запросит код подтверждения из MAX.</i>"
    )


def qr_help_text(qr_link: str, expires_at: int) -> str:
    expires = datetime.fromtimestamp(expires_at / 1000).strftime("%H:%M:%S")
    return (
        "<b>Вход по QR</b>\n\n"
        "1. Открой ссылку с QR-кодом.\n"
        "2. Отсканируй QR в MAX.\n"
        "3. Нажми «Проверить QR».\n\n"
        f"QR действует до <b>{esc(expires)}</b>.\n"
        f"Ссылка: {make_link(qr_link, 'Открыть QR-код')}"
    )


def qr_auth_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить QR", callback_data="auth:qr:check")],
            [InlineKeyboardButton(text="🔁 Обновить QR", callback_data="auth:qr:refresh")],
            [InlineKeyboardButton(text="⬅️ К способам входа", callback_data="auth:menu")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def build_members_keyboard(
    members: list[tuple[int, str]],
    chat_id: int,
    offset: int,
    chat_page: int,
    page: int,
) -> tuple[InlineKeyboardMarkup, int, int]:
    total_pages = max(1, (len(members) + MEMBERS_PAGE_SIZE - 1) // MEMBERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * MEMBERS_PAGE_SIZE
    page_items = members[start : start + MEMBERS_PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for user_id, name in page_items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {short_title(name, max_len=28)}",
                    callback_data=f"member:{chat_id}:{offset}:{chat_page}:{user_id}:{page}",
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"members:{chat_id}:{offset}:{chat_page}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"members:{chat_id}:{offset}:{chat_page}:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ К чату", callback_data=f"chat:{chat_id}:{offset}:{chat_page}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), page, total_pages


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
    has_older: bool,
    has_newer: bool,
    show_members: bool,
    profile_user_id: int | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    nav: list[InlineKeyboardButton] = []
    if has_older:
        nav.append(
            InlineKeyboardButton(
                text="⬆️ Старее 10",
                callback_data=f"chat:{chat_id}:{offset + HISTORY_PAGE_SIZE}:{chat_page}",
            )
        )
    if has_newer:
        nav.append(
            InlineKeyboardButton(
                text="⬇️ Новее 10",
                callback_data=f"chat:{chat_id}:{max(0, offset - HISTORY_PAGE_SIZE)}:{chat_page}",
            )
        )
    if nav:
        rows.append(nav)

    if show_members:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👥 Список участников",
                    callback_data=f"members:{chat_id}:{offset}:{chat_page}:0",
                )
            ]
        )
    if profile_user_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👤 Профиль",
                    callback_data=f"profile:{chat_id}:{offset}:{chat_page}:{profile_user_id}",
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
        participant_ids: list[int] = []
        for key in (chat.participants or {}).keys():
            try:
                participant_ids.append(int(key))
            except (TypeError, ValueError):
                continue
        raw_map[chat.id] = {
            "chat_id": chat.id,
            "title": chat.title,
            "chat_type": normalize_chat_type(chat.type),
            "last_event_time": int(getattr(chat, "last_event_time", 0) or 0),
            "participants": participant_ids,
        }

    try:
        for chat in await client.fetch_chats():
            participant_ids: list[int] = []
            for key in (chat.participants or {}).keys():
                try:
                    participant_ids.append(int(key))
                except (TypeError, ValueError):
                    continue
            raw_map[chat.id] = {
                "chat_id": chat.id,
                "title": chat.title,
                "chat_type": normalize_chat_type(chat.type),
                "last_event_time": int(getattr(chat, "last_event_time", 0) or 0),
                "participants": participant_ids,
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
                participants=list(item.get("participants") or []),
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


def resolve_chat_entry(chat_id: int, entries: list[ChatEntry]) -> ChatEntry | None:
    for item in entries:
        if item.chat_id == chat_id:
            return item
    return None


def render_user_profile_text(user: Any, user_id: int) -> tuple[str, str]:
    name = user_display_name(user, f"Пользователь {user_id}")
    username = str(getattr(user, "username", "") or "").strip()
    link = str(getattr(user, "link", "") or "").strip()
    description = str(getattr(user, "description", "") or "").strip()
    phone = str(getattr(user, "phone", "") or "").strip()
    avatar_url = str(getattr(user, "base_url", "") or "").strip()
    avatar_raw = str(getattr(user, "base_raw_url", "") or "").strip()
    avatar = avatar_url or avatar_raw

    lines = [
        "<b>Профиль участника</b>",
        f"Имя: <b>{esc(name)}</b>",
        f"MAX ID: <code>{esc(user_id)}</code>",
    ]
    if description:
        lines.append(f"Описание: {esc(description)}")
    if phone:
        lines.append(f"Номер: <code>{esc(phone)}</code>")
    else:
        lines.append("Номер: <i>скрыт</i>")
    if username:
        lines.append(f"Username: <code>{esc(username)}</code>")
    if link.startswith(("http://", "https://")):
        lines.append(f"Профиль в MAX: {make_link(link, 'открыть')}")

    return "\n".join(lines), avatar


async def show_profile_card(
    source_message: types.Message,
    text: str,
    keyboard: InlineKeyboardMarkup,
    avatar: str,
) -> None:
    if avatar.startswith(("http://", "https://")):
        try:
            await source_message.answer_photo(photo=avatar, caption=text, reply_markup=keyboard)
            with contextlib.suppress(Exception):
                await source_message.delete()
            return
        except TelegramBadRequest as exc:
            if not is_telegram_url_fetch_error(exc):
                logger.warning("Profile avatar URL failed: %s", exc)
            else:
                file_name = _filename_from_url(avatar, "avatar.jpg")
                path = await download_media_to_temp(avatar, file_name)
                try:
                    await source_message.answer_photo(
                        photo=FSInputFile(path),
                        caption=text,
                        reply_markup=keyboard,
                    )
                    with contextlib.suppress(Exception):
                        await source_message.delete()
                    return
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)

    await safe_edit_message(source_message, text, keyboard)


async def render_attachment_lines(
    tg_user_id: int,
    client: Any,
    chat_id: int,
    message: Any,
) -> list[str]:
    lines: list[str] = []
    attaches = getattr(message, "attaches", None) or []
    message_id = parse_int(str(getattr(message, "id", "0")), default=0)

    for attach in attaches:
        raw_type = getattr(getattr(attach, "type", None), "value", getattr(attach, "type", ""))
        attach_type = str(raw_type or "").upper()

        if attach_type == "PHOTO":
            photo_url = getattr(attach, "base_url", None)
            media_token = register_media_request(
                tg_user_id=tg_user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind="PHOTO",
                url=str(photo_url) if isinstance(photo_url, str) else None,
            )
            lines.append(f"📷 Фото: {media_command_markup(media_token)}")
            continue

        if attach_type == "FILE":
            file_name = str(getattr(attach, "name", None) or f"file_{getattr(attach, 'file_id', '')}")
            file_id = getattr(attach, "file_id", None)
            media_token = register_media_request(
                tg_user_id=tg_user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind="FILE",
                file_id=parse_int(str(file_id), default=0) or None,
                name=file_name,
            )
            lines.append(f"📎 Файл {esc(file_name)}: {media_command_markup(media_token)}")
            continue

        if attach_type == "VIDEO":
            video_id = getattr(attach, "video_id", None)
            media_token = register_media_request(
                tg_user_id=tg_user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind="VIDEO",
                video_id=parse_int(str(video_id), default=0) or None,
            )
            lines.append(f"🎬 Видео: {media_command_markup(media_token)}")
            continue

        if attach_type == "AUDIO":
            audio_url = getattr(attach, "url", None)
            media_token = register_media_request(
                tg_user_id=tg_user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind="AUDIO",
                url=str(audio_url) if isinstance(audio_url, str) else None,
            )
            if isinstance(audio_url, str):
                lines.append(f"🎵 Аудио: {media_command_markup(media_token)}")
            else:
                lines.append("🎵 Аудио")
            continue

        if attach_type == "STICKER":
            sticker_url = getattr(attach, "url", None)
            media_token = register_media_request(
                tg_user_id=tg_user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind="STICKER",
                url=str(sticker_url) if isinstance(sticker_url, str) else None,
            )
            if isinstance(sticker_url, str):
                lines.append(f"😀 Стикер: {media_command_markup(media_token)}")
            else:
                lines.append("😀 Стикер")
            continue

        if attach_type == "CONTACT":
            contact_name = str(getattr(attach, "name", "")).strip()
            if not contact_name:
                first = str(getattr(attach, "first_name", "")).strip()
                last = str(getattr(attach, "last_name", "")).strip()
                contact_name = " ".join(part for part in [first, last] if part) or "без имени"
            lines.append(f"👤 Контакт: {esc(contact_name)}")
            continue

        if attach_type == "CONTROL":
            event = str(getattr(attach, "event", "")).strip()
            lines.append(f"⚙️ Сервисное сообщение: {esc(event or 'CONTROL')}")
            continue

        lines.append(f"📦 Вложение: {esc(attach_type or 'UNKNOWN')}")

    return lines


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
    ordered = sorted(unique_messages.values(), key=lambda msg: msg.time)
    total_messages = len(ordered)

    end_index = max(0, total_messages - offset)
    start_index = max(0, end_index - HISTORY_PAGE_SIZE)
    page_messages = ordered[start_index:end_index]
    has_more = start_index > 0
    has_newer = offset > 0

    entries = await get_chat_entries(tg_user_id, client)
    chat_title = resolve_chat_title(chat_id, entries)
    chat_entry = resolve_chat_entry(chat_id, entries)
    show_members = bool(
        chat_entry and chat_entry.chat_type.upper() == "CHAT" and chat_entry.participants
    )
    profile_user_id: int | None = None
    if chat_entry and chat_entry.chat_type.upper() == "DIALOG":
        me_id = parse_int(str(getattr(client.me, "id", 0)), default=0)
        for pid in chat_entry.participants:
            if pid != me_id:
                profile_user_id = pid
                break

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
    for msg in page_messages:
        sender_name = user_display_name(users_map.get(msg.sender), f"Пользователь {msg.sender}")
        attachment_lines = await render_attachment_lines(tg_user_id, client, chat_id, msg)
        body = (msg.text or "").strip()

        block_lines = [f"<b>{esc(sender_name)}</b> <code>{time_label(msg.time)}</code>"]
        if body:
            block_lines.append(esc(body))
        if attachment_lines:
            block_lines.append("\n".join(attachment_lines))

        if msg.link and msg.link.message:
            linked_sender_id = msg.link.message.sender
            linked_sender = user_display_name(
                users_map.get(linked_sender_id),
                f"Пользователь {linked_sender_id}",
            )
            link_type = (msg.link.type or "").upper()
            if "FORWARD" in link_type:
                forward_header = f"Переслано от {linked_sender}"
            elif link_type:
                forward_header = f"{link_type} от {linked_sender}"
            else:
                forward_header = f"Связано с сообщением от {linked_sender}"

            block_lines.append(f"↪️ <i>{esc(forward_header)}</i>")

            linked_text = (msg.link.message.text or "").strip()
            if linked_text:
                block_lines.append(f"↳ {esc(linked_text)}")

            linked_chat_id = parse_int(str(getattr(msg.link, "chat_id", chat_id)), default=chat_id)
            linked_attachments = await render_attachment_lines(
                tg_user_id,
                client,
                linked_chat_id,
                msg.link.message,
            )
            if linked_attachments:
                block_lines.append("\n".join(f"↳ {line}" for line in linked_attachments))

        if not body and not attachment_lines and not (msg.link and msg.link.message):
            block_lines.append("<i>[Пустое сообщение]</i>")

        rendered_blocks.append("<blockquote>" + "\n".join(block_lines) + "</blockquote>")

    body_parts: list[str] = []
    body_length = 0
    separator = "\n\n"
    for block in rendered_blocks:
        extra = len(block) + (len(separator) if body_parts else 0)
        if body_length + extra > 3600:
            body_parts.append("<i>… часть истории скрыта из-за лимита Telegram</i>")
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
        start_no = start_index + 1
        end_no = end_index
        content = (
            f"💬 <b>{esc(chat_title)}</b>\n"
            f"<i>Сообщения {start_no}-{end_no} из {total_messages}. Новые внизу.</i>\n\n"
            f"{''.join(body_parts)}"
        )

    keyboard = build_history_keyboard(
        chat_id=chat_id,
        offset=offset,
        chat_page=chat_page,
        has_older=has_more,
        has_newer=has_newer,
        show_members=show_members,
        profile_user_id=profile_user_id,
    )
    return content, keyboard


async def send_media_by_token(message: types.Message, token: str) -> None:
    cleanup_media_cache()
    request = MEDIA_CACHE.get(token)
    if request is None:
        await message.answer("Ссылка для получения медиа устарела. Открой чат заново и нажми новую ссылку.")
        return

    if request.tg_user_id != message.from_user.id:
        await message.answer("Эта ссылка принадлежит другому пользователю.")
        return

    try:
        client = await session_manager.ensure_client(message.from_user.id)
    except Exception as exc:
        await message.answer(f"Не удалось подключиться к MAX: <code>{esc(exc)}</code>")
        return

    caption = "Медиа из MAX"
    kind = request.kind.upper()
    try:
        if kind == "PHOTO":
            if request.url:
                try:
                    await message.answer_photo(
                        photo=request.url,
                        caption=caption,
                        reply_markup=dismiss_message_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if not is_telegram_url_fetch_error(exc):
                        raise
                    file_name = _filename_from_url(request.url, "photo.jpg")
                    path = await download_media_to_temp(request.url, file_name)
                    try:
                        await message.answer_photo(
                            photo=FSInputFile(path),
                            caption=caption,
                            reply_markup=dismiss_message_keyboard(),
                        )
                    finally:
                        with contextlib.suppress(Exception):
                            os.remove(path)
            else:
                raise ValueError("URL фото не найден")
            return

        if kind == "FILE":
            if not request.file_id:
                raise ValueError("ID файла не найден")
            file_req = await client.get_file_by_id(
                chat_id=request.chat_id,
                message_id=request.message_id,
                file_id=request.file_id,
            )
            url = getattr(file_req, "url", None)
            if not isinstance(url, str) or not url:
                raise ValueError("MAX не вернул ссылку на файл")
            try:
                await message.answer_document(
                    document=url,
                    caption=request.name or caption,
                    reply_markup=dismiss_message_keyboard(),
                )
            except TelegramBadRequest as exc:
                if not is_telegram_url_fetch_error(exc):
                    raise
                file_name = request.name or _filename_from_url(url, "file.bin")
                path = await download_media_to_temp(url, file_name)
                try:
                    await message.answer_document(
                        document=FSInputFile(path, filename=file_name),
                        caption=request.name or caption,
                        reply_markup=dismiss_message_keyboard(),
                    )
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)
            return

        if kind == "VIDEO":
            if not request.video_id:
                raise ValueError("ID видео не найден")
            video_req = await client.get_video_by_id(
                chat_id=request.chat_id,
                message_id=request.message_id,
                video_id=request.video_id,
            )
            url = getattr(video_req, "url", None)
            if not isinstance(url, str) or not url:
                raise ValueError("MAX не вернул ссылку на видео")
            try:
                await message.answer_video(
                    video=url,
                    caption=caption,
                    reply_markup=dismiss_message_keyboard(),
                )
            except TelegramBadRequest as exc:
                if not is_telegram_url_fetch_error(exc):
                    raise
                file_name = _filename_from_url(url, "video.mp4")
                path = await download_media_to_temp(url, file_name)
                try:
                    await message.answer_video(
                        video=FSInputFile(path),
                        caption=caption,
                        reply_markup=dismiss_message_keyboard(),
                    )
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)
            return

        if kind == "AUDIO":
            if not request.url:
                raise ValueError("URL аудио не найден")
            try:
                await message.answer_audio(
                    audio=request.url,
                    caption=caption,
                    reply_markup=dismiss_message_keyboard(),
                )
            except TelegramBadRequest as exc:
                if not is_telegram_url_fetch_error(exc):
                    raise
                file_name = _filename_from_url(request.url, "audio.mp3")
                path = await download_media_to_temp(request.url, file_name)
                try:
                    await message.answer_audio(
                        audio=FSInputFile(path),
                        caption=caption,
                        reply_markup=dismiss_message_keyboard(),
                    )
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)
            return

        if kind == "STICKER":
            if not request.url:
                raise ValueError("URL стикера не найден")
            try:
                await message.answer_sticker(
                    sticker=request.url,
                    reply_markup=dismiss_message_keyboard(),
                )
            except TelegramBadRequest as exc:
                if not is_telegram_url_fetch_error(exc):
                    raise
                file_name = _filename_from_url(request.url, "sticker.webp")
                path = await download_media_to_temp(request.url, file_name)
                try:
                    await message.answer_document(
                        document=FSInputFile(path),
                        caption=caption,
                        reply_markup=dismiss_message_keyboard(),
                    )
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)
            return

        await message.answer("Этот тип вложения пока не поддержан для выдачи.")
    except Exception as exc:
        logger.warning("Failed to send media by token %s: %s", token, exc)
        fallback = request.url
        if fallback:
            await message.answer(
                "Не удалось отправить как файл. Открой прямую ссылку:\n"
                f"{esc(fallback)}",
                disable_web_page_preview=False,
                reply_markup=dismiss_message_keyboard(),
            )
            return
        await message.answer(
            "Не удалось отправить медиа как файл. Попробуй снова.",
            reply_markup=dismiss_message_keyboard(),
        )


def summarize_update_body(msg: Any) -> str:
    text = (getattr(msg, "text", "") or "").strip()
    attaches = getattr(msg, "attaches", None) or []
    if not text:
        if attaches:
            text = f"[Вложений: {len(attaches)}]"
        else:
            text = "[Пустое сообщение]"
    if len(text) > 1200:
        text = text[:1197] + "..."
    return text


async def poll_updates_for_user(tg_user_id: int) -> None:
    try:
        client = await session_manager.ensure_client(tg_user_id)
        entries = await get_chat_entries(tg_user_id, client, force_refresh=True)
    except Exception as exc:
        logger.debug("Skip updates for %s: %s", tg_user_id, exc)
        return

    me_id = parse_int(str(getattr(client.me, "id", 0)), default=0)

    for entry in entries[:UPDATE_POLL_CHAT_LIMIT]:
        last_event = int(entry.last_event_time or 0)
        if last_event <= 0:
            continue

        key = (tg_user_id, entry.chat_id)
        last_seen = UPDATE_LAST_SEEN.get(key)
        if last_seen is None:
            UPDATE_LAST_SEEN[key] = last_event
            continue
        if last_event <= last_seen:
            continue

        try:
            history = await client.fetch_history(
                chat_id=entry.chat_id,
                from_time=now_ms(),
                backward=UPDATE_HISTORY_BACKWARD,
            )
        except Exception as exc:
            logger.debug("History poll failed user=%s chat=%s: %s", tg_user_id, entry.chat_id, exc)
            UPDATE_LAST_SEEN[key] = max(last_seen, last_event)
            continue

        messages = history or []
        unique_messages = {msg.id: msg for msg in messages}
        ordered = sorted(unique_messages.values(), key=lambda msg: msg.time)
        new_messages = [msg for msg in ordered if int(getattr(msg, "time", 0) or 0) > last_seen]

        if not new_messages:
            UPDATE_LAST_SEEN[key] = max(last_seen, last_event)
            continue

        sender_ids: set[int] = set()
        for msg in new_messages:
            sender_id = parse_int(str(getattr(msg, "sender", 0)), default=0)
            if sender_id:
                sender_ids.add(sender_id)

        users_map: dict[int, Any] = {}
        if sender_ids:
            try:
                users = await client.get_users(sorted(sender_ids))
                users_map = {int(user.id): user for user in users}
            except Exception:
                users_map = {}

        max_sent_time = last_seen
        for msg in new_messages:
            msg_time = int(getattr(msg, "time", 0) or 0)
            if msg_time > max_sent_time:
                max_sent_time = msg_time

            sender_id = parse_int(str(getattr(msg, "sender", 0)), default=0)
            if sender_id and sender_id == me_id:
                continue

            sender_name = user_display_name(users_map.get(sender_id), f"Пользователь {sender_id}")
            body = summarize_update_body(msg)
            notify_text = (
                f"🔔 <b>Новое в {esc(entry.title)}</b>\n"
                f"<b>{esc(sender_name)}</b> <code>{time_label(msg_time)}</code>\n"
                f"{esc(body)}"
            )
            try:
                await bot.send_message(chat_id=tg_user_id, text=notify_text)
            except Exception as exc:
                logger.debug("Could not deliver update to tg=%s: %s", tg_user_id, exc)

        UPDATE_LAST_SEEN[key] = max(max_sent_time, last_event)


async def updates_loop() -> None:
    while True:
        try:
            user_ids = session_manager.get_authorized_user_ids()
            for user_id in user_ids:
                await poll_updates_for_user(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background update loop failed")
        await asyncio.sleep(UPDATE_POLL_SECONDS)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)

    text_raw = (message.text or "").strip()
    parts = text_raw.split(maxsplit=1)
    start_payload = parts[1].strip() if len(parts) > 1 else ""
    if start_payload.startswith("media_"):
        token = start_payload[6:]
        await session_manager.clear_auth_flow(message.from_user.id)
        await state.clear()
        with contextlib.suppress(Exception):
            await message.delete()
        await send_media_by_token(message, token)
        return

    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()

    has_token = session_manager.has_token(message.from_user.id)
    name = " ".join(
        part for part in [message.from_user.first_name, message.from_user.last_name] if part
    ).strip() or "друг"
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
    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    has_token = session_manager.has_token(message.from_user.id)
    await message.answer("<b>Главное меню</b>", reply_markup=main_menu_keyboard(has_token))


@dp.message(Command("login"))
async def cmd_login(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    has_token = session_manager.has_token(message.from_user.id)
    await message.answer(auth_menu_text(has_token), reply_markup=auth_methods_keyboard(has_token))


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    has_token = session_manager.has_token(message.from_user.id)
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard(has_token))


@dp.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
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


@dp.callback_query(F.data == "auth:menu")
async def cb_auth_menu(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.clear()
    has_token = session_manager.has_token(callback.from_user.id)
    await safe_edit_message(
        callback.message,
        auth_menu_text(has_token),
        auth_methods_keyboard(has_token),
    )
    await callback.answer()


@dp.callback_query(F.data == "auth:token")
async def cb_auth_token(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.set_state(UserFlow.waiting_for_token)
    await callback.message.answer(token_help_text(), reply_markup=cancel_flow_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "auth:phone")
async def cb_auth_phone(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.set_state(UserFlow.waiting_for_auth_phone)
    await callback.message.answer(phone_help_text(), reply_markup=cancel_flow_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "auth:qr")
@dp.callback_query(F.data == "auth:qr:refresh")
async def cb_auth_qr(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await state.clear()

    wait_message = await callback.message.answer("Генерирую новый QR-код…")
    try:
        data = await session_manager.begin_qr_login(callback.from_user.id)
        await wait_message.edit_text(
            qr_help_text(
                qr_link=str(data["qr_link"]),
                expires_at=int(data["expires_at"]),
            ),
            reply_markup=qr_auth_keyboard(),
        )
    except Exception as exc:
        await wait_message.edit_text(
            f"❌ {esc(exc)}\n\n"
            "Попробуй снова или выбери другой способ входа.",
            reply_markup=auth_methods_keyboard(session_manager.has_token(callback.from_user.id)),
        )
    await callback.answer()


@dp.callback_query(F.data == "auth:qr:check")
async def cb_auth_qr_check(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    try:
        status, token = await session_manager.check_qr_login(callback.from_user.id)
    except Exception as exc:
        await callback.message.answer(
            f"❌ {esc(exc)}\n\n"
            "Попробуй обновить QR или выбери другой способ входа.",
            reply_markup=qr_auth_keyboard(),
        )
        await callback.answer("Ошибка проверки QR", show_alert=True)
        return

    if status == "pending":
        await callback.answer("QR пока не подтвержден", show_alert=True)
        return

    if status == "expired":
        await callback.message.answer(
            "⌛️ Срок QR-кода истек. Нажми «Обновить QR».",
            reply_markup=qr_auth_keyboard(),
        )
        await callback.answer()
        return

    if status == "ready" and token:
        wait_message = await callback.message.answer("Подтверждаю вход и сохраняю токен…")
        try:
            await session_manager.validate_and_save_token(callback.from_user.id, token)
            CHAT_CACHE.pop(callback.from_user.id, None)
            await state.clear()
            await wait_message.edit_text(
                "✅ MAX успешно подключен через QR.",
                reply_markup=main_menu_keyboard(True),
            )
        except Exception as exc:
            await wait_message.edit_text(
                f"❌ {esc(exc)}\n\n"
                "Попробуй снова или выбери вход по токену.",
                reply_markup=auth_methods_keyboard(session_manager.has_token(callback.from_user.id)),
            )
    await callback.answer()


@dp.callback_query(F.data == "token:set")
async def cb_token_set(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.set_state(UserFlow.waiting_for_token)
    await callback.message.answer(token_help_text(), reply_markup=cancel_flow_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "flow:cancel")
async def cb_flow_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.clear()
    with contextlib.suppress(Exception):
        await callback.message.delete()
    await callback.answer("Отменено")


@dp.callback_query(F.data == "msg:close")
async def cb_msg_close(callback: types.CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.message(UserFlow.waiting_for_auth_phone, F.text)
async def input_auth_phone(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    phone = normalize_phone_input(message.text or "")
    if not phone:
        await message.answer(
            "Номер не распознан. Используй формат <code>+79991234567</code> и отправь снова."
        )
        return

    wait_message = await message.answer("Отправляю код подтверждения в MAX…")
    try:
        sent_phone = await session_manager.begin_phone_login(message.from_user.id, phone)
        await state.set_state(UserFlow.waiting_for_auth_code)
        await state.update_data(auth_phone=sent_phone)
        await wait_message.edit_text(
            f"✅ Код отправлен на <code>{esc(mask_phone(sent_phone))}</code>.\n"
            "Отправь код одним сообщением (6 цифр).",
            reply_markup=cancel_flow_keyboard(),
        )
    except Exception as exc:
        await wait_message.edit_text(
            f"❌ {esc(exc)}\n\n"
            "Проверь номер и попробуй снова.",
            reply_markup=cancel_flow_keyboard(),
        )


@dp.message(UserFlow.waiting_for_auth_code, F.text)
async def input_auth_code(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    code = re.sub(r"\D", "", message.text or "")
    if len(code) != 6:
        await message.answer("Нужен код из 6 цифр. Отправь его без лишнего текста.")
        return

    wait_message = await message.answer("Проверяю код…")
    try:
        token = await session_manager.complete_phone_login(message.from_user.id, code)
    except Exception as exc:
        await wait_message.edit_text(
            f"❌ {esc(exc)}\n\n"
            "Отправь код повторно.",
            reply_markup=cancel_flow_keyboard(),
        )
        return

    try:
        await session_manager.validate_and_save_token(message.from_user.id, token)
        CHAT_CACHE.pop(message.from_user.id, None)
        await state.clear()
        await wait_message.edit_text(
            "✅ MAX успешно подключен по телефону.",
            reply_markup=main_menu_keyboard(True),
        )
    except Exception as exc:
        await state.clear()
        await wait_message.edit_text(
            f"❌ {esc(exc)}\n\n"
            "Авторизацию по телефону нужно запустить заново.",
            reply_markup=auth_methods_keyboard(session_manager.has_token(message.from_user.id)),
        )


@dp.message(UserFlow.waiting_for_token, F.text)
async def input_token(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await session_manager.clear_auth_flow(message.from_user.id)
    token = normalize_token_input(message.text or "")

    if not token or len(token) < 20:
        await message.answer(
            "Не получилось распознать токен. Отправь только значение токена одной строкой.",
            reply_markup=cancel_flow_keyboard(),
        )
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
            "Проверь формат токена и попробуй снова.",
            reply_markup=cancel_flow_keyboard(),
        )


@dp.callback_query(F.data.startswith("chats:"))
async def cb_chats(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    page = parse_int((callback.data or "").split(":", maxsplit=1)[1], default=0)

    if not session_manager.has_token(callback.from_user.id):
        await callback.answer("Сначала авторизуйся в MAX", show_alert=True)
        await callback.message.answer(
            auth_menu_text(False),
            reply_markup=auth_methods_keyboard(False),
        )
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


@dp.callback_query(F.data.startswith("profile:"))
async def cb_profile_from_chat(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[1], default=0)
    offset = max(0, parse_int(parts[2], default=0))
    chat_page = max(0, parse_int(parts[3], default=0))
    user_id = parse_int(parts[4], default=0)
    if chat_id == 0 or user_id == 0:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        users = await client.get_users([user_id])
        user = users[0] if users else None
        text, avatar = render_user_profile_text(user, user_id)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Открыть чат",
                        callback_data=f"openpm:{user_id}:{chat_page}",
                    )
                ],
            ]
        )
        await show_profile_card(callback.message, text, keyboard, avatar)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open profile for user %s from chat", user_id)
        await callback.answer("Ошибка загрузки профиля", show_alert=True)
        await callback.message.answer(f"Не удалось открыть профиль: <code>{esc(exc)}</code>")


@dp.callback_query(F.data.startswith("members:"))
async def cb_members(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[1], default=0)
    offset = max(0, parse_int(parts[2], default=0))
    chat_page = max(0, parse_int(parts[3], default=0))
    page = max(0, parse_int(parts[4], default=0))
    if chat_id == 0:
        await callback.answer("Чат не найден", show_alert=True)
        return

    current_text = (callback.message.text or "").strip()
    is_members_view = "Участники:" in current_text

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        entries = await get_chat_entries(callback.from_user.id, client)
        entry = resolve_chat_entry(chat_id, entries)
        if entry is None or entry.chat_type.upper() != "CHAT":
            await callback.answer("Список участников доступен только для групп", show_alert=True)
            return

        participant_set: set[int] = set()
        me_id = parse_int(str(getattr(client.me, "id", 0)), default=0)
        for pid in (entry.participants or []):
            parsed = parse_int(str(pid), default=0)
            if parsed and parsed != me_id:
                participant_set.add(parsed)
        participant_ids = sorted(participant_set)
        if not participant_ids:
            empty_text = f"👥 <b>{esc(entry.title)}</b>\n<i>Нет участников для отображения.</i>"
            empty_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ К чату",
                            callback_data=f"chat:{chat_id}:{offset}:{chat_page}",
                        )
                    ]
                ]
            )
            if is_members_view:
                await safe_edit_message(
                    callback.message,
                    empty_text,
                    empty_keyboard,
                )
            else:
                with contextlib.suppress(Exception):
                    await callback.message.delete()
                await callback.message.answer(
                    empty_text,
                    reply_markup=empty_keyboard,
                )
            await callback.answer()
            return

        users = await client.get_users(participant_ids)
        users_map = {int(user.id): user for user in users}
        members = [
            (uid, user_display_name(users_map.get(uid), f"Пользователь {uid}"))
            for uid in participant_ids
        ]

        keyboard, current_page, total_pages = build_members_keyboard(
            members=members,
            chat_id=chat_id,
            offset=offset,
            chat_page=chat_page,
            page=page,
        )
        text = (
            f"👥 <b>Участники: {esc(entry.title)}</b>\n"
            f"Страница <b>{current_page + 1}/{total_pages}</b>\n"
            "Нажми на участника, чтобы открыть профиль."
        )
        if is_members_view:
            await safe_edit_message(callback.message, text, keyboard)
        else:
            with contextlib.suppress(Exception):
                await callback.message.delete()
            await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to load members for chat %s", chat_id)
        await callback.answer("Ошибка загрузки участников", show_alert=True)
        await callback.message.answer(f"Не удалось загрузить участников: <code>{esc(exc)}</code>")
@dp.callback_query(F.data.startswith("member:"))
async def cb_member_profile(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 6:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[1], default=0)
    offset = max(0, parse_int(parts[2], default=0))
    chat_page = max(0, parse_int(parts[3], default=0))
    user_id = parse_int(parts[4], default=0)
    members_page = max(0, parse_int(parts[5], default=0))
    if chat_id == 0 or user_id == 0:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        users = await client.get_users([user_id])
        user = users[0] if users else None
        text, avatar = render_user_profile_text(user, user_id)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Открыть чат",
                        callback_data=f"openpm:{user_id}:{chat_page}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К участникам",
                        callback_data=f"members:{chat_id}:{offset}:{chat_page}:{members_page}",
                    )
                ],
            ]
        )
        await show_profile_card(callback.message, text, keyboard, avatar)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open member profile %s", user_id)
        await callback.answer("Ошибка загрузки профиля", show_alert=True)
        await callback.message.answer(f"Не удалось открыть профиль: <code>{esc(exc)}</code>")


@dp.callback_query(F.data.startswith("openpm:"))
async def cb_open_private_chat(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    user_id = parse_int(parts[1], default=0)
    chat_page = max(0, parse_int(parts[2], default=0))
    if user_id == 0:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        me_id = parse_int(str(getattr(client.me, "id", 0)), default=0)
        if me_id == 0:
            raise ValueError("Не удалось определить твой MAX ID")

        dm_chat_id = client.get_chat_id(me_id, user_id)
        text, keyboard = await build_history_text(
            tg_user_id=callback.from_user.id,
            client=client,
            chat_id=dm_chat_id,
            offset=0,
            chat_page=chat_page,
        )
        with contextlib.suppress(Exception):
            await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open private chat with user %s", user_id)
        await callback.answer("Не удалось открыть чат", show_alert=True)
        await callback.message.answer(f"Ошибка открытия чата: <code>{esc(exc)}</code>")


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
        "✍️ Отправь текст сообщения одним сообщением.",
        reply_markup=cancel_flow_keyboard(),
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
            "Попробуй снова.",
            reply_markup=cancel_flow_keyboard(),
        )


@dp.message(F.text.startswith("/media_"))
async def cmd_media_link(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    match = MEDIA_CMD_REGEX.match((message.text or "").strip())
    if not match:
        return
    token = match.group(1)
    if await state.get_state() is not None:
        await state.clear()
    await send_media_by_token(message, token)


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
    global UPDATE_TASK
    logger.info("Bot is starting")
    os.makedirs("sessions", exist_ok=True)
    try:
        await ensure_bot_username()
    except Exception as exc:
        logger.warning("Could not resolve bot username for media links: %s", exc)

    UPDATE_TASK = asyncio.create_task(updates_loop(), name="max-updates-loop")
    try:
        await dp.start_polling(bot)
    finally:
        if UPDATE_TASK:
            UPDATE_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await UPDATE_TASK
            UPDATE_TASK = None
        await session_manager.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
