import asyncio
import contextlib
import ast
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
from collections import deque
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
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from pymax.files import File as MaxFile
from pymax.files import Photo as MaxPhoto
from pymax.files import Video as MaxVideo

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
MEDIA_CMD_REGEX = re.compile(r"^/media_([A-Za-z0-9]+)$")
DELETE_START_PAYLOAD_REGEX = re.compile(r"^del_(-?\d+)_(-?\d+)$")
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_INSTRUCTION_IMAGE_PATH = os.path.join(BASE_DIR, "instruction.png")
MAIN_MENU_IMAGE_PATH = os.path.join(BASE_DIR, "main.png")
CHATS_MENU_IMAGE_PATH = os.path.join(BASE_DIR, "chats.png")
TOKEN_INSTRUCTION_PHOTO_FILE_ID: str | None = None
MAIN_MENU_PHOTO_FILE_ID: str | None = None
CHATS_MENU_PHOTO_FILE_ID: str | None = None
MAIN_MENU_PHOTO_SHA1: str | None = None
CHATS_MENU_PHOTO_SHA1: str | None = None

try:
    UPDATE_POLL_SECONDS = max(3, int(os.getenv("UPDATE_POLL_SECONDS", "10").strip()))
except Exception:
    UPDATE_POLL_SECONDS = 10

try:
    CHAT_AUTORELOAD_SECONDS = max(
        3,
        min(5, int(os.getenv("CHAT_AUTORELOAD_SECONDS", "4").strip())),
    )
except Exception:
    CHAT_AUTORELOAD_SECONDS = 4

try:
    QUEUE_RETRY_SECONDS = max(3, int(os.getenv("QUEUE_RETRY_SECONDS", "5").strip()))
except Exception:
    QUEUE_RETRY_SECONDS = 5

try:
    QUEUE_BATCH_SIZE = max(1, int(os.getenv("QUEUE_BATCH_SIZE", "20").strip()))
except Exception:
    QUEUE_BATCH_SIZE = 20

BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
OUTBOX_DIR = os.path.join("sessions", "outbox")
UI_PHOTO_CACHE_PATH = os.path.join("sessions", "ui_photo_file_ids.json")
TEMPORARY_SEND_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "temporarily",
    "temporary",
    "connect",
    "connection",
    "network",
    "unavailable",
    "service unavailable",
    "502",
    "503",
    "504",
    "send and wait failed",
    "broken pipe",
    "connection reset",
    "socket",
    "websocket",
    "transport",
)
PERMANENT_SEND_ERROR_MARKERS = (
    "invalid token",
    "login.token",
    "not found",
    "forbidden",
    "permission denied",
    "auth",
    "unauthorized",
)

HISTORY_ANCHORS: dict[tuple[int, int], int] = {}
CHAT_CACHE: dict[int, tuple[float, list["ChatEntry"]]] = {}
MEDIA_CACHE: dict[str, "MediaRequest"] = {}
MEDIA_REQUEST_INDEX: dict[str, str] = {}
UPDATE_LAST_SEEN: dict[tuple[int, int], int] = {}
UNREAD_COUNTS: dict[tuple[int, int], int] = {}
ACTIVE_CHAT_VIEWS: dict[int, "ActiveChatView"] = {}
UPDATE_TASK: asyncio.Task | None = None
CHAT_REFRESH_TASK: asyncio.Task | None = None
QUEUE_TASK: asyncio.Task | None = None

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True),
)
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


@dataclass
class ActiveChatView:
    tg_user_id: int
    tg_chat_id: int
    tg_message_id: int
    chat_id: int
    chat_page: int
    offset: int = 0
    paused: bool = False
    signature: str = ""
    last_refresh_at: float = 0.0
    in_progress: bool = False


@dataclass
class SendErrorEvent:
    at: int
    source: str
    tg_user_id: int
    chat_id: int
    error: str


@dataclass
class RuntimeMetrics:
    started_at: float = field(default_factory=time.time)
    direct_sent: int = 0
    queued_messages: int = 0
    queue_sent: int = 0
    send_failures: int = 0
    update_notifications: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=400))
    send_errors: deque[SendErrorEvent] = field(default_factory=lambda: deque(maxlen=40))


METRICS = RuntimeMetrics()
ADMIN_IDS: set[int] = set()


def esc(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_int(raw: str, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for chunk in re.split(r"[\s,;]+", (raw or "").strip()):
        if not chunk:
            continue
        user_id = parse_int(chunk, default=0)
        if user_id > 0:
            result.add(user_id)
    return result


ADMIN_IDS = _parse_admin_ids(ADMIN_IDS_RAW)
if ADMIN_IDS:
    logger.info("Admin commands enabled for %s user(s)", len(ADMIN_IDS))
else:
    logger.warning("ADMIN_IDS is empty: /health and /stats will be available for all users")


def is_admin_user(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def format_duration(seconds: int) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * ratio))
    idx = max(0, min(len(ordered) - 1, idx))
    return float(ordered[idx])


def record_send_latency(latency_ms: float) -> None:
    METRICS.latencies_ms.append(max(0.0, float(latency_ms)))


def record_send_error(source: str, tg_user_id: int, chat_id: int, exc: Exception) -> None:
    METRICS.send_failures += 1
    message = str(exc).strip().replace("\n", " ")
    if len(message) > 300:
        message = message[:297] + "..."
    METRICS.send_errors.append(
        SendErrorEvent(
            at=int(time.time()),
            source=source,
            tg_user_id=tg_user_id,
            chat_id=chat_id,
            error=message,
        )
    )


def unread_count_for_chat(tg_user_id: int, chat_id: int) -> int:
    return max(0, int(UNREAD_COUNTS.get((tg_user_id, chat_id), 0)))


def total_unread_for_user(tg_user_id: int) -> int:
    return sum(count for (uid, _), count in UNREAD_COUNTS.items() if uid == tg_user_id)


def mark_chat_read(tg_user_id: int, chat_id: int, seen_time: int | None = None) -> None:
    key = (tg_user_id, chat_id)
    mark = int(seen_time or now_ms())
    UPDATE_LAST_SEEN[key] = max(UPDATE_LAST_SEEN.get(key, 0), mark)
    UNREAD_COUNTS.pop(key, None)


def increment_unread_count(tg_user_id: int, chat_id: int, delta: int) -> None:
    if delta <= 0:
        return
    key = (tg_user_id, chat_id)
    UNREAD_COUNTS[key] = max(0, int(UNREAD_COUNTS.get(key, 0)) + int(delta))


def clear_all_unread_for_user(tg_user_id: int) -> int:
    keys = [key for key in UNREAD_COUNTS if key[0] == tg_user_id]
    cleared = sum(int(UNREAD_COUNTS.get(key, 0)) for key in keys)
    for key in keys:
        UNREAD_COUNTS.pop(key, None)
    return cleared


def set_active_chat_view(
    tg_user_id: int,
    tg_chat_id: int,
    tg_message_id: int,
    chat_id: int,
    chat_page: int,
    offset: int,
    signature: str,
    paused: bool = False,
) -> None:
    ACTIVE_CHAT_VIEWS[tg_user_id] = ActiveChatView(
        tg_user_id=tg_user_id,
        tg_chat_id=tg_chat_id,
        tg_message_id=tg_message_id,
        chat_id=chat_id,
        chat_page=chat_page,
        offset=offset,
        paused=paused,
        signature=signature,
        last_refresh_at=time.time(),
    )


def clear_active_chat_view(tg_user_id: int) -> None:
    ACTIVE_CHAT_VIEWS.pop(tg_user_id, None)


def chat_view_signature(text: str, reply_markup: InlineKeyboardMarkup | None) -> str:
    payload = text
    if reply_markup is not None:
        try:
            payload += "\n" + json.dumps(reply_markup.model_dump(), ensure_ascii=True, sort_keys=True)
        except Exception:
            payload += "\n" + str(reply_markup)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def is_temporary_send_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    if any(marker in lowered for marker in PERMANENT_SEND_ERROR_MARKERS):
        return False
    return any(marker in lowered for marker in TEMPORARY_SEND_ERROR_MARKERS)


def normalize_token_input(raw: str) -> str | None:
    source = (raw or "").strip()
    if not source:
        return None

    if source.startswith("```") and source.endswith("```"):
        code_block = source[3:-3].strip()
        code_lines = code_block.splitlines()
        if len(code_lines) > 1 and code_lines[0].strip().lower() in {"json", "js", "javascript"}:
            source = "\n".join(code_lines[1:]).strip()
        else:
            source = code_block

    if source.lower().startswith("bearer "):
        source = source[7:].strip()

    if len(source) >= 2 and source[0] == source[-1] and source[0] in {"'", '"', "`"}:
        source = source[1:-1].strip()

    def _extract_mapping_token(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("token", "user_token", "auth_token", "access_token"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    try:
        extracted = _extract_mapping_token(json.loads(source))
        if extracted:
            source = extracted
    except json.JSONDecodeError:
        pass

    if source.startswith("{") and source.endswith("}"):
        with contextlib.suppress(Exception):
            extracted = _extract_mapping_token(ast.literal_eval(source))
            if extracted:
                source = extracted

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


def make_link(url: str, label: str) -> str:
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def _media_request_key(
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
    payload = {
        "tg_user_id": int(tg_user_id),
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "kind": str(kind or "").upper(),
        "file_id": int(file_id or 0),
        "video_id": int(video_id or 0),
        "url": str(url or ""),
        "name": str(name or ""),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def cleanup_media_cache() -> None:
    now = time.time()
    stale_keys = [
        token
        for token, item in MEDIA_CACHE.items()
        if now - item.created_at > MEDIA_LINK_TTL_SECONDS
    ]
    for token in stale_keys:
        MEDIA_CACHE.pop(token, None)

    stale_index = [
        request_key
        for request_key, token in MEDIA_REQUEST_INDEX.items()
        if token in stale_keys or token not in MEDIA_CACHE
    ]
    for request_key in stale_index:
        MEDIA_REQUEST_INDEX.pop(request_key, None)


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
    request_key = _media_request_key(
        tg_user_id=tg_user_id,
        chat_id=chat_id,
        message_id=message_id,
        kind=kind,
        file_id=file_id,
        video_id=video_id,
        url=url,
        name=name,
    )

    cached_token = MEDIA_REQUEST_INDEX.get(request_key)
    if cached_token:
        cached_item = MEDIA_CACHE.get(cached_token)
        if cached_item is not None:
            cached_item.created_at = time.time()
            return cached_token
        MEDIA_REQUEST_INDEX.pop(request_key, None)

    token = secrets.token_urlsafe(8).replace("_", "").replace("-", "")[:12]
    while token in MEDIA_CACHE:
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
    MEDIA_REQUEST_INDEX[request_key] = token
    return token


def media_command_markup(token: str) -> str:
    command = f"/media_{token}"
    if BOT_USERNAME:
        return make_link(f"https://t.me/{BOT_USERNAME}?start=media_{token}", "получить в боте")
    return f"<code>{esc(command)}</code>"


def delete_message_start_payload(chat_id: int, message_id: int) -> str:
    return f"del_{int(chat_id)}_{int(message_id)}"


def delete_message_command_markup(chat_id: int, message_id: int) -> str:
    payload = delete_message_start_payload(chat_id, message_id)
    if BOT_USERNAME:
        return make_link(f"https://t.me/{BOT_USERNAME}?start={payload}", "🗑️")
    return f"<code>/start {esc(payload)}</code>"


def parse_delete_start_payload(raw_payload: str) -> tuple[int, int] | None:
    payload = (raw_payload or "").strip()
    match = DELETE_START_PAYLOAD_REGEX.fullmatch(payload)
    if not match:
        return None

    chat_id = parse_int(match.group(1), default=0)
    message_id = parse_int(match.group(2), default=0)
    if chat_id == 0 or message_id == 0:
        return None
    return chat_id, message_id


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


async def download_telegram_file_to_temp(
    file_id: str,
    filename_hint: str,
) -> str:
    file_data = await bot.get_file(file_id)
    file_path = str(getattr(file_data, "file_path", "") or "").strip()
    if not file_path:
        raise ValueError("Telegram did not return file path")

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    file_name = os.path.basename(file_path) or filename_hint
    return await download_media_to_temp(file_url, file_name)


async def build_max_attachment_from_message(
    message: types.Message,
) -> tuple[MaxPhoto | MaxVideo | MaxFile | None, str | None]:
    if message.photo:
        photo = message.photo[-1]
        path = await download_telegram_file_to_temp(photo.file_id, "photo.jpg")
        return MaxPhoto(path=path), path

    if message.video:
        file_name = str(getattr(message.video, "file_name", "") or "video.mp4")
        path = await download_telegram_file_to_temp(message.video.file_id, file_name)
        return MaxVideo(path=path), path

    if message.animation:
        file_name = str(getattr(message.animation, "file_name", "") or "animation.mp4")
        path = await download_telegram_file_to_temp(message.animation.file_id, file_name)
        return MaxVideo(path=path), path

    if message.document:
        doc = message.document
        file_name = str(getattr(doc, "file_name", "") or "document.bin")
        mime_type = str(getattr(doc, "mime_type", "") or "").lower()
        ext = os.path.splitext(file_name)[1].lower()
        path = await download_telegram_file_to_temp(doc.file_id, file_name)

        if mime_type.startswith("image/") and ext in PHOTO_EXTENSIONS:
            return MaxPhoto(path=path), path
        if mime_type.startswith("video/"):
            return MaxVideo(path=path), path
        return MaxFile(path=path), path

    if message.audio:
        file_name = str(getattr(message.audio, "file_name", "") or "audio.mp3")
        path = await download_telegram_file_to_temp(message.audio.file_id, file_name)
        return MaxFile(path=path), path

    if message.voice:
        path = await download_telegram_file_to_temp(message.voice.file_id, "voice.ogg")
        return MaxFile(path=path), path

    return None, None


def outgoing_attachment_kind(attachment: MaxPhoto | MaxVideo | MaxFile | None) -> str | None:
    if attachment is None:
        return None
    if isinstance(attachment, MaxPhoto):
        return "PHOTO"
    if isinstance(attachment, MaxVideo):
        return "VIDEO"
    return "FILE"


def persist_outgoing_attachment(
    tg_user_id: int,
    temp_path: str,
    source_name: str | None = None,
) -> str:
    if not temp_path or not os.path.isfile(temp_path):
        raise ValueError("Не найден временный файл вложения для очереди отправки")

    user_dir = os.path.join(OUTBOX_DIR, f"user_{tg_user_id}")
    os.makedirs(user_dir, exist_ok=True)
    file_name = os.path.basename(source_name or temp_path or "attachment.bin")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_name).strip("._") or "attachment.bin"
    destination = os.path.join(
        user_dir,
        f"{int(time.time())}_{secrets.token_hex(4)}_{safe_name}",
    )
    os.replace(temp_path, destination)
    return destination


def build_outgoing_attachment_from_queue(
    item: dict[str, Any],
) -> MaxPhoto | MaxVideo | MaxFile | None:
    attachment_path = str(item.get("attachment_path") or "").strip()
    if not attachment_path:
        return None
    if not os.path.isfile(attachment_path):
        raise FileNotFoundError(f"Файл вложения не найден: {attachment_path}")

    kind = str(item.get("attachment_type") or "").upper()
    if kind == "PHOTO":
        return MaxPhoto(path=attachment_path)
    if kind == "VIDEO":
        return MaxVideo(path=attachment_path)
    return MaxFile(path=attachment_path)


def queued_message_keyboard(chat_id: int, chat_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Открыть чат", callback_data=f"chat:{chat_id}:0:{chat_page}")],
            [InlineKeyboardButton(text="⬅️ Назад к чатам", callback_data=f"chats:{chat_page}")],
        ]
    )


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
    if not has_token:
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🔐 Войти в MAX", callback_data="auth:menu")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile:me")],
        [InlineKeyboardButton(text="💬 Мои чаты", callback_data="chats:0")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def auth_methods_keyboard(has_token: bool) -> InlineKeyboardMarkup:
    token_text = (
        "🔑 Обновить MAX токен (Самый рабочий)"
        if has_token
        else "🔑 Войти по токену (Самый рабочий)"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=token_text, callback_data="auth:token")],
        [InlineKeyboardButton(text="🧩 Войти через QR", callback_data="auth:qr")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def self_profile_keyboard(has_token: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_token:
        rows.append([InlineKeyboardButton(text="🚪 Выйти", callback_data="logout:confirm")])
    else:
        rows.append([InlineKeyboardButton(text="🔐 Войти в MAX", callback_data="auth:menu")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def logout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, выйти", callback_data="logout:yes")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="logout:cancel")],
        ]
    )


def cancel_flow_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отменить", callback_data="flow:cancel")]]
    )


def dismiss_message_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👌", callback_data="msg:close")]]
    )


def update_notification_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Прочитать", callback_data=f"notify:read:{int(chat_id)}")],
            [InlineKeyboardButton(text="💬 Перейти в чат", callback_data=f"notify:open:{int(chat_id)}")],
        ]
    )


def _with_dismiss_row(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    dismiss_rows = dismiss_message_keyboard().inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=[*rows, *dismiss_rows])


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🩺 Health", callback_data="admin:health"),
            InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats"),
        ],
        [
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
            InlineKeyboardButton(text="📥 Очередь", callback_data="admin:queue"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:panel")],
    ]
    return _with_dismiss_row(rows)


def admin_back_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:panel")]]
    return _with_dismiss_row(rows)


def render_health_text() -> str:
    uptime = format_duration(int(time.time() - METRICS.started_at))
    authorized_users = session_manager.get_authorized_user_ids()
    pending_queue = session_manager.count_pending_outgoing()
    queue_users = session_manager.get_outgoing_user_ids()
    latencies = list(METRICS.latencies_ms)

    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        p95_latency = percentile(latencies, 0.95)
        latency_line = f"{avg_latency:.0f}ms avg / {p95_latency:.0f}ms p95"
    else:
        latency_line = "нет данных"

    lines = [
        "<b>Health</b>",
        f"Uptime: <code>{esc(uptime)}</code>",
        f"Авторизовано пользователей: <b>{len(authorized_users)}</b>",
        f"Активных MAX клиентов: <b>{session_manager.active_client_count()}</b>",
        f"Активных auth-flow: <b>{session_manager.active_auth_flow_count()}</b>",
        f"Открытых чатов с автообновлением: <b>{len(ACTIVE_CHAT_VIEWS)}</b>",
        f"Очередь отправки: <b>{pending_queue}</b> (пользователей: {len(queue_users)})",
        f"Latency отправки: <b>{esc(latency_line)}</b>",
        f"Updates loop: <b>{'OK' if UPDATE_TASK and not UPDATE_TASK.done() else 'DOWN'}</b>",
        f"Chat refresh loop: <b>{'OK' if CHAT_REFRESH_TASK and not CHAT_REFRESH_TASK.done() else 'DOWN'}</b>",
        f"Queue loop: <b>{'OK' if QUEUE_TASK and not QUEUE_TASK.done() else 'DOWN'}</b>",
    ]
    return "\n".join(lines)


def render_stats_text() -> str:
    latencies = list(METRICS.latencies_ms)
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        p95_latency = percentile(latencies, 0.95)
    else:
        avg_latency = 0.0
        p95_latency = 0.0

    lines = [
        "<b>Stats</b>",
        f"Direct sent: <b>{METRICS.direct_sent}</b>",
        f"Queued (enqueued): <b>{METRICS.queued_messages}</b>",
        f"Queued (delivered): <b>{METRICS.queue_sent}</b>",
        f"Send failures: <b>{METRICS.send_failures}</b>",
        f"Update notifications: <b>{METRICS.update_notifications}</b>",
        f"Pending queue now: <b>{session_manager.count_pending_outgoing()}</b>",
        f"Unread now: <b>{sum(UNREAD_COUNTS.values())}</b>",
        f"Latency avg/p95: <b>{avg_latency:.0f}ms / {p95_latency:.0f}ms</b>",
    ]

    recent = list(METRICS.send_errors)[-8:]
    if recent:
        lines.append("")
        lines.append("<b>Последние ошибки отправки:</b>")
        for event in recent:
            stamp = datetime.fromtimestamp(event.at).strftime("%H:%M:%S")
            lines.append(
                f"• <code>{esc(stamp)}</code> [{esc(event.source)}] "
                f"u{event.tg_user_id} c{event.chat_id}: {esc(event.error)}"
            )
    return "\n".join(lines)


def render_admin_users_text() -> str:
    authorized = sorted(session_manager.get_authorized_user_ids())
    queued = sorted(session_manager.get_outgoing_user_ids())
    authorized_set = set(authorized)
    queued_set = set(queued)
    all_ids = sorted(authorized_set | queued_set)

    lines = [
        "<b>👥 Пользователи</b>",
        f"С токеном MAX: <b>{len(authorized)}</b>",
        f"С очередью отправки: <b>{len(queued)}</b>",
    ]

    if not all_ids:
        lines.append("")
        lines.append("<i>Пока нет пользователей для отображения.</i>")
        return "\n".join(lines)

    lines.append("")
    lines.append("<b>Список (до 25):</b>")
    for user_id in all_ids[:25]:
        badges: list[str] = []
        if user_id in authorized_set:
            badges.append("MAX")
        if user_id in queued_set:
            badges.append(f"Q:{session_manager.count_pending_outgoing(user_id)}")
        badge_text = ", ".join(badges) if badges else "—"
        lines.append(f"• <code>{user_id}</code> [{esc(badge_text)}]")

    extra = len(all_ids) - 25
    if extra > 0:
        lines.append(f"… и еще <b>{extra}</b> пользователей")
    return "\n".join(lines)


def render_admin_queue_text() -> str:
    pending_total = session_manager.count_pending_outgoing()
    queue_users = sorted(session_manager.get_outgoing_user_ids())

    lines = [
        "<b>📥 Очередь отправки</b>",
        f"Всего сообщений в очереди: <b>{pending_total}</b>",
        f"Пользователей с очередью: <b>{len(queue_users)}</b>",
    ]

    if not queue_users:
        lines.append("")
        lines.append("<i>Очередь пуста.</i>")
        return "\n".join(lines)

    lines.append("")
    lines.append("<b>Нагрузка по пользователям:</b>")
    for user_id in queue_users[:30]:
        count = session_manager.count_pending_outgoing(user_id)
        lines.append(f"• <code>{user_id}</code>: <b>{count}</b>")

    extra = len(queue_users) - 30
    if extra > 0:
        lines.append(f"… и еще <b>{extra}</b> пользователей")
    return "\n".join(lines)


def render_admin_panel_text() -> str:
    uptime = format_duration(int(time.time() - METRICS.started_at))
    lines = [
        "<b>🛠 Админ-панель Tg2max</b>",
        f"Uptime: <code>{esc(uptime)}</code>",
        f"Авторизовано пользователей: <b>{len(session_manager.get_authorized_user_ids())}</b>",
        f"Активных MAX клиентов: <b>{session_manager.active_client_count()}</b>",
        f"Очередь отправки: <b>{session_manager.count_pending_outgoing()}</b>",
        f"Непрочитанные (в RAM): <b>{sum(UNREAD_COUNTS.values())}</b>",
        "",
        f"Updates loop: <b>{'OK' if UPDATE_TASK and not UPDATE_TASK.done() else 'DOWN'}</b>",
        f"Chat refresh loop: <b>{'OK' if CHAT_REFRESH_TASK and not CHAT_REFRESH_TASK.done() else 'DOWN'}</b>",
        f"Queue loop: <b>{'OK' if QUEUE_TASK and not QUEUE_TASK.done() else 'DOWN'}</b>",
        "",
        "Выбери раздел ниже.",
    ]
    return "\n".join(lines)


def auth_menu_text(has_token: bool) -> str:
    status = "подключен ✅" if has_token else "не подключен ❌"
    return (
        "<b>Авторизация MAX</b>\n"
        f"Текущий статус: <b>{status}</b>\n\n"
        "Выбери способ входа:\n"
        "• токен <u>(Самый рабочий)</u>\n"
        "• QR-код"
    )


def main_menu_text(has_token: bool, name: str) -> str:
    status = "подключен ✅" if has_token else "не подключен ❌"
    return (
        "<b>Tg2max</b>\n"
        f"Привет, <b>{esc(name)}</b>.\n\n"
        f"Статус MAX: <b>{status}</b>\n"
        "Выбери действие:"
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
        "Можно отправить:\n"
        "• токен в чистом виде\n"
        "• JSON, например: <code>{\"token\":\"...\",\"viewerId\":94350134}</code>\n"
        "<i>Я сам извлеку поле token.</i>\n"
        "\n"
        "<i>Токен сохранится только для твоего Telegram ID.</i>"
    )


async def send_token_instructions(message: types.Message) -> list[int]:
    global TOKEN_INSTRUCTION_PHOTO_FILE_ID
    sent_ids: list[int] = []

    if TOKEN_INSTRUCTION_PHOTO_FILE_ID:
        try:
            sent_photo = await message.answer_photo(
                photo=TOKEN_INSTRUCTION_PHOTO_FILE_ID,
                caption="Инструкция на скриншоте.",
            )
            sent_ids.append(sent_photo.message_id)
        except Exception as exc:
            logger.warning(
                "Failed to send cached token instruction image %s: %s",
                TOKEN_INSTRUCTION_PHOTO_FILE_ID,
                exc,
            )
            TOKEN_INSTRUCTION_PHOTO_FILE_ID = None

    photo_candidates = [TOKEN_INSTRUCTION_IMAGE_PATH, "instruction.png"]
    seen_paths: set[str] = set()
    if not sent_ids:
        for candidate in photo_candidates:
            full_path = os.path.abspath(candidate)
            if full_path in seen_paths:
                continue
            seen_paths.add(full_path)
            if not os.path.isfile(full_path):
                continue

            try:
                sent_photo = await message.answer_photo(
                    photo=FSInputFile(full_path),
                    caption="Инструкция на скриншоте.",
                )
                sent_ids.append(sent_photo.message_id)
                photo_sizes = getattr(sent_photo, "photo", None) or []
                if photo_sizes:
                    file_id = getattr(photo_sizes[-1], "file_id", None)
                    if isinstance(file_id, str) and file_id:
                        TOKEN_INSTRUCTION_PHOTO_FILE_ID = file_id
                break
            except Exception as exc:
                logger.warning("Failed to send token instruction image %s: %s", full_path, exc)

    sent_text = await message.answer(token_help_text(), reply_markup=cancel_flow_keyboard())
    sent_ids.append(sent_text.message_id)
    return sent_ids


async def cleanup_auth_instruction_messages(
    state: FSMContext,
    chat_id: int,
) -> None:
    data = await state.get_data()
    raw_ids = data.get("auth_instruction_message_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return

    unique_ids: list[int] = []
    seen: set[int] = set()
    for item in raw_ids:
        try:
            mid = int(item)
        except (TypeError, ValueError):
            continue
        if mid <= 0 or mid in seen:
            continue
        seen.add(mid)
        unique_ids.append(mid)

    for message_id in unique_ids:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)

    await state.update_data(auth_instruction_message_ids=[])


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
            [InlineKeyboardButton(text="✖️ Отменить", callback_data="auth:menu")],
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


def build_chats_keyboard(
    entries: list[ChatEntry],
    page: int,
    tg_user_id: int,
) -> tuple[InlineKeyboardMarkup, int, int]:
    total_pages = max(1, (len(entries) + CHAT_PAGE_SIZE - 1) // CHAT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CHAT_PAGE_SIZE
    page_items = entries[start : start + CHAT_PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    unread_total = total_unread_for_user(tg_user_id)
    for item in page_items:
        label = f"{chat_type_icon(item.chat_type)} {short_title(item.title)}"
        unread_count = unread_count_for_chat(tg_user_id, item.chat_id)
        if unread_count > 0:
            label = f"{label} • {unread_count}"
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

    if unread_total > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Прочитать все ({unread_total})",
                    callback_data=f"readall:{page}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), page, total_pages


def resolve_chat_page(entries: list[ChatEntry], chat_id: int) -> int:
    for index, entry in enumerate(entries):
        if int(entry.chat_id) == int(chat_id):
            return index // CHAT_PAGE_SIZE
    return 0


def build_history_keyboard(
    chat_id: int,
    offset: int,
    chat_page: int,
    has_older: bool,
    has_newer: bool,
    page_step: int,
    show_members: bool,
    profile_user_id: int | None,
    auto_refresh_paused: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    step = max(1, page_step)

    nav: list[InlineKeyboardButton] = []
    if has_older:
        nav.append(
            InlineKeyboardButton(
                text=f"⬆️ Старее {step}",
                callback_data=f"chat:{chat_id}:{offset + step}:{chat_page}",
            )
        )
    if has_newer:
        nav.append(
            InlineKeyboardButton(
                text=f"⬇️ Новее {step}",
                callback_data=f"chat:{chat_id}:{max(0, offset - step)}:{chat_page}",
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

    if offset == 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Продолжить" if auto_refresh_paused else "⏸ Пауза",
                    callback_data=(
                        f"chatauto:resume:{chat_id}:{chat_page}"
                        if auto_refresh_paused
                        else f"chatauto:pause:{chat_id}:{chat_page}"
                    ),
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
        if getattr(message, "photo", None) or getattr(message, "video", None):
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.answer(text=text, reply_markup=reply_markup)


def _message_has_photo_or_video(message: types.Message) -> bool:
    return bool(getattr(message, "photo", None) or getattr(message, "video", None))


def _photo_cache_key(photo_ref: str) -> str | None:
    full_path = os.path.abspath((photo_ref or "").strip())
    if full_path == os.path.abspath(MAIN_MENU_IMAGE_PATH):
        return "main"
    if full_path == os.path.abspath(CHATS_MENU_IMAGE_PATH):
        return "chats"
    return None


def _file_sha1(path: str) -> str | None:
    full_path = os.path.abspath((path or "").strip())
    if not full_path or not os.path.isfile(full_path):
        return None

    h = hashlib.sha1()
    with open(full_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _save_ui_photo_cache() -> None:
    payload = {
        "main": {
            "file_id": MAIN_MENU_PHOTO_FILE_ID or "",
            "sha1": MAIN_MENU_PHOTO_SHA1 or "",
        },
        "chats": {
            "file_id": CHATS_MENU_PHOTO_FILE_ID or "",
            "sha1": CHATS_MENU_PHOTO_SHA1 or "",
        },
    }
    os.makedirs(os.path.dirname(UI_PHOTO_CACHE_PATH), exist_ok=True)
    with open(UI_PHOTO_CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=True)


def _load_ui_photo_cache() -> None:
    global MAIN_MENU_PHOTO_FILE_ID, CHATS_MENU_PHOTO_FILE_ID
    global MAIN_MENU_PHOTO_SHA1, CHATS_MENU_PHOTO_SHA1

    current_main_sha1 = _file_sha1(MAIN_MENU_IMAGE_PATH)
    current_chats_sha1 = _file_sha1(CHATS_MENU_IMAGE_PATH)

    if not os.path.isfile(UI_PHOTO_CACHE_PATH):
        MAIN_MENU_PHOTO_SHA1 = current_main_sha1
        CHATS_MENU_PHOTO_SHA1 = current_chats_sha1
        return
    try:
        with open(UI_PHOTO_CACHE_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        logger.debug("Could not read UI photo cache: %s", exc)
        MAIN_MENU_PHOTO_SHA1 = current_main_sha1
        CHATS_MENU_PHOTO_SHA1 = current_chats_sha1
        return

    main_file_id = ""
    main_sha1 = ""
    chats_file_id = ""
    chats_sha1 = ""

    if isinstance(payload, dict):
        raw_main = payload.get("main")
        raw_chats = payload.get("chats")

        # Backward compatible: old format stored plain file_id strings.
        if isinstance(raw_main, str):
            main_file_id = raw_main.strip()
        elif isinstance(raw_main, dict):
            main_file_id = str(raw_main.get("file_id", "") or "").strip()
            main_sha1 = str(raw_main.get("sha1", "") or "").strip()

        if isinstance(raw_chats, str):
            chats_file_id = raw_chats.strip()
        elif isinstance(raw_chats, dict):
            chats_file_id = str(raw_chats.get("file_id", "") or "").strip()
            chats_sha1 = str(raw_chats.get("sha1", "") or "").strip()

    if main_file_id and main_sha1 and current_main_sha1 and main_sha1 == current_main_sha1:
        MAIN_MENU_PHOTO_FILE_ID = main_file_id
    else:
        MAIN_MENU_PHOTO_FILE_ID = None

    if chats_file_id and chats_sha1 and current_chats_sha1 and chats_sha1 == current_chats_sha1:
        CHATS_MENU_PHOTO_FILE_ID = chats_file_id
    else:
        CHATS_MENU_PHOTO_FILE_ID = None

    MAIN_MENU_PHOTO_SHA1 = current_main_sha1
    CHATS_MENU_PHOTO_SHA1 = current_chats_sha1

    # Rewrite old/invalid cache shape to the new format.
    with contextlib.suppress(Exception):
        _save_ui_photo_cache()


def _get_cached_photo_file_id(photo_ref: str) -> str | None:
    global MAIN_MENU_PHOTO_FILE_ID, CHATS_MENU_PHOTO_FILE_ID
    global MAIN_MENU_PHOTO_SHA1, CHATS_MENU_PHOTO_SHA1

    key = _photo_cache_key(photo_ref)
    if key == "main":
        current_sha1 = _file_sha1(MAIN_MENU_IMAGE_PATH)
        if current_sha1 and MAIN_MENU_PHOTO_SHA1 and current_sha1 != MAIN_MENU_PHOTO_SHA1:
            MAIN_MENU_PHOTO_FILE_ID = None
            MAIN_MENU_PHOTO_SHA1 = current_sha1
            with contextlib.suppress(Exception):
                _save_ui_photo_cache()
        return MAIN_MENU_PHOTO_FILE_ID
    if key == "chats":
        current_sha1 = _file_sha1(CHATS_MENU_IMAGE_PATH)
        if current_sha1 and CHATS_MENU_PHOTO_SHA1 and current_sha1 != CHATS_MENU_PHOTO_SHA1:
            CHATS_MENU_PHOTO_FILE_ID = None
            CHATS_MENU_PHOTO_SHA1 = current_sha1
            with contextlib.suppress(Exception):
                _save_ui_photo_cache()
        return CHATS_MENU_PHOTO_FILE_ID
    return None


def _set_cached_photo_file_id(photo_ref: str, file_id: str | None) -> None:
    global MAIN_MENU_PHOTO_FILE_ID, CHATS_MENU_PHOTO_FILE_ID
    global MAIN_MENU_PHOTO_SHA1, CHATS_MENU_PHOTO_SHA1

    key = _photo_cache_key(photo_ref)
    if key == "main":
        MAIN_MENU_PHOTO_FILE_ID = file_id
        MAIN_MENU_PHOTO_SHA1 = _file_sha1(MAIN_MENU_IMAGE_PATH)
    elif key == "chats":
        CHATS_MENU_PHOTO_FILE_ID = file_id
        CHATS_MENU_PHOTO_SHA1 = _file_sha1(CHATS_MENU_IMAGE_PATH)
    with contextlib.suppress(Exception):
        _save_ui_photo_cache()


def _extract_message_photo_file_id(message: types.Message) -> str | None:
    photos = getattr(message, "photo", None) or []
    if not photos:
        return None
    file_id = getattr(photos[-1], "file_id", None)
    if isinstance(file_id, str) and file_id:
        return file_id
    return None


def _resolve_photo_source(photo: str) -> str | FSInputFile | None:
    ref = (photo or "").strip()
    if not ref:
        return None
    full_path = os.path.abspath(ref)
    if os.path.isfile(full_path):
        return FSInputFile(full_path)
    return ref


async def _send_text_or_photo(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    photo: str | None,
) -> types.Message:
    photo_ref = (photo or "").strip()
    if not photo_ref:
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    cached_file_id = _get_cached_photo_file_id(photo_ref)
    if cached_file_id:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=cached_file_id,
                caption=text,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            logger.debug("Cached photo file_id is invalid for %s: %s", photo_ref, exc)
            _set_cached_photo_file_id(photo_ref, None)

    photo_source = _resolve_photo_source(photo_ref)
    if photo_source is None:
        logger.warning("Photo source not found for send: %s", photo_ref)
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_source,
            caption=text,
            reply_markup=reply_markup,
        )
        cached_from_send = _extract_message_photo_file_id(sent)
        if cached_from_send:
            _set_cached_photo_file_id(photo_ref, cached_from_send)
        return sent
    except TelegramBadRequest as exc:
        if is_telegram_url_fetch_error(exc) and photo_ref.startswith(("http://", "https://")):
            file_name = _filename_from_url(photo_ref, "screen.jpg")
            temp_path = await download_media_to_temp(photo_ref, file_name)
            try:
                sent = await bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(temp_path),
                    caption=text,
                    reply_markup=reply_markup,
                )
                cached_from_send = _extract_message_photo_file_id(sent)
                if cached_from_send:
                    _set_cached_photo_file_id(photo_ref, cached_from_send)
                return sent
            finally:
                with contextlib.suppress(Exception):
                    os.remove(temp_path)
        logger.warning("Failed to send photo screen %s: %s", photo_ref, exc)
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def _edit_photo_message(
    message: types.Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    photo: str,
) -> bool:
    photo_ref = (photo or "").strip()
    if not photo_ref:
        return False

    cached_file_id = _get_cached_photo_file_id(photo_ref)
    if cached_file_id:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=cached_file_id, caption=text, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
            return True
        except TelegramBadRequest as exc:
            lowered = str(exc).lower()
            if "message is not modified" in lowered:
                return True
            logger.debug("Cached edit_media photo file_id failed for %s: %s", photo_ref, exc)
            _set_cached_photo_file_id(photo_ref, None)

    media_source = _resolve_photo_source(photo_ref)
    if media_source is None:
        return False

    try:
        edited = await message.edit_media(
            media=InputMediaPhoto(media=media_source, caption=text, parse_mode="HTML"),
            reply_markup=reply_markup,
        )
        cached_from_message = (
            _extract_message_photo_file_id(edited)
            if isinstance(edited, types.Message)
            else None
        )
        if cached_from_message:
            _set_cached_photo_file_id(photo_ref, cached_from_message)
        return True
    except TelegramBadRequest as exc:
        lowered = str(exc).lower()
        if "message is not modified" in lowered:
            return True
        if is_telegram_url_fetch_error(exc) and photo_ref.startswith(("http://", "https://")):
            file_name = _filename_from_url(photo_ref, "screen.jpg")
            temp_path = await download_media_to_temp(photo_ref, file_name)
            try:
                await message.edit_media(
                    media=InputMediaPhoto(
                        media=FSInputFile(temp_path),
                        caption=text,
                        parse_mode="HTML",
                    ),
                    reply_markup=reply_markup,
                )
                return True
            except TelegramBadRequest as nested_exc:
                if "message is not modified" in str(nested_exc).lower():
                    return True
            finally:
                with contextlib.suppress(Exception):
                    os.remove(temp_path)
        return False


async def switch_screen_message(
    source_message: types.Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    photo: str | None = None,
) -> types.Message:
    source_is_photo = _message_has_photo_or_video(source_message)
    target_is_photo = bool((photo or "").strip())

    if target_is_photo:
        if source_is_photo:
            current_photo_file_id = _extract_message_photo_file_id(source_message)
            target_cached_file_id = _get_cached_photo_file_id(str(photo))
            if (
                current_photo_file_id
                and target_cached_file_id
                and current_photo_file_id == target_cached_file_id
            ):
                try:
                    await source_message.edit_caption(caption=text, reply_markup=reply_markup)
                    return source_message
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        return source_message

            if await _edit_photo_message(source_message, text, reply_markup, str(photo)):
                return source_message
            with contextlib.suppress(Exception):
                await source_message.delete()
            return await _send_text_or_photo(
                chat_id=source_message.chat.id,
                text=text,
                reply_markup=reply_markup,
                photo=photo,
            )

        with contextlib.suppress(Exception):
            await source_message.delete()
        return await _send_text_or_photo(
            chat_id=source_message.chat.id,
            text=text,
            reply_markup=reply_markup,
            photo=photo,
        )

    if source_is_photo:
        with contextlib.suppress(Exception):
            await source_message.delete()
        return await bot.send_message(
            chat_id=source_message.chat.id,
            text=text,
            reply_markup=reply_markup,
        )

    try:
        await source_message.edit_text(text=text, reply_markup=reply_markup)
        return source_message
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return source_message
        return await bot.send_message(
            chat_id=source_message.chat.id,
            text=text,
            reply_markup=reply_markup,
        )


async def send_main_menu_message(chat_id: int, has_token: bool, name: str) -> types.Message:
    return await _send_text_or_photo(
        chat_id=chat_id,
        text=main_menu_text(has_token, name),
        reply_markup=main_menu_keyboard(has_token),
        photo=MAIN_MENU_IMAGE_PATH,
    )


async def edit_message_no_fallback(
    message: types.Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        if getattr(message, "photo", None) or getattr(message, "video", None):
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        lowered = str(exc).lower()
        if "message is not modified" in lowered:
            return
        logger.debug("Skip message edit without fallback: %s", exc)


def remember_user(user: types.User) -> None:
    session_manager.register_telegram_user(
        tg_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def clear_user_runtime_cache(tg_user_id: int) -> None:
    clear_active_chat_view(tg_user_id)
    CHAT_CACHE.pop(tg_user_id, None)

    media_keys = [key for key, item in MEDIA_CACHE.items() if item.tg_user_id == tg_user_id]
    for key in media_keys:
        MEDIA_CACHE.pop(key, None)

    media_index_keys = [
        request_key
        for request_key, token in MEDIA_REQUEST_INDEX.items()
        if token in media_keys
    ]
    for request_key in media_index_keys:
        MEDIA_REQUEST_INDEX.pop(request_key, None)

    anchor_keys = [key for key in HISTORY_ANCHORS if key[0] == tg_user_id]
    for key in anchor_keys:
        HISTORY_ANCHORS.pop(key, None)

    seen_keys = [key for key in UPDATE_LAST_SEEN if key[0] == tg_user_id]
    for key in seen_keys:
        UPDATE_LAST_SEEN.pop(key, None)

    unread_keys = [key for key in UNREAD_COUNTS if key[0] == tg_user_id]
    for key in unread_keys:
        UNREAD_COUNTS.pop(key, None)


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


def render_self_profile_text(user: Any, me: Any) -> tuple[str, str]:
    me_id = parse_int(str(getattr(me, "id", 0)), default=0)
    name = user_display_name(user or me, "Пользователь")
    description = str(getattr(user, "description", "") or "").strip()
    phone = str(getattr(me, "phone", "") or "").strip()
    link = str(getattr(user, "link", "") or "").strip()
    avatar_url = str(getattr(user, "base_url", "") or "").strip()
    avatar_raw = str(getattr(user, "base_raw_url", "") or "").strip()
    avatar = avatar_url or avatar_raw

    lines = [
        "<b>Твой профиль MAX</b>",
        f"Имя: <b>{esc(name)}</b>",
    ]
    if me_id:
        lines.append(f"MAX ID: <code>{esc(me_id)}</code>")
    if description:
        lines.append(f"Описание: {esc(description)}")
    if phone:
        lines.append(f"Номер: <code>{esc(phone)}</code>")
    else:
        lines.append("Номер: <i>скрыт</i>")
    if link.startswith(("http://", "https://")):
        lines.append(f"Профиль в MAX: {make_link(link, 'открыть')}")

    return "\n".join(lines), avatar


async def show_profile_card(
    source_message: types.Message,
    text: str,
    keyboard: InlineKeyboardMarkup,
    avatar: str,
) -> None:
    avatar_ref = (avatar or "").strip()
    await switch_screen_message(
        source_message,
        text=text,
        reply_markup=keyboard,
        photo=avatar_ref or None,
    )


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
    has_newer = offset > 0

    entries = await get_chat_entries(tg_user_id, client)
    chat_title = resolve_chat_title(chat_id, entries)
    chat_entry = resolve_chat_entry(chat_id, entries)
    show_members = bool(
        chat_entry and chat_entry.chat_type.upper() == "CHAT" and chat_entry.participants
    )
    me_id = parse_int(str(getattr(client.me, "id", 0)), default=0)
    profile_user_id: int | None = None
    if chat_entry and chat_entry.chat_type.upper() == "DIALOG":
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
        sender_id = parse_int(str(getattr(msg, "sender", 0)), default=0)
        sender_name = user_display_name(users_map.get(msg.sender), f"Пользователь {msg.sender}")
        message_id = parse_int(str(getattr(msg, "id", 0)), default=0)
        delete_markup = ""
        if sender_id > 0 and sender_id == me_id and message_id > 0:
            delete_markup = delete_message_command_markup(chat_id, message_id)
        attachment_lines = await render_attachment_lines(tg_user_id, client, chat_id, msg)
        body = (msg.text or "").strip()

        header_prefix = f"{delete_markup} " if delete_markup else ""
        block_lines = [f"{header_prefix}<b>{esc(sender_name)}</b> <code>{time_label(msg.time)}</code>"]
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

        rendered_blocks.append("<blockquote expandable>" + "\n".join(block_lines) + "</blockquote>")

    separator = "\n\n"
    max_body_len = 3600
    selected_rev: list[str] = []
    selected_len = 0
    for block in reversed(rendered_blocks):
        extra = len(block) + (len(separator) if selected_rev else 0)
        if selected_len + extra > max_body_len and selected_rev:
            break
        if selected_len + extra > max_body_len and not selected_rev:
            cut = max(200, max_body_len - 80)
            trimmed = block[:cut] + "\n<i>…сообщение обрезано из-за лимита Telegram</i>"
            selected_rev.append(trimmed)
            selected_len = len(trimmed)
            break
        selected_rev.append(block)
        selected_len += extra

    selected_blocks = list(reversed(selected_rev))
    shown_count = len(selected_blocks)
    hidden_due_limit = max(0, len(rendered_blocks) - shown_count)
    has_more = start_index > 0 or hidden_due_limit > 0
    page_step = shown_count if shown_count > 0 else 1

    if not page_messages:
        content = (
            f"💬 <b>{esc(chat_title)}</b>\n"
            f"<i>В этом чате пока нет сообщений.</i>"
        )
    else:
        start_no = end_index - shown_count + 1
        end_no = end_index
        range_note = (
            f"<i>Сообщения {start_no}-{end_no} из {total_messages}. Новые внизу.</i>"
            if hidden_due_limit == 0
            else f"<i>Сообщения {start_no}-{end_no} из {total_messages}. "
            "Показаны самые новые, остальное через «Старее».</i>"
        )
        content = (
            f"💬 <b>{esc(chat_title)}</b>\n"
            f"{range_note}\n\n"
            f"{separator.join(selected_blocks)}"
        )

    active_view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
    auto_paused = bool(
        offset == 0
        and active_view is not None
        and active_view.chat_id == chat_id
        and active_view.paused
    )

    keyboard = build_history_keyboard(
        chat_id=chat_id,
        offset=offset,
        chat_page=chat_page,
        has_older=has_more,
        has_newer=has_newer,
        page_step=page_step,
        show_members=show_members,
        profile_user_id=profile_user_id,
        auto_refresh_paused=auto_paused,
    )
    return content, keyboard


async def send_media_by_token(message: types.Message, token: str) -> None:
    cleanup_media_cache()
    request = MEDIA_CACHE.get(token)
    if request is None:
        await message.answer("Ссылка для получения медиа устарела. Открой чат заново и нажми новую ссылку.",
                             reply_markup=dismiss_message_keyboard())
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
    fallback_url = request.url
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
            fallback_url = url
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
            candidates: list[str] = []
            url = getattr(video_req, "url", None)
            if isinstance(url, str) and url:
                candidates.append(url)
            external = getattr(video_req, "external", None)
            if isinstance(external, str) and external.startswith(("http://", "https://")):
                if external not in candidates:
                    candidates.append(external)

            if not candidates:
                raise ValueError("MAX не вернул ссылку на видео")
            last_error: Exception | None = None
            for candidate in candidates:
                fallback_url = candidate
                try:
                    await message.answer_video(
                        video=candidate,
                        caption=caption,
                        reply_markup=dismiss_message_keyboard(),
                    )
                    return
                except TelegramBadRequest as exc:
                    last_error = exc
                    if not is_telegram_url_fetch_error(exc):
                        continue

                    file_name = _filename_from_url(candidate, "video.mp4")
                    path = await download_media_to_temp(candidate, file_name)
                    try:
                        try:
                            await message.answer_video(
                                video=FSInputFile(path, filename=file_name),
                                caption=caption,
                                reply_markup=dismiss_message_keyboard(),
                            )
                            return
                        except TelegramBadRequest:
                            await message.answer_document(
                                document=FSInputFile(path, filename=file_name),
                                caption=caption,
                                reply_markup=dismiss_message_keyboard(),
                            )
                            return
                    finally:
                        with contextlib.suppress(Exception):
                            os.remove(path)
                except Exception as exc:
                    last_error = exc
                    continue

            if last_error:
                raise last_error
            raise ValueError("Не удалось отправить видео")

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
        fallback = fallback_url
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


async def delete_max_message_by_link(message: types.Message, chat_id: int, message_id: int) -> None:
    try:
        client = await session_manager.ensure_client(message.from_user.id)
        await client.delete_message(chat_id=chat_id, message_ids=[message_id], for_me=False)
    except Exception as exc:
        logger.warning(
            "Failed to delete MAX message user=%s chat=%s message=%s: %s",
            message.from_user.id,
            chat_id,
            message_id,
            exc,
        )
        failed_notice = await message.answer("Ошибка, попробуйте позже")
        await asyncio.sleep(2)
        with contextlib.suppress(Exception):
            await failed_notice.delete()
        return

    ok_notice = await message.answer("Сообщение удалено!")
    HISTORY_ANCHORS.pop((message.from_user.id, chat_id), None)
    refreshed = await refresh_active_chat_view_now(message.from_user.id, expected_chat_id=chat_id)
    if not refreshed:
        with contextlib.suppress(Exception):
            await session_manager.disconnect_client(message.from_user.id)
        HISTORY_ANCHORS.pop((message.from_user.id, chat_id), None)
        await refresh_active_chat_view_now(message.from_user.id, expected_chat_id=chat_id)
    await asyncio.sleep(0.7)
    HISTORY_ANCHORS.pop((message.from_user.id, chat_id), None)
    await refresh_active_chat_view_now(message.from_user.id, expected_chat_id=chat_id)
    await asyncio.sleep(1.3)
    with contextlib.suppress(Exception):
        await ok_notice.delete()


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

        active_view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
        is_live_view = bool(
            active_view
            and active_view.chat_id == entry.chat_id
            and active_view.offset == 0
            and not active_view.paused
        )

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
        incoming_count = 0
        for msg in new_messages:
            msg_time = int(getattr(msg, "time", 0) or 0)
            if msg_time > max_sent_time:
                max_sent_time = msg_time

            sender_id = parse_int(str(getattr(msg, "sender", 0)), default=0)
            if sender_id and sender_id == me_id:
                continue

            incoming_count += 1
            if is_live_view:
                continue

            sender_name = user_display_name(users_map.get(sender_id), f"Пользователь {sender_id}")
            body = summarize_update_body(msg)
            notify_text = (
                f"🔔 <b>Новое в {esc(entry.title)}</b>\n"
                f"<b>{esc(sender_name)}</b> <code>{time_label(msg_time)}</code>\n"
                f"{esc(body)}"
            )
            try:
                await bot.send_message(
                    chat_id=tg_user_id,
                    text=notify_text,
                    reply_markup=update_notification_keyboard(entry.chat_id),
                )
                METRICS.update_notifications += 1
            except Exception as exc:
                logger.debug("Could not deliver update to tg=%s: %s", tg_user_id, exc)

        if incoming_count > 0:
            if is_live_view:
                mark_chat_read(tg_user_id, entry.chat_id, seen_time=max(max_sent_time, last_event))
            else:
                increment_unread_count(tg_user_id, entry.chat_id, incoming_count)

        UPDATE_LAST_SEEN[key] = max(max_sent_time, last_event)


async def refresh_active_chat_for_user(tg_user_id: int) -> None:
    view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
    if view is None or view.paused or view.offset != 0 or view.in_progress:
        return

    if time.time() - view.last_refresh_at < max(1.0, CHAT_AUTORELOAD_SECONDS - 0.4):
        return

    view.in_progress = True
    try:
        client = await session_manager.ensure_client(tg_user_id)
        text, keyboard = await build_history_text(
            tg_user_id=tg_user_id,
            client=client,
            chat_id=view.chat_id,
            offset=0,
            chat_page=view.chat_page,
        )
        signature = chat_view_signature(text, keyboard)

        if signature != view.signature:
            try:
                await bot.edit_message_text(
                    chat_id=view.tg_chat_id,
                    message_id=view.tg_message_id,
                    text=text,
                    reply_markup=keyboard,
                )
            except TelegramBadRequest as exc:
                lowered = str(exc).lower()
                if "message is not modified" not in lowered:
                    if "message to edit not found" in lowered or "message can't be edited" in lowered:
                        clear_active_chat_view(tg_user_id)
                        return
                    logger.debug("Auto-refresh edit failed user=%s: %s", tg_user_id, exc)

        current_view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
        if current_view and current_view.tg_message_id == view.tg_message_id:
            current_view.signature = signature
            current_view.last_refresh_at = time.time()

        mark_chat_read(tg_user_id, view.chat_id)
    except Exception as exc:
        logger.debug("Skip chat auto-refresh for %s: %s", tg_user_id, exc)
    finally:
        current_view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
        if current_view and current_view.tg_message_id == view.tg_message_id:
            current_view.in_progress = False


async def refresh_active_chat_view_now(tg_user_id: int, expected_chat_id: int | None = None) -> bool:
    view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
    if view is None or view.in_progress:
        return False
    if expected_chat_id is not None and view.chat_id != expected_chat_id:
        return False

    was_updated = False
    view.in_progress = True
    try:
        previous_signature = view.signature
        client = await session_manager.ensure_client(tg_user_id)
        text, keyboard = await build_history_text(
            tg_user_id=tg_user_id,
            client=client,
            chat_id=view.chat_id,
            offset=view.offset,
            chat_page=view.chat_page,
        )
        signature = chat_view_signature(text, keyboard)
        if signature != view.signature:
            try:
                await bot.edit_message_text(
                    chat_id=view.tg_chat_id,
                    message_id=view.tg_message_id,
                    text=text,
                    reply_markup=keyboard,
                )
                was_updated = True
            except TelegramBadRequest as exc:
                lowered = str(exc).lower()
                if "message is not modified" not in lowered:
                    if "message to edit not found" in lowered or "message can't be edited" in lowered:
                        clear_active_chat_view(tg_user_id)
                        return False
                    logger.debug("Manual chat refresh failed user=%s: %s", tg_user_id, exc)
        else:
            was_updated = signature != previous_signature

        current_view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
        if current_view and current_view.tg_message_id == view.tg_message_id:
            current_view.signature = signature
            current_view.last_refresh_at = time.time()

        if view.offset == 0:
            mark_chat_read(tg_user_id, view.chat_id)
    except Exception as exc:
        logger.debug("Skip manual chat refresh for %s: %s", tg_user_id, exc)
        return False
    finally:
        current_view = ACTIVE_CHAT_VIEWS.get(tg_user_id)
        if current_view and current_view.tg_message_id == view.tg_message_id:
            current_view.in_progress = False
    return was_updated


async def chat_refresh_loop() -> None:
    while True:
        try:
            for tg_user_id in list(ACTIVE_CHAT_VIEWS.keys()):
                await refresh_active_chat_for_user(tg_user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background chat refresh loop failed")
        await asyncio.sleep(CHAT_AUTORELOAD_SECONDS)


async def flush_outgoing_for_user(tg_user_id: int) -> None:
    queued = session_manager.fetch_outgoing_messages(tg_user_id, limit=QUEUE_BATCH_SIZE)
    if not queued:
        return

    try:
        client = await session_manager.ensure_client(tg_user_id)
    except Exception as exc:
        logger.debug("Skip queue flush for %s: %s", tg_user_id, exc)
        return

    for item in queued:
        queue_id = parse_int(str(item.get("id", "0")), default=0)
        chat_id = parse_int(str(item.get("chat_id", "0")), default=0)
        text = str(item.get("text") or " ").strip() or " "
        attachment_path = str(item.get("attachment_path") or "").strip()
        if queue_id <= 0 or chat_id <= 0:
            if queue_id > 0:
                session_manager.mark_outgoing_message_sent(queue_id)
            continue

        try:
            attachment = build_outgoing_attachment_from_queue(item)
            started_at = time.perf_counter()
            await client.send_message(
                text=text,
                chat_id=chat_id,
                attachment=attachment,
            )
            record_send_latency((time.perf_counter() - started_at) * 1000)
            METRICS.queue_sent += 1
            session_manager.mark_outgoing_message_sent(queue_id)
            HISTORY_ANCHORS.pop((tg_user_id, chat_id), None)
            if attachment_path:
                with contextlib.suppress(Exception):
                    os.remove(attachment_path)
        except Exception as exc:
            record_send_error("queue", tg_user_id, chat_id, exc)
            logger.warning(
                "Queued send failed user=%s chat=%s queue_id=%s: %s",
                tg_user_id,
                chat_id,
                queue_id,
                exc,
            )
            session_manager.mark_outgoing_message_attempt(queue_id, str(exc))
            if isinstance(exc, FileNotFoundError):
                session_manager.mark_outgoing_message_sent(queue_id)
                continue
            if is_temporary_send_error(exc):
                with contextlib.suppress(Exception):
                    await session_manager.disconnect_client(tg_user_id)
                break


async def outgoing_queue_loop() -> None:
    while True:
        try:
            for tg_user_id in session_manager.get_outgoing_user_ids():
                await flush_outgoing_for_user(tg_user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background outgoing queue loop failed")
        await asyncio.sleep(QUEUE_RETRY_SECONDS)


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
    delete_target = parse_delete_start_payload(start_payload)
    if delete_target is not None:
        chat_id, message_id = delete_target
        await session_manager.clear_auth_flow(message.from_user.id)
        await state.clear()
        with contextlib.suppress(Exception):
            await message.delete()
        await delete_max_message_by_link(message, chat_id, message_id)
        return

    if start_payload.startswith("media_"):
        token = start_payload[6:]
        await session_manager.clear_auth_flow(message.from_user.id)
        await state.clear()
        clear_active_chat_view(message.from_user.id)
        with contextlib.suppress(Exception):
            await message.delete()
        await send_media_by_token(message, token)
        return

    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    clear_active_chat_view(message.from_user.id)

    has_token = session_manager.has_token(message.from_user.id)
    name = " ".join(
        part for part in [message.from_user.first_name, message.from_user.last_name] if part
    ).strip() or "друг"
    await send_main_menu_message(message.chat.id, has_token, name)


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    clear_active_chat_view(message.from_user.id)
    has_token = session_manager.has_token(message.from_user.id)
    name = " ".join(
        part for part in [message.from_user.first_name, message.from_user.last_name] if part
    ).strip() or "друг"
    await send_main_menu_message(message.chat.id, has_token, name)


@dp.message(Command("login"))
async def cmd_login(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    clear_active_chat_view(message.from_user.id)
    has_token = session_manager.has_token(message.from_user.id)
    await message.answer(auth_menu_text(has_token), reply_markup=auth_methods_keyboard(has_token))


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await session_manager.clear_auth_flow(message.from_user.id)
    await state.clear()
    clear_active_chat_view(message.from_user.id)
    has_token = session_manager.has_token(message.from_user.id)
    name = " ".join(
        part for part in [message.from_user.first_name, message.from_user.last_name] if part
    ).strip() or "друг"
    await send_main_menu_message(message.chat.id, has_token, name)


@dp.message(Command("health"))
async def cmd_health(message: types.Message) -> None:
    remember_user(message.from_user)
    if not is_admin_user(message.from_user.id):
        return
    await message.answer(render_health_text())


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    remember_user(message.from_user)
    if not is_admin_user(message.from_user.id):
        return
    await message.answer(render_stats_text())


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message) -> None:
    remember_user(message.from_user)
    with contextlib.suppress(Exception):
        await message.delete()
    if not is_admin_user(message.from_user.id):
        return

    clear_active_chat_view(message.from_user.id)
    await message.answer(
        render_admin_panel_text(),
        reply_markup=admin_panel_keyboard(),
    )


@dp.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.clear()
    clear_active_chat_view(callback.from_user.id)
    has_token = session_manager.has_token(callback.from_user.id)
    name = " ".join(
        part for part in [callback.from_user.first_name, callback.from_user.last_name] if part
    ).strip() or "друг"
    text = main_menu_text(has_token, name)
    keyboard = main_menu_keyboard(has_token)
    await switch_screen_message(
        callback.message,
        text=text,
        reply_markup=keyboard,
        photo=MAIN_MENU_IMAGE_PATH,
    )
    await callback.answer()


@dp.callback_query(F.data == "profile:me")
async def cb_profile_me(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.clear()
    clear_active_chat_view(callback.from_user.id)

    has_token = session_manager.has_token(callback.from_user.id)
    if not has_token:
        name = " ".join(
            part for part in [callback.from_user.first_name, callback.from_user.last_name] if part
        ).strip() or "друг"
        text = (
            "<b>Твой профиль</b>\n"
            f"Telegram: <b>{esc(name)}</b>\n"
            "MAX: <b>не подключен ❌</b>\n\n"
            "Нажми «Войти в MAX», чтобы подключить аккаунт."
        )
        await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=self_profile_keyboard(False),
            photo=None,
        )
        await callback.answer()
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        me = getattr(client, "me", None)
        if me is None:
            raise ValueError("Не удалось получить профиль MAX")

        me_id = parse_int(str(getattr(me, "id", 0)), default=0)
        profile = None
        if me_id:
            users = await client.get_users([me_id])
            profile = users[0] if users else None

        text, avatar = render_self_profile_text(profile, me)
        await show_profile_card(
            source_message=callback.message,
            text=text,
            keyboard=self_profile_keyboard(True),
            avatar=avatar,
        )
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open self profile for %s", callback.from_user.id)
        await callback.answer("Ошибка загрузки профиля", show_alert=True)
        await callback.message.answer(f"Не удалось открыть профиль: <code>{esc(exc)}</code>")


@dp.callback_query(F.data == "logout:confirm")
async def cb_logout_confirm(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)
    await switch_screen_message(
        callback.message,
        text="<b>Выход из MAX</b>\n\nПодтвердить выход из аккаунта?",
        reply_markup=logout_confirm_keyboard(),
        photo=None,
    )
    await callback.answer()


@dp.callback_query(F.data == "logout:cancel")
async def cb_logout_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.clear()
    clear_active_chat_view(callback.from_user.id)

    has_token = session_manager.has_token(callback.from_user.id)
    if not has_token:
        name = " ".join(
            part for part in [callback.from_user.first_name, callback.from_user.last_name] if part
        ).strip() or "друг"
        text = (
            "<b>Твой профиль</b>\n"
            f"Telegram: <b>{esc(name)}</b>\n"
            "MAX: <b>не подключен ❌</b>\n\n"
            "Нажми «Войти в MAX», чтобы подключить аккаунт."
        )
        await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=self_profile_keyboard(False),
            photo=None,
        )
        await callback.answer()
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        me = getattr(client, "me", None)
        if me is None:
            raise ValueError("Не удалось получить профиль MAX")

        me_id = parse_int(str(getattr(me, "id", 0)), default=0)
        profile = None
        if me_id:
            users = await client.get_users([me_id])
            profile = users[0] if users else None

        text, _ = render_self_profile_text(profile, me)
        await safe_edit_message(
            callback.message,
            text,
            self_profile_keyboard(True),
        )
        await callback.answer()
    except Exception:
        logger.exception("Failed to return to self profile on logout cancel for %s", callback.from_user.id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.callback_query(F.data == "logout:yes")
async def cb_logout_yes(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    await state.clear()
    await session_manager.clear_auth_flow(callback.from_user.id)

    try:
        await session_manager.logout(callback.from_user.id)
    except Exception as exc:
        logger.warning("MAX logout failed for %s: %s", callback.from_user.id, exc)

    clear_user_runtime_cache(callback.from_user.id)
    name = " ".join(
        part for part in [callback.from_user.first_name, callback.from_user.last_name] if part
    ).strip() or "друг"
    await switch_screen_message(
        callback.message,
        text="✅ Ты вышел из MAX.\n\n" + main_menu_text(False, name),
        reply_markup=main_menu_keyboard(False),
        photo=MAIN_MENU_IMAGE_PATH,
    )
    await callback.answer("Выход выполнен")


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
    clear_active_chat_view(callback.from_user.id)
    has_token = session_manager.has_token(callback.from_user.id)
    await switch_screen_message(
        callback.message,
        text=auth_menu_text(has_token),
        reply_markup=auth_methods_keyboard(has_token),
        photo=None,
    )
    await callback.answer()


@dp.callback_query(F.data == "auth:token")
async def cb_auth_token(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer()
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.set_state(UserFlow.waiting_for_token)
    with contextlib.suppress(Exception):
        await callback.message.delete()
    sent_ids = await send_token_instructions(callback.message)
    await state.update_data(auth_instruction_message_ids=sent_ids)


@dp.callback_query(F.data == "auth:qr")
@dp.callback_query(F.data == "auth:qr:refresh")
async def cb_auth_qr(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)
    await state.clear()

    if callback.data == "auth:qr":
        with contextlib.suppress(Exception):
            await callback.message.delete()
        wait_message = await callback.message.answer("Генерирую новый QR-код…")
    else:
        wait_message = callback.message
        await edit_message_no_fallback(wait_message, "Генерирую новый QR-код…")

    try:
        data = await session_manager.begin_qr_login(callback.from_user.id)
        await edit_message_no_fallback(
            wait_message,
            qr_help_text(
                qr_link=str(data["qr_link"]),
                expires_at=int(data["expires_at"]),
            ),
            reply_markup=qr_auth_keyboard(),
        )
    except Exception as exc:
        await edit_message_no_fallback(
            wait_message,
            f"❌ {esc(exc)}\n\n"
            "Попробуй снова или выбери другой способ входа.",
            reply_markup=auth_methods_keyboard(session_manager.has_token(callback.from_user.id)),
        )
    await callback.answer()


@dp.callback_query(F.data == "auth:qr:check")
async def cb_auth_qr_check(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)

    try:
        status, token = await session_manager.check_qr_login(callback.from_user.id)
    except Exception as exc:
        await edit_message_no_fallback(
            callback.message,
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
        await edit_message_no_fallback(
            callback.message,
            "⌛️ Срок QR-кода истек. Нажми «Обновить QR».",
            reply_markup=qr_auth_keyboard(),
        )
        await callback.answer()
        return

    if status == "ready" and token:
        await edit_message_no_fallback(callback.message, "Подтверждаю вход и сохраняю токен…")
        try:
            await session_manager.validate_and_save_token(callback.from_user.id, token)
            CHAT_CACHE.pop(callback.from_user.id, None)
            await state.clear()
            await edit_message_no_fallback(
                callback.message,
                "✅ MAX успешно подключен через QR.",
                reply_markup=main_menu_keyboard(True),
            )
        except Exception as exc:
            await edit_message_no_fallback(
                callback.message,
                f"❌ {esc(exc)}\n\n"
                "Попробуй снова или выбери вход по токену.",
                reply_markup=auth_methods_keyboard(session_manager.has_token(callback.from_user.id)),
            )
    await callback.answer()


@dp.callback_query(F.data == "token:set")
async def cb_token_set(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer()
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.set_state(UserFlow.waiting_for_token)
    with contextlib.suppress(Exception):
        await callback.message.delete()
    sent_ids = await send_token_instructions(callback.message)
    await state.update_data(auth_instruction_message_ids=sent_ids)


@dp.callback_query(F.data == "flow:cancel")
async def cb_flow_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    current_state = await state.get_state()
    is_token_flow = current_state == UserFlow.waiting_for_token.state
    is_write_flow = current_state == UserFlow.waiting_for_chat_message.state

    if is_write_flow:
        data = await state.get_data()
        chat_id = parse_int(str(data.get("chat_id", "0")), default=0)
        chat_page = max(0, parse_int(str(data.get("chat_page", "0")), default=0))
        await state.clear()
        with contextlib.suppress(Exception):
            await callback.answer("Отменено")
        if chat_id > 0:
            try:
                client = await session_manager.ensure_client(callback.from_user.id)
                text, keyboard = await build_history_text(
                    tg_user_id=callback.from_user.id,
                    client=client,
                    chat_id=chat_id,
                    offset=0,
                    chat_page=chat_page,
                )
                await safe_edit_message(callback.message, text, keyboard)
                set_active_chat_view(
                    tg_user_id=callback.from_user.id,
                    tg_chat_id=callback.message.chat.id,
                    tg_message_id=callback.message.message_id,
                    chat_id=chat_id,
                    chat_page=chat_page,
                    offset=0,
                    signature=chat_view_signature(text, keyboard),
                    paused=False,
                )
                mark_chat_read(callback.from_user.id, chat_id)
                return
            except Exception as exc:
                logger.warning(
                    "Failed to restore chat view on cancel for user=%s chat=%s: %s",
                    callback.from_user.id,
                    chat_id,
                    exc,
                )
        with contextlib.suppress(Exception):
            await callback.message.delete()
        return

    await cleanup_auth_instruction_messages(state, callback.message.chat.id)
    await session_manager.clear_auth_flow(callback.from_user.id)
    await state.clear()
    clear_active_chat_view(callback.from_user.id)
    with contextlib.suppress(Exception):
        await callback.message.delete()
    if is_token_flow:
        has_token = session_manager.has_token(callback.from_user.id)
        await callback.message.answer(
            auth_menu_text(has_token),
            reply_markup=auth_methods_keyboard(has_token),
        )
    await callback.answer("Отменено")


@dp.callback_query(F.data == "msg:close")
async def cb_msg_close(callback: types.CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def cb_admin_panel(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    clear_active_chat_view(callback.from_user.id)
    action = parts[1]
    if action == "panel":
        text = render_admin_panel_text()
        keyboard = admin_panel_keyboard()
    elif action == "health":
        text = render_health_text()
        keyboard = admin_back_keyboard()
    elif action == "stats":
        text = render_stats_text()
        keyboard = admin_back_keyboard()
    elif action == "users":
        text = render_admin_users_text()
        keyboard = admin_back_keyboard()
    elif action == "queue":
        text = render_admin_queue_text()
        keyboard = admin_back_keyboard()
    else:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    await safe_edit_message(callback.message, text, keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("notify:read:"))
async def cb_notify_read(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[2], default=0)
    if chat_id <= 0:
        await callback.answer("Чат не найден", show_alert=True)
        return

    mark_chat_read(callback.from_user.id, chat_id)
    with contextlib.suppress(Exception):
        await callback.message.delete()
    await callback.answer("Прочитано")


@dp.callback_query(F.data.startswith("notify:open:"))
async def cb_notify_open(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    chat_id = parse_int(parts[2], default=0)
    if chat_id <= 0:
        await callback.answer("Чат не найден", show_alert=True)
        return

    if not session_manager.has_token(callback.from_user.id):
        await callback.answer("Сначала авторизуйся в MAX", show_alert=True)
        await switch_screen_message(
            callback.message,
            text=auth_menu_text(False),
            reply_markup=auth_methods_keyboard(False),
            photo=None,
        )
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        entries = await get_chat_entries(callback.from_user.id, client, force_refresh=False)
        chat_page = resolve_chat_page(entries, chat_id)
        text, keyboard = await build_history_text(
            tg_user_id=callback.from_user.id,
            client=client,
            chat_id=chat_id,
            offset=0,
            chat_page=chat_page,
        )
        rendered_message = await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=keyboard,
            photo=None,
        )
        set_active_chat_view(
            tg_user_id=callback.from_user.id,
            tg_chat_id=rendered_message.chat.id,
            tg_message_id=rendered_message.message_id,
            chat_id=chat_id,
            chat_page=chat_page,
            offset=0,
            signature=chat_view_signature(text, keyboard),
            paused=False,
        )
        mark_chat_read(callback.from_user.id, chat_id)
        await callback.answer()
    except Exception as exc:
        logger.exception(
            "Failed to open chat from update notify for user=%s chat=%s: %s",
            callback.from_user.id,
            chat_id,
            exc,
        )
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.message(UserFlow.waiting_for_token, F.text)
async def input_token(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await cleanup_auth_instruction_messages(state, message.chat.id)
    await session_manager.clear_auth_flow(message.from_user.id)
    token = normalize_token_input(message.text or "")

    if not token or len(token) < 20:
        await message.answer(
            "Не получилось распознать токен. Отправь токен строкой или JSON вида "
            '<code>{"token":"...","viewerId":94350134}</code>.',
            reply_markup=cancel_flow_keyboard(),
        )
        return

    wait_message = await message.answer("Проверяю токен и подключаюсь к MAX…")

    try:
        await session_manager.validate_and_save_token(message.from_user.id, token)
        CHAT_CACHE.pop(message.from_user.id, None)
        await state.clear()
        name = " ".join(
            part for part in [message.from_user.first_name, message.from_user.last_name] if part
        ).strip() or "друг"
        rendered_text = "✅ MAX токен сохранен.\n\n" + main_menu_text(True, name)
        await switch_screen_message(
            wait_message,
            text=rendered_text,
            reply_markup=main_menu_keyboard(True),
            photo=MAIN_MENU_IMAGE_PATH,
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
    clear_active_chat_view(callback.from_user.id)

    page = parse_int((callback.data or "").split(":", maxsplit=1)[1], default=0)
    current_message = callback.message

    if not session_manager.has_token(callback.from_user.id):
        await callback.answer("Сначала авторизуйся в MAX", show_alert=True)
        await switch_screen_message(
            current_message,
            text=auth_menu_text(False),
            reply_markup=auth_methods_keyboard(False),
            photo=None,
        )
        return

    try:
        current_message = await switch_screen_message(
            current_message,
            text="⏳ Загружаю чаты…",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
                ]
            ),
            photo=CHATS_MENU_IMAGE_PATH,
        )

        client = await session_manager.ensure_client(callback.from_user.id)
        entries = await get_chat_entries(
            callback.from_user.id,
            client,
            force_refresh=False,
        )

        if not entries:
            await switch_screen_message(
                current_message,
                text="💬 Чаты пока не найдены.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
                    ]
                ),
                photo=CHATS_MENU_IMAGE_PATH,
            )
            await callback.answer()
            return

        keyboard, current_page, total_pages = build_chats_keyboard(
            entries,
            page,
            callback.from_user.id,
        )
        unread_total = total_unread_for_user(callback.from_user.id)
        unread_line = f"\nНепрочитанные: <b>{unread_total}</b>" if unread_total > 0 else ""
        text = (
            "<b>Твои чаты в MAX</b>\n"
            f"Страница <b>{current_page + 1}/{total_pages}</b>\n"
            "Нажми на чат, чтобы открыть последние сообщения."
            f"{unread_line}"
        )
        await switch_screen_message(
            current_message,
            text=text,
            reply_markup=keyboard,
            photo=CHATS_MENU_IMAGE_PATH,
        )
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to load chats for user %s", callback.from_user.id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.callback_query(F.data.startswith("readall:"))
async def cb_read_all(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)

    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    page = max(0, parse_int(parts[1], default=0))
    if not session_manager.has_token(callback.from_user.id):
        await callback.answer("Сначала авторизуйся в MAX", show_alert=True)
        return

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        entries = await get_chat_entries(callback.from_user.id, client, force_refresh=True)

        for entry in entries:
            if entry.last_event_time > 0:
                UPDATE_LAST_SEEN[(callback.from_user.id, entry.chat_id)] = max(
                    UPDATE_LAST_SEEN.get((callback.from_user.id, entry.chat_id), 0),
                    int(entry.last_event_time),
                )

        cleared = clear_all_unread_for_user(callback.from_user.id)

        keyboard, current_page, total_pages = build_chats_keyboard(
            entries,
            page,
            callback.from_user.id,
        )
        unread_total = total_unread_for_user(callback.from_user.id)
        unread_line = f"\nНепрочитанные: <b>{unread_total}</b>" if unread_total > 0 else ""
        text = (
            "<b>Твои чаты в MAX</b>\n"
            f"Страница <b>{current_page + 1}/{total_pages}</b>\n"
            "Нажми на чат, чтобы открыть последние сообщения."
            f"{unread_line}"
        )
        await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=keyboard,
            photo=CHATS_MENU_IMAGE_PATH,
        )
        await callback.answer(f"Отмечено прочитанным: {cleared}")
    except Exception:
        logger.exception("Failed to mark all chats as read for %s", callback.from_user.id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


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
        rendered_message = await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=keyboard,
            photo=None,
        )
        set_active_chat_view(
            tg_user_id=callback.from_user.id,
            tg_chat_id=rendered_message.chat.id,
            tg_message_id=rendered_message.message_id,
            chat_id=chat_id,
            chat_page=chat_page,
            offset=offset,
            signature=chat_view_signature(text, keyboard),
            paused=False,
        )
        if offset == 0:
            mark_chat_read(callback.from_user.id, chat_id)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open chat %s for user %s", chat_id, callback.from_user.id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.callback_query(F.data.startswith("chatauto:"))
async def cb_chat_auto_refresh(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    action = parts[1]
    chat_id = parse_int(parts[2], default=0)
    chat_page = max(0, parse_int(parts[3], default=0))
    if chat_id <= 0 or action not in {"pause", "resume"}:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    paused = action == "pause"
    view = ACTIVE_CHAT_VIEWS.get(callback.from_user.id)
    if view and view.chat_id == chat_id and view.tg_message_id == callback.message.message_id:
        view.paused = paused
        view.chat_page = chat_page
        view.offset = 0
    else:
        set_active_chat_view(
            tg_user_id=callback.from_user.id,
            tg_chat_id=callback.message.chat.id,
            tg_message_id=callback.message.message_id,
            chat_id=chat_id,
            chat_page=chat_page,
            offset=0,
            signature="",
            paused=paused,
        )

    try:
        client = await session_manager.ensure_client(callback.from_user.id)
        text, keyboard = await build_history_text(
            tg_user_id=callback.from_user.id,
            client=client,
            chat_id=chat_id,
            offset=0,
            chat_page=chat_page,
        )
        await safe_edit_message(callback.message, text, keyboard)
        current_view = ACTIVE_CHAT_VIEWS.get(callback.from_user.id)
        if current_view and current_view.chat_id == chat_id:
            current_view.signature = chat_view_signature(text, keyboard)
            current_view.last_refresh_at = time.time()
            current_view.paused = paused
        if not paused:
            mark_chat_read(callback.from_user.id, chat_id)
        await callback.answer("Пауза включена" if paused else "Автообновление включено")
    except Exception:
        logger.exception("Failed to toggle chat auto refresh for user=%s chat=%s", callback.from_user.id, chat_id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.callback_query(F.data.startswith("profile:"))
async def cb_profile_from_chat(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)

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
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.callback_query(F.data.startswith("members:"))
async def cb_members(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)

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
            await switch_screen_message(
                callback.message,
                text=empty_text,
                reply_markup=empty_keyboard,
                photo=None,
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
        await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=keyboard,
            photo=None,
        )
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to load members for chat %s", chat_id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)
@dp.callback_query(F.data.startswith("member:"))
async def cb_member_profile(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)

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
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


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
        sent = await switch_screen_message(
            callback.message,
            text=text,
            reply_markup=keyboard,
            photo=None,
        )
        set_active_chat_view(
            tg_user_id=callback.from_user.id,
            tg_chat_id=sent.chat.id,
            tg_message_id=sent.message_id,
            chat_id=dm_chat_id,
            chat_page=chat_page,
            offset=0,
            signature=chat_view_signature(text, keyboard),
            paused=False,
        )
        mark_chat_read(callback.from_user.id, dm_chat_id)
        await callback.answer()
    except Exception as exc:
        logger.exception("Failed to open private chat with user %s", user_id)
        await callback.answer("Ошибка, попробуйте позже", show_alert=True)


@dp.callback_query(F.data.startswith("write:"))
async def cb_write(callback: types.CallbackQuery, state: FSMContext) -> None:
    if callback.from_user:
        remember_user(callback.from_user)
    clear_active_chat_view(callback.from_user.id)

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
    await state.update_data(
        chat_id=chat_id,
        chat_page=chat_page,
        write_prompt_message_id=callback.message.message_id,
        write_callback_query_id=callback.id,
    )

    await safe_edit_message(
        callback.message,
        "✍️ Отправь текст, фото, видео или файл одним сообщением.",
        cancel_flow_keyboard(),
    )
    await callback.answer()


@dp.message(UserFlow.waiting_for_chat_message)
async def send_message_to_chat(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    data = await state.get_data()

    chat_id = parse_int(str(data.get("chat_id", "0")), default=0)
    chat_page = max(0, parse_int(str(data.get("chat_page", "0")), default=0))
    prompt_message_id = parse_int(str(data.get("write_prompt_message_id", "0")), default=0)
    write_callback_query_id = str(data.get("write_callback_query_id", "") or "").strip()
    with contextlib.suppress(Exception):
        await message.delete()

    if chat_id == 0:
        await state.clear()
        await bot.send_message(
            chat_id=message.chat.id,
            text="Не удалось определить чат. Открой чат заново через меню.",
        )
        return

    if prompt_message_id > 0:
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                text="⏳ Отправка…",
            )

    text = (message.text or message.caption or "").strip()
    attachment: MaxPhoto | MaxVideo | MaxFile | None = None
    temp_path: str | None = None
    try:
        attachment, temp_path = await build_max_attachment_from_message(message)
    except Exception as exc:
        logger.warning("Failed to prepare outgoing media for %s: %s", message.from_user.id, exc)
        error_text = (
            f"❌ Не удалось подготовить медиа: <code>{esc(exc)}</code>\n"
            "Попробуй отправить файл еще раз."
        )
        if prompt_message_id > 0:
            with contextlib.suppress(Exception):
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text=error_text,
                    reply_markup=cancel_flow_keyboard(),
                )
                return
        await bot.send_message(chat_id=message.chat.id, text=error_text, reply_markup=cancel_flow_keyboard())
        return

    if not text and attachment is None:
        hint_text = "Отправь текст или медиафайл."
        if prompt_message_id > 0:
            with contextlib.suppress(Exception):
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text=hint_text,
                    reply_markup=cancel_flow_keyboard(),
                )
                return
        await bot.send_message(chat_id=message.chat.id, text=hint_text)
        return

    outgoing_text = text if text else " "
    attachment_kind = outgoing_attachment_kind(attachment)
    attachment_name = ""
    if message.document and getattr(message.document, "file_name", None):
        attachment_name = str(message.document.file_name)
    elif message.video and getattr(message.video, "file_name", None):
        attachment_name = str(message.video.file_name)
    elif message.animation and getattr(message.animation, "file_name", None):
        attachment_name = str(message.animation.file_name)
    elif message.audio and getattr(message.audio, "file_name", None):
        attachment_name = str(message.audio.file_name)
    elif temp_path:
        attachment_name = os.path.basename(temp_path)

    try:
        started_at = time.perf_counter()
        client = await session_manager.ensure_client(message.from_user.id)
        await client.send_message(
            text=outgoing_text,
            chat_id=chat_id,
            attachment=attachment,
        )
        record_send_latency((time.perf_counter() - started_at) * 1000)
        METRICS.direct_sent += 1
        await state.clear()

        HISTORY_ANCHORS.pop((message.from_user.id, chat_id), None)
        mark_chat_read(message.from_user.id, chat_id)
        history_text, history_keyboard = await build_history_text(
            tg_user_id=message.from_user.id,
            client=client,
            chat_id=chat_id,
            offset=0,
            chat_page=chat_page,
        )

        target_chat_id = message.chat.id
        target_message_id = prompt_message_id
        if prompt_message_id > 0:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text=history_text,
                    reply_markup=history_keyboard,
                )
            except TelegramBadRequest:
                sent = await bot.send_message(
                    chat_id=message.chat.id,
                    text=history_text,
                    reply_markup=history_keyboard,
                )
                target_chat_id = sent.chat.id
                target_message_id = sent.message_id
        else:
            sent = await bot.send_message(
                chat_id=message.chat.id,
                text=history_text,
                reply_markup=history_keyboard,
            )
            target_chat_id = sent.chat.id
            target_message_id = sent.message_id

        if target_message_id > 0:
            set_active_chat_view(
                tg_user_id=message.from_user.id,
                tg_chat_id=target_chat_id,
                tg_message_id=target_message_id,
                chat_id=chat_id,
                chat_page=chat_page,
                offset=0,
                signature=chat_view_signature(history_text, history_keyboard),
                paused=False,
            )

        if write_callback_query_id:
            with contextlib.suppress(Exception):
                await bot.answer_callback_query(write_callback_query_id, text="Отправлено")
    except Exception as exc:
        record_send_error("direct", message.from_user.id, chat_id, exc)
        is_temporary = is_temporary_send_error(exc)
        if is_temporary:
            logger.warning(
                "Temporary send failure for user=%s chat=%s; queueing message: %s",
                message.from_user.id,
                chat_id,
                exc,
            )

            queued_path: str | None = None
            if temp_path:
                try:
                    queued_path = persist_outgoing_attachment(
                        tg_user_id=message.from_user.id,
                        temp_path=temp_path,
                        source_name=attachment_name,
                    )
                    temp_path = None
                except Exception as move_exc:
                    logger.warning("Could not persist attachment for outgoing queue: %s", move_exc)

            queue_id = session_manager.enqueue_outgoing_message(
                tg_user_id=message.from_user.id,
                chat_id=chat_id,
                text=outgoing_text,
                attachment_type=attachment_kind if queued_path else None,
                attachment_path=queued_path,
                attachment_name=attachment_name or None,
            )
            METRICS.queued_messages += 1
            await state.clear()
            clear_active_chat_view(message.from_user.id)

            queue_text = (
                "📥 MAX временно недоступен.\n"
                f"Сообщение добавлено в очередь <code>#{queue_id}</code> и будет отправлено автоматически."
            )
            delivered_notice = False
            if prompt_message_id > 0:
                try:
                    await bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=prompt_message_id,
                        text=queue_text,
                        reply_markup=queued_message_keyboard(chat_id, chat_page),
                    )
                    delivered_notice = True
                except Exception:
                    delivered_notice = False
            if not delivered_notice:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=queue_text,
                    reply_markup=queued_message_keyboard(chat_id, chat_page),
                )

            if write_callback_query_id:
                with contextlib.suppress(Exception):
                    await bot.answer_callback_query(write_callback_query_id, text="В очереди")
            return

        logger.exception("Failed to send message for user %s", message.from_user.id)
        error_text = (
            f"❌ Не удалось отправить сообщение: <code>{esc(exc)}</code>\n"
            "Попробуй снова."
        )
        if prompt_message_id > 0:
            with contextlib.suppress(Exception):
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text=error_text,
                    reply_markup=cancel_flow_keyboard(),
                )
                return
        await bot.send_message(chat_id=message.chat.id, text=error_text, reply_markup=cancel_flow_keyboard())
    finally:
        if temp_path:
            with contextlib.suppress(Exception):
                os.remove(temp_path)


@dp.message(F.text.startswith("/media_"))
async def cmd_media_link(message: types.Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    clear_active_chat_view(message.from_user.id)
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
    name = " ".join(
        part for part in [message.from_user.first_name, message.from_user.last_name] if part
    ).strip() or "друг"
    await send_main_menu_message(message.chat.id, has_token, name)


async def main() -> None:
    global UPDATE_TASK, CHAT_REFRESH_TASK, QUEUE_TASK
    logger.info("Bot is starting")
    os.makedirs("sessions", exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    _load_ui_photo_cache()
    try:
        await ensure_bot_username()
    except Exception as exc:
        logger.warning("Could not resolve bot username for media links: %s", exc)

    UPDATE_TASK = asyncio.create_task(updates_loop(), name="max-updates-loop")
    CHAT_REFRESH_TASK = asyncio.create_task(chat_refresh_loop(), name="max-chat-refresh-loop")
    QUEUE_TASK = asyncio.create_task(outgoing_queue_loop(), name="max-outgoing-queue-loop")
    try:
        await dp.start_polling(bot)
    finally:
        for task_name in ("QUEUE_TASK", "CHAT_REFRESH_TASK", "UPDATE_TASK"):
            task = globals().get(task_name)
            if isinstance(task, asyncio.Task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                globals()[task_name] = None
        await session_manager.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
