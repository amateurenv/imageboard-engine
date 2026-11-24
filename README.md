# Imageboard Engine

Движок имиджборды на Python и FastAPI. Проект пока в ранней разработке

## Что уже работает

- документация API на `/docs`
- `/health` показывает, что приложение запущено, а `/ready` проверяет PostgreSQL и Redis
- SQL-миграции с проверкой контрольных сумм
- настройки сайта хранятся в БД и проверяются через Pydantic
- доски: `GET /api/v1/boards`, `GET /api/v1/boards/{slug}`, `POST /api/v1/boards`, `PATCH /api/v1/boards/{slug}`
- треды и посты: `GET /api/v1/boards/{slug}/threads` (закреплённые сверху), `POST /api/v1/boards/{slug}/threads`, `GET /api/v1/threads/{thread_id}`, `POST /api/v1/threads/{thread_id}/posts`
  - длины постов и заголовков валидируются по настройкам сайта; ссылки `>>N` сохраняются в `post_references`
  - флаги: закрепить (`is_sticky` при создании треда), закрыть (`is_locked` блокирует постинг, ответ 422)
- авторизация: `POST /api/v1/auth/login` (bcrypt, rate limit через redis), `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- админка: `GET/POST /api/v1/admin/boards`, `PATCH/DELETE /api/v1/admin/boards/{slug}`, `PATCH/DELETE /api/v1/admin/threads/{id}`, `DELETE /api/v1/admin/posts/{id}`, `GET/PUT /api/v1/admin/settings`, `GET/POST/DELETE /api/v1/admin/ip_bans` (IP-баны проверяются при создании треда/поста)
  - журнал: `moderation_log`; роли: owner/admin/moderator
- миграция: `003_staff.sql` (users, user_sessions, moderation_log, ip_bans)
- первый owner-админ: `scripts/seed_admin.py`
- тесты и Ruff

## Админка (Этап 4, дизайн)

- Роли: владелец, администратор, модератор (владелец выше админа выше модератора)
- Доски: `GET /api/v1/admin/boards`, `POST /api/v1/admin/boards`, `PATCH/DELETE /api/v1/admin/boards/{slug}` (title, description, position, is_active, is_nsfw)
- Треды: `PATCH /api/v1/admin/threads/{id}` (`is_sticky`, `is_locked`, `is_archived`, `subject`), `DELETE /api/v1/admin/threads/{id}`
- Посты: `DELETE /api/v1/admin/posts/{id}`
- Настройки: `GET/PUT /api/v1/admin/settings` (группы: thread, post, localization)
- Сессии: `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- IP-баны: `GET/POST /api/v1/admin/ip_bans`, `DELETE /api/v1/admin/ip_bans/{ip}` — проверяются при создании треда/поста
- Журнал: таблица `moderation_log` (actor, action, target_id, reason)
- Структура: миграция `003_staff.sql` (users, user_sessions, moderation_log)

## Стек

Python 3.14+, FastAPI, PostgreSQL, Redis, asyncpg, Pydantic, pytest, Ruff

## Структура

```text
app/
  api/            маршруты и зависимости
  core/           настройки, подключение к БД и Redis
  repositories/   SQL-запросы
  schemas/        схемы запросов и ответов
  services/       логика движка
migrations/       SQL-миграции
scripts/          служебные скрипты
tests/            тесты
```

## Запуск

Нужны Python 3.14+, PostgreSQL и Redis

Клонируй репозиторий и установи зависимости:

Создай базу. Пароль здесь для примера, подставь свой:

```sql
CREATE ROLE imageboard_app WITH LOGIN PASSWORD 'change-me';
CREATE DATABASE imageboard_dev OWNER imageboard_app;
```

Скопируй настройки, впиши в `.env` пароль от БД и примени миграции:

```bash
cp .env.example .env
python -m scripts.migrate
```

Запусти приложение:

```bash
uvicorn app.main:app --reload
```

После запуска откроются:

- http://127.0.0.1:8000/docs: документация API
- http://127.0.0.1:8000/health: проверка, что приложение работает
- http://127.0.0.1:8000/ready: проверка PostgreSQL и Redis
- http://127.0.0.1:8000/api/v1/boards: список досок, пока доски не созданы, вернётся `{"items":[]}`


## Планы

1. Получение доски по `slug`
2. Чтение и создание тредов и постов
3. Бампы, лимиты, точные счётчики
4. Защита от спама и staff-аккаунты
5. Админ-панель
6. Загрузка медиа и превью
7. WebSocket-обновления и Telegram-бот
8. Темы и настройка внешнего вида

## Принципы

- Клиенту не доверяю
- SQL-запросы только с параметрами
- API отдаёт только явно разрешённые поля
- Секреты и пароли не хранятся в Git
- Структура БД меняется только через миграции