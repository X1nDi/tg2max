import asyncio
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass

from pymax import MaxClient

logger = logging.getLogger(__name__)
_INVALID_TOKEN_RE = re.compile(r"(Invalid token:\s*)(\S+)", re.IGNORECASE)


class UserStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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
        return row[0]

    def set_token(self, tg_user_id: int, token: str) -> None:
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
                (tg_user_id, token, now, now),
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


@dataclass
class _ClientContext:
    client: MaxClient
    task: asyncio.Task | None = None


class MaxSessionManager:
    def __init__(self, db_path: str = "sessions/users.db") -> None:
        self.store = UserStore(db_path)
        self._contexts: dict[int, _ClientContext] = {}
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
    def _client_phone(tg_user_id: int) -> str:
        # pymax validates phone format even when token auth is used.
        # Build a deterministic placeholder in E.164 style.
        return f"+79{tg_user_id % 1_000_000_000:09d}"

    def _reset_session_cache(self, tg_user_id: int) -> None:
        session_dir = self._session_dir(tg_user_id)
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        os.makedirs(session_dir, exist_ok=True)

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
