# Imageboard Engine

Движок имиджборды на Python и FastAPI. Проект пока в ранней разработке

## Что уже работает

- документация API на `/docs`
- `/health` показывает, что приложение запущено, а `/ready` проверяет PostgreSQL и Redis
- SQL-миграции с проверкой контрольных сумм
- настройки сайта хранятся в БД и проверяются через Pydantic
- `GET /api/v1/boards` отдаёт список активных досок
- тесты и Ruff

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