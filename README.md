# Tg2max

Telegram-бот-мост между Telegram и MAX.

## Что умеет

1. Авторизация в MAX:
- по токену (основной и самый стабильный способ),
- по QR.
2. Просмотр чатов MAX и истории сообщений.
3. Отправка в MAX текста, фото, видео и файлов из Telegram.
4. Уведомления о новых сообщениях из MAX с кнопками:
- `Прочитать`,
- `Перейти в чат`.
5. Непрочитанные сообщения:
- счетчики в списке чатов,
- кнопка `Прочитать все`.
6. Автообновление открытого чата (с кнопкой паузы).
7. Очередь исходящих сообщений, если MAX временно недоступен.
8. Удаление своих сообщений в MAX через `🗑️` в истории.
9. Админ-инструменты:
- команды `/health`, `/stats`,
- inline-панель `/admin`.

## Безопасность

1. MAX-токены хранятся в SQLite в зашифрованном виде (Fernet).
2. Ключ шифрования задается через `TOKEN_ENCRYPTION_KEY`.
3. Токен привязан к Telegram ID пользователя.

## Требования

1. Docker + Docker Compose.
2. Telegram bot token (BotFather).
3. Fernet key для шифрования токенов.

## Быстрый старт

1. Создай `.env` в корне проекта:

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

2. Если нет ключа Fernet:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

3. Сборка и запуск:

```powershell
docker compose build max_bridge_bot
docker compose up -d max_bridge_bot
```

4. Логи:

```powershell
docker compose logs --tail=120 max_bridge_bot
```

## Использование

1. Открой бота и отправь `/start`.
2. Выполни вход в MAX.
3. Открой `Мои чаты`.
4. Выбери чат, читай историю, отправляй сообщения.

## Команды

1. `/start` - запуск и главное меню.
2. `/menu` - открыть главное меню.
3. `/login` - открыть меню авторизации MAX.
4. `/cancel` - отменить активный flow.
5. `/admin` - inline админ-панель (только для admin).
6. `/health` - health-статус (только для admin).
7. `/stats` - runtime-статистика (только для admin).

## Переменные окружения

Обязательные:
1. `BOT_TOKEN`
2. `TOKEN_ENCRYPTION_KEY`

Опциональные:
1. `BOT_USERNAME`
2. `ADMIN_IDS`
3. `UPDATE_POLL_SECONDS`
4. `CHAT_AUTORELOAD_SECONDS`
5. `QUEUE_RETRY_SECONDS`
6. `QUEUE_BATCH_SIZE`

## Документация по коду

1. [docs/DEVELOPER_GUIDE_RU.md](docs/DEVELOPER_GUIDE_RU.md) - полный разбор архитектуры и логики.
2. [docs/FUNCTION_REFERENCE_RU.md](docs/FUNCTION_REFERENCE_RU.md) - справочник по функциям и обработчикам.
3. [docs/README.md](docs/README.md) - точка входа в документацию.

## Структура проекта

1. `bot.py` - Telegram-слой, UI, обработчики, фоновые задачи.
2. `max_manager.py` - MAX-клиенты, auth-flow, шифрование, хранилище, очередь.
3. `sessions/` - runtime-данные (db, очереди, временные файлы, сессии).
4. `main.png`, `chats.png`, `instruction.png` - UI/инструкции.

## Troubleshooting

1. `TelegramConflictError: terminated by other getUpdates request`
- Причина: запущено больше одного инстанса с тем же `BOT_TOKEN`.
- Решение: оставить только один активный процесс/контейнер.

2. Изменения в коде не применились
- Проект запускается в Docker-образе.
- После правок выполняй:
```powershell
docker compose build max_bridge_bot
docker compose up -d --force-recreate max_bridge_bot
```

3. Долгая отправка экранов с фото
- После первого успешного показа бот кэширует Telegram `file_id` и ускоряет повторные отправки.
- Не удаляй `sessions/ui_photo_file_ids.json`, если хочешь сохранить прогретый кэш.
