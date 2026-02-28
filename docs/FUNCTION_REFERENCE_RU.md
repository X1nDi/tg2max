# Tg2max: Справочник Функций И Обработчиков

Этот документ дополняет `docs/DEVELOPER_GUIDE_RU.md` и дает быстрый «что делает что» по коду.

---

## 1. `max_manager.py`

## 1.1 Класс `UserStore`

1. `__init__(db_path)`
Инициализирует шифрование, схему БД и миграцию plaintext токенов.

2. `_connect()`
Создает SQLite connection.

3. `_build_fernet()`
Считывает `TOKEN_ENCRYPTION_KEY` и создает Fernet-объект.

4. `_is_encrypted_token(value)`
Проверка префикса `enc:v1:`.

5. `_encrypt_token(token)`
Шифрует токен и добавляет префикс версии формата.

6. `_decrypt_token(value)`
Дешифрует токен с префиксом; legacy plaintext возвращает как есть.

7. `_migrate_plaintext_tokens()`
При старте шифрует старые токены из БД.

8. `_init_schema()`
Создает таблицы `users`, `outgoing_queue`, индекс `idx_outgoing_queue_user_id`.

9. `register_user(...)`
Upsert данных Telegram-пользователя.

10. `get_user_row(tg_user_id)`
Читает полную запись пользователя.

11. `get_token(tg_user_id)`
Возвращает токен (с авто-миграцией legacy).

12. `set_token(tg_user_id, token)`
Шифрует и сохраняет токен.

13. `clear_token(tg_user_id)`
Очищает токен.

14. `list_token_user_ids()`
Список пользователей с токеном.

15. `enqueue_outgoing(...)`
Добавляет исходящее в очередь.

16. `fetch_outgoing(tg_user_id, limit)`
Читает очередь пользователя.

17. `mark_outgoing_sent(queue_id)`
Удаляет запись очереди как доставленную.

18. `mark_outgoing_attempt(queue_id, error_text)`
Увеличивает attempt_count и сохраняет текст ошибки.

19. `count_outgoing(tg_user_id=None)`
Считает размер очереди.

20. `list_outgoing_user_ids()`
Возвращает пользователей с непустой очередью.

## 1.2 Dataclass-контексты

1. `_ClientContext`
Контейнер активного `MaxClient` (+ task, если появится).

2. `_AuthContext`
Контейнер временных данных QR-авторизации.

## 1.3 Класс `MaxSessionManager`

1. `__init__(db_path)`
Создает store и runtime-контейнеры клиентов/lock-ов.

2. `_get_lock(tg_user_id)`
Выдает пер-пользовательский asyncio lock.

3. `register_telegram_user(...)`
Прокси в `UserStore.register_user`.

4. `get_user(tg_user_id)`
Прокси чтения пользователя.

5. `has_token(tg_user_id)`
Есть ли валидный токен в storage.

6. `get_authorized_user_ids()`
Список пользователей с токеном.

7. `active_client_count()`
Количество активных MAX-клиентов.

8. `active_auth_flow_count()`
Количество активных auth-контекстов.

9. `enqueue_outgoing_message(...)`
Прокси enqueue для очереди.

10. `fetch_outgoing_messages(...)`
Прокси чтения очереди.

11. `mark_outgoing_message_sent(queue_id)`
Прокси удаления из очереди.

12. `mark_outgoing_message_attempt(...)`
Прокси фиксации попытки с ошибкой.

13. `count_pending_outgoing(...)`
Прокси count очереди.

14. `get_outgoing_user_ids()`
Прокси списка пользователей очереди.

15. `validate_and_save_token(...)`
Валидация нового токена через MAX, с откатом на старый при ошибке.

16. `ensure_client(tg_user_id)`
Создает/переиспользует подключенный `MaxClient`.

17. `disconnect_client(tg_user_id)`
Безопасно закрывает и удаляет клиент из контекста.

18. `shutdown()`
Закрывает все активные клиенты и auth-клиенты.

19. `begin_qr_login(tg_user_id)`
Запуск QR flow и возврат `qr_link/expires_at`.

20. `check_qr_login(tg_user_id)`
Проверка статуса QR: `pending/expired/ready`.

21. `clear_auth_flow(tg_user_id)`
Очистка auth-контекста пользователя.

22. `logout(tg_user_id)`
Logout в MAX + удаление токена + cleanup сессии.

23. `_stop_context(ctx)`
Жестко закрывает клиент и завершает task.

24. `_hard_close_client(client)`
Вызывает `close` и `_cleanup_client` с подавлением падений.

25. `_session_dir(tg_user_id)`
Путь сессии клиента.

26. `_auth_session_dir(tg_user_id)`
Путь сессии auth-клиента.

27. `_client_phone(tg_user_id)`
Генерирует детерминированный phone-заполнитель E.164.

28. `_reset_session_cache(tg_user_id)`
Снос и пересоздание каталога сессии.

29. `_reset_auth_session_cache(tg_user_id)`
Снос и пересоздание auth-сессии.

30. `_create_auth_client(phone, work_dir)`
Создает auth-клиент без токена, подключает к MAX.

31. `_clear_auth_context_locked(tg_user_id)`
Удаляет auth-контекст и чистит auth-session-dir.

32. `_extract_login_token(payload)`
Извлекает login token из payload QR-авторизации.

33. `_redact_sensitive(text)`
Маскирует token в текстах ошибок.

34. `_humanize_validation_error(exc)`
Делает понятные для пользователя ошибки валидации токена.

35. `_humanize_auth_flow_error(exc)`
Делает понятные для пользователя ошибки auth-flow.

---

## 2. `bot.py` — служебные сущности

## 2.1 FSM и dataclass

1. `UserFlow`
Состояния: `waiting_for_token`, `waiting_for_chat_message`.

2. `ChatEntry`
Нормализованная запись чата для UI.

3. `MediaRequest`
Кэш-информация для deep-link выдачи медиа.

4. `ActiveChatView`
Состояние открытого чата в Telegram.

5. `SendErrorEvent`
Структура ошибки отправки для метрик.

6. `RuntimeMetrics`
Счетчики и latency для `/health`, `/stats`.

## 2.2 Базовые util-функции

1. `esc` — HTML escape.
2. `now_ms` — unix timestamp в ms.
3. `parse_int` — безопасный int parse.
4. `_parse_admin_ids` — парсер `ADMIN_IDS`.
5. `is_admin_user` — доступ к админ-командам.
6. `format_duration` — формат uptime.
7. `percentile` — percentile для latency.
8. `record_send_latency` — фиксация latency.
9. `record_send_error` — фиксация send ошибок.

## 2.3 Unread и active view

1. `unread_count_for_chat`
2. `total_unread_for_user`
3. `mark_chat_read`
4. `increment_unread_count`
5. `clear_all_unread_for_user`
6. `set_active_chat_view`
7. `clear_active_chat_view`
8. `chat_view_signature`

## 2.4 Ошибки/токены/ссылки

1. `is_temporary_send_error`
2. `normalize_token_input`
3. `make_link`

## 2.5 Медиа-кэш

1. `_media_request_key`
2. `cleanup_media_cache`
3. `register_media_request`
4. `media_command_markup`
5. `_filename_from_url`
6. `_download_url_to_path`
7. `download_media_to_temp`
8. `is_telegram_url_fetch_error`
9. `ensure_bot_username`

## 2.6 /start payload для удаления/медиа

1. `delete_message_start_payload`
2. `delete_message_command_markup`
3. `parse_delete_start_payload`

## 2.7 Вложения исходящих

1. `download_telegram_file_to_temp`
2. `build_max_attachment_from_message`
3. `outgoing_attachment_kind`
4. `persist_outgoing_attachment`
5. `build_outgoing_attachment_from_queue`
6. `queued_message_keyboard`

## 2.8 Форматирование и UI-тексты

1. `normalize_chat_type`
2. `chat_type_icon`
3. `user_display_name`
4. `time_label`
5. `short_title`
6. `main_menu_keyboard`
7. `auth_methods_keyboard`
8. `self_profile_keyboard`
9. `logout_confirm_keyboard`
10. `cancel_flow_keyboard`
11. `dismiss_message_keyboard`
12. `update_notification_keyboard`
13. `auth_menu_text`
14. `main_menu_text`
15. `token_help_text`
16. `send_token_instructions`
17. `cleanup_auth_instruction_messages`
18. `qr_help_text`
19. `qr_auth_keyboard`
20. `build_members_keyboard`
21. `build_chats_keyboard`
22. `resolve_chat_page`
23. `build_history_keyboard`

## 2.9 Переключение экранов и кэш UI-фото

1. `safe_edit_message`
2. `_message_has_photo_or_video`
3. `_photo_cache_key`
4. `_file_sha1`
5. `_save_ui_photo_cache`
6. `_load_ui_photo_cache`
7. `_get_cached_photo_file_id`
8. `_set_cached_photo_file_id`
9. `_extract_message_photo_file_id`
10. `_resolve_photo_source`
11. `_send_text_or_photo`
12. `_edit_photo_message`
13. `switch_screen_message`
14. `send_main_menu_message`
15. `edit_message_no_fallback`

## 2.10 Runtime cache helpers

1. `remember_user`
2. `clear_user_runtime_cache`

## 2.11 Чаты, профили, история

1. `get_chat_entries`
2. `resolve_chat_title`
3. `resolve_chat_entry`
4. `render_user_profile_text`
5. `render_self_profile_text`
6. `show_profile_card`
7. `render_attachment_lines`
8. `build_history_text`

## 2.12 Медиа/удаление/обновления

1. `send_media_by_token`
2. `delete_max_message_by_link`
3. `summarize_update_body`
4. `poll_updates_for_user`

## 2.13 Автообновление и очередь

1. `refresh_active_chat_for_user`
2. `refresh_active_chat_view_now`
3. `chat_refresh_loop`
4. `flush_outgoing_for_user`
5. `outgoing_queue_loop`
6. `updates_loop`

---

## 3. `bot.py` — Telegram handlers

## 3.1 Команды

1. `cmd_start` (`/start`)
2. `cmd_menu` (`/menu`)
3. `cmd_login` (`/login`)
4. `cmd_cancel` (`/cancel`)
5. `cmd_health` (`/health`)
6. `cmd_stats` (`/stats`)

## 3.2 Callback обработчики меню/авторизации

1. `cb_menu_main` (`menu:main`)
2. `cb_profile_me` (`profile:me`)
3. `cb_logout_confirm` (`logout:confirm`)
4. `cb_logout_cancel` (`logout:cancel`)
5. `cb_logout_yes` (`logout:yes`)
6. `cb_token_info` (`token:info`)
7. `cb_auth_menu` (`auth:menu`)
8. `cb_auth_token` (`auth:token`)
9. `cb_auth_qr` (`auth:qr`, `auth:qr:refresh`)
10. `cb_auth_qr_check` (`auth:qr:check`)
11. `cb_token_set` (`token:set`)
12. `cb_flow_cancel` (`flow:cancel`)
13. `cb_msg_close` (`msg:close`)

## 3.3 Callback обработчики уведомлений

1. `cb_notify_read` (`notify:read:<chat_id>`)
2. `cb_notify_open` (`notify:open:<chat_id>`)

## 3.4 Обработчики чатов

1. `input_token` (message в state `waiting_for_token`)
2. `cb_chats` (`chats:<page>`)
3. `cb_read_all` (`readall:<page>`)
4. `cb_chat` (`chat:<chat_id>:<offset>:<chat_page>`)
5. `cb_chat_auto_refresh` (`chatauto:pause/resume:...`)
6. `cb_profile_from_chat` (`profile:<chat_id>:<offset>:<chat_page>:<user_id>`)
7. `cb_members` (`members:<chat_id>:<offset>:<chat_page>:<page>`)
8. `cb_member_profile` (`member:<chat_id>:<offset>:<chat_page>:<user_id>:<members_page>`)
9. `cb_open_private_chat` (`openpm:<user_id>:<chat_page>`)
10. `cb_write` (`write:<chat_id>:<chat_page>`)
11. `send_message_to_chat` (message в state `waiting_for_chat_message`)

## 3.5 Прочие message handlers

1. `cmd_media_link` (`/media_<token>`)
2. `fallback_text` (обычный текст без slash-команды)

## 3.6 Entry point

1. `main` — старт фоновых задач, polling, graceful shutdown.

---

## 4. Формат Deep-Link Payload

1. `media_<token>`
Выдать пользователю конкретное вложение MAX.

2. `del_<chat_id>_<message_id>`
Удалить сообщение в MAX (для сообщений текущего пользователя, кнопка 🗑️ в истории).

---

## 5. Мини-Чеклист Перед Изменениями

1. Определите, какой слой меняете: UI (`bot.py`) или MAX/session (`max_manager.py`).
2. Проверьте, не влияет ли изменение на FSM состояния.
3. Проверьте callback_data формат и обратную совместимость.
4. Прогоните `python -m py_compile bot.py max_manager.py`.
5. Если Docker-run, пересоберите контейнер.

