# Tg2max

Бот-мост между Telegram и MAX.
Позволяет авторизоваться в MAX и работать с чатами MAX прямо из Telegram.

## Возможности

- Авторизация в MAX:
  - по токену (основной способ),
  - по QR-коду.
- Просмотр списка чатов MAX.
- Просмотр истории сообщений с навигацией.
- Отправка текста, фото, видео и файлов в MAX из Telegram.
- Автообновление открытого чата (с кнопкой паузы/продолжения).
- Счетчики непрочитанных + кнопка "Прочитать все".
- Очередь исходящих сообщений:
  - если MAX временно недоступен, сообщения не теряются,
  - бот отправит их автоматически после восстановления.
- Админ-команды `/health` и `/stats`.

## Безопасность

- MAX-токен хранится в зашифрованном виде (Fernet).
- Ключ шифрования задается через `TOKEN_ENCRYPTION_KEY`.
- Токен используется только для подключения вашего аккаунта MAX.

## Требования

- Docker + Docker Compose
- Telegram Bot Token (от BotFather)
- Ключ Fernet для шифрования токенов

## Быстрый старт

1. Создайте файл `.env` в корне проекта:

```env
BOT_TOKEN=your_telegram_bot_token
TOKEN_ENCRYPTION_KEY=your_fernet_key

# Опционально
BOT_USERNAME=tg2max_robot
ADMIN_IDS=123456789
UPDATE_POLL_SECONDS=10
CHAT_AUTORELOAD_SECONDS=4
QUEUE_RETRY_SECONDS=5
QUEUE_BATCH_SIZE=20
```

2. Сгенерируйте ключ Fernet (если нет):

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

3. Соберите и запустите контейнер:

```powershell
docker compose build max_bridge_bot
docker compose up -d max_bridge_bot
```

4. Посмотрите логи:

```powershell
docker compose logs --tail=120 max_bridge_bot
```

## Использование

- Откройте бота в Telegram и нажмите `/start`.
- Для нового пользователя доступен вход в MAX.
- После авторизации:
  - `Главное меню` (экран `main.png`),
  - `Мои чаты` (экран `chats.png`),
  - просмотр и отправка сообщений в MAX.

## Команды бота

- `/start` — запуск и главное меню
- `/menu` — открыть главное меню
- `/login` — меню авторизации MAX
- `/cancel` — отмена текущего действия
- `/health` — технический статус (только админ)
- `/stats` — статистика работы (только админ)

## Переменные окружения

Обязательные:

- `BOT_TOKEN` — токен Telegram-бота.
- `TOKEN_ENCRYPTION_KEY` — ключ Fernet для шифрования MAX-токенов.

Опциональные:

- `BOT_USERNAME` — username бота без `@`.
- `ADMIN_IDS` — Telegram user id админов через запятую.
- `UPDATE_POLL_SECONDS` — интервал фонового опроса обновлений MAX.
- `CHAT_AUTORELOAD_SECONDS` — интервал автообновления открытого чата (3-5).
- `QUEUE_RETRY_SECONDS` — интервал повторной попытки отправки из очереди.
- `QUEUE_BATCH_SIZE` — размер пачки сообщений очереди на один цикл.

## Структура проекта

- `bot.py` — Telegram-логика и UI.
- `max_manager.py` — сессии MAX, хранилище пользователей и очередь.
- `Dockerfile` / `docker-compose.yml` — контейнеризация.
- `instruction.png` — картинка инструкции по токену.
- `main.png` — фон/экран главного меню.
- `chats.png` — фон/экран меню чатов.
- `sessions/` — сессии и runtime-данные (БД, кэш, очередь).

## Troubleshooting

- `TelegramConflictError: terminated by other getUpdates request`
  - Запущено несколько экземпляров одного и того же бота.
  - Оставьте только один процесс/контейнер, который читает `getUpdates`.

- Долгая загрузка меню с фото:
  - После первого показа бот кэширует `file_id` изображений и ускоряется.
  - Проверьте, что `sessions/` примонтирована и сохраняется между перезапусками.

