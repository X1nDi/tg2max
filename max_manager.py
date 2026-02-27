import asyncio
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pymax import MaxClient
from pymax.static.enum import Opcode

logger = logging.getLogger(__name__)
_INVALID_TOKEN_RE = re.compile(r"(Invalid token:\s*)(\S+)", re.IGNORECASE)
_TOKEN_PREFIX = "enc:v1:"
_TOKEN_ENCRYPTION_KEY_ENV = "TOKEN_ENCRYPTION_KEY"


class UserStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._fernet = self._build_fernet()
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_schema()
        self._migrate_plaintext_tokens()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _build_fernet(self) -> Fernet:
        raw_key = os.getenv(_TOKEN_ENCRYPTION_KEY_ENV, "").strip().strip('"').strip("'")
        if not raw_key:
            raise ValueError(
                f"{_TOKEN_ENCRYPTION_KEY_ENV} не найден. "
                "Сгенерируй ключ Fernet и добавь его в .env."
            )
        try:
            return Fernet(raw_key.encode("utf-8"))
        except Exception as exc:
            raise ValueError(
                f"{_TOKEN_ENCRYPTION_KEY_ENV} имеет неверный формат. "
                "Ожидается ключ Fernet (urlsafe base64)."
            ) from exc

    @staticmethod
    def _is_encrypted_token(value: str) -> bool:
        return value.startswith(_TOKEN_PREFIX)

    def _encrypt_token(self, token: str) -> str:
        encrypted = self._fernet.encrypt(token.encode("utf-8")).decode("utf-8")
        return f"{_TOKEN_PREFIX}{encrypted}"

    def _decrypt_token(self, value: str) -> str:
        if not self._is_encrypted_token(value):
            return value
        payload = value[len(_TOKEN_PREFIX) :]
        try:
            return self._fernet.decrypt(payload.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Не удалось расшифровать MAX токен. Проверь TOKEN_ENCRYPTION_KEY."
            ) from exc

    def _migrate_plaintext_tokens(self) -> None:
        now = int(time.time())
        updates: list[tuple[str, int, int]] = []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT tg_user_id, max_token FROM users WHERE max_token IS NOT NULL AND max_token != ''"
            ).fetchall()
            for row in rows:
                tg_user_id = int(row["tg_user_id"])
                raw = str(row["max_token"] or "").strip()
                if not raw:
                    continue
                if self._is_encrypted_token(raw):
                    # Validate existing encrypted tokens on startup.
                    self._decrypt_token(raw)
                    continue
                updates.append((self._encrypt_token(raw), now, tg_user_id))

            if updates:
                conn.executemany(
                    "UPDATE users SET max_token = ?, updated_at = ? WHERE tg_user_id = ?",
                    updates,
                )
            conn.commit()

        if updates:
            logger.info("Migrated %s plaintext MAX token(s) to encrypted storage", len(updates))

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    tg_user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    max_token TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outgoing_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    attachment_type TEXT,
                    attachment_path TEXT,
                    attachment_name TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outgoing_queue_user_id ON outgoing_queue(tg_user_id, id)"
            )
            conn.commit()

    def register_user(
        self,
        tg_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (tg_user_id, username, first_name, last_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    updated_at=excluded.updated_at
                """,
                (tg_user_id, username, first_name, last_name, now, now),
            )
            conn.commit()

    def get_user_row(self, tg_user_id: int) -> dict[str, object] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE tg_user_id = ?",
                (tg_user_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_token(self, tg_user_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT max_token FROM users WHERE tg_user_id = ?",
                (tg_user_id,),
            ).fetchone()
        if not row:
            return None
        raw = str(row[0] or "").strip()
        if not raw:
            return None
        token = self._decrypt_token(raw).strip()
        if token and not self._is_encrypted_token(raw):
            self.set_token(tg_user_id, token)
        return token or None

    def set_token(self, tg_user_id: int, token: str) -> None:
        clean_token = (token or "").strip()
        encrypted_token = self._encrypt_token(clean_token)
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (tg_user_id, max_token, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    max_token=excluded.max_token,
                    updated_at=excluded.updated_at
                """,
                (tg_user_id, encrypted_token, now, now),
            )
            conn.commit()

    def clear_token(self, tg_user_id: int) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET max_token = NULL, updated_at = ? WHERE tg_user_id = ?",
                (now, tg_user_id),
            )
            conn.commit()

    def list_token_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tg_user_id FROM users WHERE max_token IS NOT NULL AND max_token != ''"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def enqueue_outgoing(
        self,
        tg_user_id: int,
        chat_id: int,
        text: str,
        attachment_type: str | None = None,
        attachment_path: str | None = None,
        attachment_name: str | None = None,
    ) -> int:
        now = int(time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO outgoing_queue (
                    tg_user_id,
                    chat_id,
                    text,
                    attachment_type,
                    attachment_path,
                    attachment_name,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_user_id,
                    chat_id,
                    text,
                    attachment_type,
                    attachment_path,
                    attachment_name,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def fetch_outgoing(self, tg_user_id: int, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    id,
                    tg_user_id,
                    chat_id,
                    text,
                    attachment_type,
                    attachment_path,
                    attachment_name,
                    attempt_count,
                    last_error,
                    created_at,
                    updated_at
                FROM outgoing_queue
                WHERE tg_user_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (tg_user_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_outgoing_sent(self, queue_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM outgoing_queue WHERE id = ?", (queue_id,))
            conn.commit()

    def mark_outgoing_attempt(self, queue_id: int, error_text: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outgoing_queue
                SET attempt_count = attempt_count + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error_text[:1000], now, queue_id),
            )
            conn.commit()

    def count_outgoing(self, tg_user_id: int | None = None) -> int:
        with self._connect() as conn:
            if tg_user_id is None:
                row = conn.execute("SELECT COUNT(1) FROM outgoing_queue").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(1) FROM outgoing_queue WHERE tg_user_id = ?",
                    (tg_user_id,),
                ).fetchone()
        return int(row[0] if row else 0)

    def list_outgoing_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tg_user_id FROM outgoing_queue ORDER BY tg_user_id ASC"
            ).fetchall()
        return [int(row[0]) for row in rows]


@dataclass
class _ClientContext:
    client: MaxClient
    task: asyncio.Task | None = None


@dataclass
class _AuthContext:
    kind: str
    client: MaxClient
    phone: str
    temp_token: str | None = None
    track_id: str | None = None
    qr_link: str | None = None
    qr_expires_at: int | None = None


class MaxSessionManager:
    def __init__(self, db_path: str = "sessions/users.db") -> None:
        self.store = UserStore(db_path)
        self._contexts: dict[int, _ClientContext] = {}
        self._auth_contexts: dict[int, _AuthContext] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, tg_user_id: int) -> asyncio.Lock:
        lock = self._locks.get(tg_user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tg_user_id] = lock
        return lock

    def register_telegram_user(
        self,
        tg_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        self.store.register_user(tg_user_id, username, first_name, last_name)

    def get_user(self, tg_user_id: int) -> dict[str, object] | None:
        return self.store.get_user_row(tg_user_id)

    def has_token(self, tg_user_id: int) -> bool:
        return bool(self.store.get_token(tg_user_id))

    def get_authorized_user_ids(self) -> list[int]:
        return self.store.list_token_user_ids()

    def active_client_count(self) -> int:
        return len(self._contexts)

    def active_auth_flow_count(self) -> int:
        return len(self._auth_contexts)

    def enqueue_outgoing_message(
        self,
        tg_user_id: int,
        chat_id: int,
        text: str,
        attachment_type: str | None = None,
        attachment_path: str | None = None,
        attachment_name: str | None = None,
    ) -> int:
        return self.store.enqueue_outgoing(
            tg_user_id=tg_user_id,
            chat_id=chat_id,
            text=text,
            attachment_type=attachment_type,
            attachment_path=attachment_path,
            attachment_name=attachment_name,
        )

    def fetch_outgoing_messages(self, tg_user_id: int, limit: int = 25) -> list[dict[str, Any]]:
        return self.store.fetch_outgoing(tg_user_id=tg_user_id, limit=limit)

    def mark_outgoing_message_sent(self, queue_id: int) -> None:
        self.store.mark_outgoing_sent(queue_id)

    def mark_outgoing_message_attempt(self, queue_id: int, error_text: str) -> None:
        self.store.mark_outgoing_attempt(queue_id, error_text)

    def count_pending_outgoing(self, tg_user_id: int | None = None) -> int:
        return self.store.count_outgoing(tg_user_id=tg_user_id)

    def get_outgoing_user_ids(self) -> list[int]:
        return self.store.list_outgoing_user_ids()

    async def validate_and_save_token(self, tg_user_id: int, token: str) -> None:
        old_token = self.store.get_token(tg_user_id)
        await self.disconnect_client(tg_user_id)
        self._reset_session_cache(tg_user_id)
        self.store.set_token(tg_user_id, token)

        try:
            client = await self.ensure_client(tg_user_id)
            await client.fetch_chats()
        except Exception as exc:
            logger.warning(
                "Token validation failed for %s: %s",
                tg_user_id,
                self._redact_sensitive(str(exc)),
            )
            await self.disconnect_client(tg_user_id)
            if old_token:
                self._reset_session_cache(tg_user_id)
                self.store.set_token(tg_user_id, old_token)
            else:
                self.store.clear_token(tg_user_id)
            raise ValueError(self._humanize_validation_error(exc)) from exc

    async def ensure_client(self, tg_user_id: int) -> MaxClient:
        token = self.store.get_token(tg_user_id)
        if not token:
            raise ValueError("MAX токен не найден. Сначала добавь токен.")

        lock = self._get_lock(tg_user_id)
        async with lock:
            ctx = self._contexts.get(tg_user_id)
            if ctx and ctx.client.is_connected and ctx.client.me is not None:
                return ctx.client

            if ctx is not None:
                await self._stop_context(ctx)

            session_dir = self._session_dir(tg_user_id)
            os.makedirs(session_dir, exist_ok=True)

            client = MaxClient(
                phone=self._client_phone(tg_user_id),
                token=token,
                work_dir=session_dir,
                reconnect=False,
            )

            try:
                await client.connect(client.user_agent)
                await client._sync(client.user_agent)
                await client._post_login_tasks(sync=False)
            except Exception:
                await self._hard_close_client(client)
                raise

            self._contexts[tg_user_id] = _ClientContext(client=client)
            return client

    async def disconnect_client(self, tg_user_id: int) -> None:
        lock = self._get_lock(tg_user_id)
        async with lock:
            ctx = self._contexts.pop(tg_user_id, None)
            if ctx:
                await self._stop_context(ctx)

    async def shutdown(self) -> None:
        items = list(self._contexts.items())
        self._contexts.clear()
        for _, ctx in items:
            await self._stop_context(ctx)

        auth_items = list(self._auth_contexts.items())
        self._auth_contexts.clear()
        for _, auth_ctx in auth_items:
            await self._hard_close_client(auth_ctx.client)

    async def begin_qr_login(self, tg_user_id: int) -> dict[str, str | int]:
        lock = self._get_lock(tg_user_id)
        async with lock:
            await self._clear_auth_context_locked(tg_user_id)
            self._reset_auth_session_cache(tg_user_id)
            auth_client = await self._create_auth_client(
                phone=self._client_phone(tg_user_id),
                work_dir=self._auth_session_dir(tg_user_id),
            )

            try:
                payload = await auth_client._request_qr_login()
                track_id = payload.get("trackId")
                qr_link = payload.get("qrLink")
                expires_at = payload.get("expiresAt")
                if not track_id or not qr_link or not expires_at:
                    raise ValueError("MAX вернул неполные данные QR-авторизации.")
            except Exception as exc:
                await self._hard_close_client(auth_client)
                raise ValueError(self._humanize_auth_flow_error(exc)) from exc

            self._auth_contexts[tg_user_id] = _AuthContext(
                kind="qr",
                client=auth_client,
                phone=self._client_phone(tg_user_id),
                track_id=str(track_id),
                qr_link=str(qr_link),
                qr_expires_at=int(expires_at),
            )
            return {
                "qr_link": str(qr_link),
                "expires_at": int(expires_at),
            }

    async def check_qr_login(self, tg_user_id: int) -> tuple[str, str | None]:
        lock = self._get_lock(tg_user_id)
        async with lock:
            auth_ctx = self._auth_contexts.get(tg_user_id)
            if auth_ctx is None or auth_ctx.kind != "qr" or not auth_ctx.track_id:
                raise ValueError("Сначала запроси QR-код для входа.")

            now = int(time.time() * 1000)
            if auth_ctx.qr_expires_at and now >= auth_ctx.qr_expires_at:
                await self._clear_auth_context_locked(tg_user_id)
                return "expired", None

            try:
                data = await auth_ctx.client._send_and_wait(
                    opcode=Opcode.GET_QR_STATUS,
                    payload={"trackId": auth_ctx.track_id},
                )
                payload = data.get("payload", {}) if isinstance(data, dict) else {}
                status = payload.get("status") if isinstance(payload, dict) else {}
                if isinstance(status, dict):
                    expires_at = status.get("expiresAt")
                    if isinstance(expires_at, (int, float)):
                        auth_ctx.qr_expires_at = int(expires_at)
                else:
                    status = {}

                if status.get("loginAvailable"):
                    login_payload = await auth_ctx.client._get_qr_login_data(auth_ctx.track_id)
                    token = self._extract_login_token(login_payload)
                    await self._clear_auth_context_locked(tg_user_id)
                    return "ready", token
            except Exception as exc:
                raise ValueError(self._humanize_auth_flow_error(exc)) from exc

            now = int(time.time() * 1000)
            if auth_ctx.qr_expires_at and now >= auth_ctx.qr_expires_at:
                await self._clear_auth_context_locked(tg_user_id)
                return "expired", None

            return "pending", None

    async def clear_auth_flow(self, tg_user_id: int) -> None:
        lock = self._get_lock(tg_user_id)
        async with lock:
            await self._clear_auth_context_locked(tg_user_id)

    async def logout(self, tg_user_id: int) -> None:
        lock = self._get_lock(tg_user_id)
        async with lock:
            ctx = self._contexts.pop(tg_user_id, None)
            if ctx:
                try:
                    await ctx.client.logout()
                except Exception as exc:
                    logger.warning("MAX logout request failed for %s: %s", tg_user_id, exc)
                await self._stop_context(ctx)

            await self._clear_auth_context_locked(tg_user_id)
            self.store.clear_token(tg_user_id)
            self._reset_session_cache(tg_user_id)

    async def _stop_context(self, ctx: _ClientContext) -> None:
        await self._hard_close_client(ctx.client)

        if ctx.task is not None:
            if not ctx.task.done():
                ctx.task.cancel()
            try:
                await asyncio.wait_for(ctx.task, timeout=5)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                logger.warning("MAX client task did not stop in time")
            except Exception:
                logger.exception("MAX client task failed while stopping")

    async def _hard_close_client(self, client: MaxClient) -> None:
        try:
            await client.close()
        except Exception:
            logger.exception("Failed to request MAX client close")

        try:
            await client._cleanup_client()
        except Exception:
            logger.exception("Failed to cleanup MAX client resources")

    @staticmethod
    def _session_dir(tg_user_id: int) -> str:
        return os.path.join("sessions", f"user_{tg_user_id}")

    @staticmethod
    def _auth_session_dir(tg_user_id: int) -> str:
        return os.path.join("sessions", f"user_{tg_user_id}_auth")

    @staticmethod
    def _client_phone(tg_user_id: int) -> str:
        # pymax validates phone format even when token auth is used.
        # Build a deterministic placeholder in E.164 style.
        return f"+79{tg_user_id % 1_000_000_000:09d}"

    def _reset_session_cache(self, tg_user_id: int) -> None:
        session_dir = self._session_dir(tg_user_id)
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        os.makedirs(session_dir, exist_ok=True)

    def _reset_auth_session_cache(self, tg_user_id: int) -> None:
        session_dir = self._auth_session_dir(tg_user_id)
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        os.makedirs(session_dir, exist_ok=True)

    async def _create_auth_client(self, phone: str, work_dir: str) -> MaxClient:
        client = MaxClient(
            phone=phone,
            work_dir=work_dir,
            reconnect=False,
        )
        try:
            await client.connect(client.user_agent)
            return client
        except Exception:
            await self._hard_close_client(client)
            raise

    async def _clear_auth_context_locked(self, tg_user_id: int) -> None:
        auth_ctx = self._auth_contexts.pop(tg_user_id, None)
        if auth_ctx:
            await self._hard_close_client(auth_ctx.client)
            self._reset_auth_session_cache(tg_user_id)

    @staticmethod
    def _extract_login_token(payload: dict[str, Any]) -> str:
        login_attrs = payload.get("tokenAttrs", {}).get("LOGIN", {})
        token = login_attrs.get("token")
        if token:
            return str(token)
        if payload.get("passwordChallenge"):
            raise ValueError(
                "Аккаунт требует пароль 2FA. Через бота этот шаг не поддержан, используй вход через готовый токен."
            )
        raise ValueError("MAX не вернул login token после подтверждения авторизации.")

    @staticmethod
    def _redact_sensitive(text: str) -> str:
        if not text:
            return text
        return _INVALID_TOKEN_RE.sub(r"\1[hidden]", text)

    def _humanize_validation_error(self, exc: Exception) -> str:
        text = self._redact_sensitive(str(exc))
        lowered = text.lower()
        if "invalid token" in lowered or "login.token" in lowered:
            return "MAX отклонил токен (login.token). Возьми свежий токен из WEB-сессии MAX и отправь снова."
        if "timeout" in lowered or "подключ" in lowered:
            return "Не удалось подключиться к MAX из-за сети/таймаута. Повтори попытку."
        return f"Ошибка авторизации в MAX: {text}"

    def _humanize_auth_flow_error(self, exc: Exception) -> str:
        text = self._redact_sensitive(str(exc))
        lowered = text.lower()

        if "invalid phone" in lowered or "номер" in lowered:
            return "Некорректный номер телефона. Используй международный формат, например +79991234567."
        if "verify" in lowered or "code" in lowered or "код" in lowered:
            return "Код подтверждения не подошел. Проверь код и попробуй снова."
        if "expired" in lowered:
            return "QR-код уже истек. Запроси новый QR и повтори попытку."
        if "timeout" in lowered or "send and wait failed" in lowered:
            return "Не удалось связаться с MAX (таймаут/сеть). Попробуй еще раз."
        return f"Ошибка авторизации в MAX: {text}"

