# Imageboard Engine

Настраиваемый движок имиджборды на Python и FastAPI

## Статус

Это ранний локальный прототип. Основа backend уже проверена: миграции,
транзакции модерации, CSRF, антиспам, cursor pagination, обычные и profile-доски.
Перед публичным запуском ещё нужно завершить media, realtime и настоящую
серверную инфраструктуру

Универсальный маршрут `/api/v1/media` и `/api/v1/ws` намеренно выключены. Для realtime
подготовлена базовая модель подписок, heartbeat и лимитов, но её ещё нужно
проверить сквозными сценариями в реальном окружении

## Что уже есть

- FastAPI-приложение и интерактивная документация `/docs`
- `/health` для проверки запуска и `/ready` для проверки PostgreSQL и Redis
- PostgreSQL и Redis, подключаемые через `.env`
- SQL-миграции с учётом уже применённых файлов и проверкой контрольных сумм
- настройки сайта в PostgreSQL с проверкой через Pydantic
- публичное чтение досок, тредов и постов с cursor pagination
- единая identity-модель: обычный пользователь и staff используют `users` и
  одну сессионную схему
- регистрация пользователя с созданием личной profile-доски в одной транзакции
- profile URL `/@handle` и canonical URL тредов `/slug/number` или
  `/@handle/number`, без технического id треда
- board-local `thread_number`, выдаваемый атомарно через PostgreSQL
- policy публикаций `owner_only`, `authenticated`, `anyone` в service-слое
- точные счётчики тредов и постов на доске, включая активность за последние 24 часа
- основа для staff-аккаунтов, сессий и ролей owner/admin/moderator
- административные маршруты с разделением прав доступа
- защита публикаций от простого спама через Redis
- unit-тесты и HTTP-интеграционные тесты, которые запускаются при заданных
  `TEST_DATABASE_URL` и `TEST_REDIS_URL`
- Ruff, MyPy и проверка запуска с PostgreSQL/Redis
- CSRF-защита изменяющих staff-маршрутов
- owner-модерация profile-досок: pin, lock, hide и delete с moderation log
- self-delete анонимных и account-публикаций без раскрытия автора
- posting claims для атомарной привязки готовых media к будущему треду или ответу
- image upload через claim: JPEG, PNG, static WebP, GIF и animated WebP в закрытый quarantine
- worker создаёт безопасный WebP display и статичный WebP preview через server-side re-encode
- выдача только ready display/preview через FastAPI-проверку и internal Nginx
- audio MP3/M4A/AAC и voice OGG/Opus через private quarantine и canonical transcoding
- защита от потерянных частичных обновлений досок и тредов
- ограничение bcrypt-пароля 72 UTF-8 байтами и контролируемый `409` для повторного IP-ban
- команда очистки истёкших staff-сессий
- GitHub Actions, запускающий Ruff и pytest при push и pull request

## Что пока не следует считать готовым

- управление profile-доской доступно через API, публичного интерфейса нет
- полноценной админ-панели пока нет: минимальная страница `/admin` управляет
  входом, досками и настройками через существующее API
- CDN и production Nginx пока не настроены - есть только tracked template
- видео, video notes, stickers, TGS/WebM, Telegram media и антивирус ещё не реализованы
- WebSocket-события, Telegram-бот и публичный frontend не готовы
- нет production-конфигурации reverse proxy, HTTPS, резервного копирования и мониторинга

## Стек

Python 3.14+, FastAPI, PostgreSQL, Redis, asyncpg, Pydantic, pytest и Ruff

## Структура

```text
app/
  api/            http-маршруты и зависимости
  core/           настройки, подключения и общие вещи
  repositories/   SQL-запросы к postgresql
  schemas/        формы запросов и ответов API
  services/       правила работы движка
migrations/       SQL-миграции
scripts/          служебные скрипты
tests/            автоматические проверки
```

## Локальный запуск

Нужны Python 3.14+, PostgreSQL и Redis

```bash
git clone https://github.com/amateurenv/imageboard-engine.git
cd imageboard-engine
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Создай локальную базу и роль. Пароль ниже - только пример, для своего окружения
используй другой

```sql
CREATE ROLE imageboard_app WITH LOGIN PASSWORD 'change-me';
CREATE DATABASE imageboard_dev OWNER imageboard_app;
```

Скопируй пример настроек, укажи реальный пароль в `.env` и примени миграции

```bash
cp .env.example .env
python -m scripts.migrate
```

Запусти приложение

```bash
uvicorn app.main:app --reload
```

После запуска доступны:

- http://127.0.0.1:8000/docs - документация API
- http://127.0.0.1:8000/health - приложение запущено
- http://127.0.0.1:8000/ready - PostgreSQL и Redis отвечают
- http://127.0.0.1:8000/api/v1/boards - список активных досок

## Чтение тредов и постраничная загрузка

Треды и ответы загружаются небольшими страницами. Это не даёт одному запросу
забрать тысячи записей и перегрузить сервер

### Треды доски

```text
GET /api/v1/boards/{slug}/threads?limit=30&cursor=...
```

- `limit` - число тредов в ответе, от 1 до 100
- без `cursor` сервер возвращает первую страницу
- `next_cursor` из ответа передаётся в следующий запрос
- `next_cursor: null` значит, что страниц больше нет
- закреплённые треды идут выше обычных, остальные - по последнему bump
- каждый элемент содержит `canonical_url`, например `/b/1`

### Посты треда

```text
GET /api/v1/threads/{thread_id}?limit=100&cursor=...
```

Первый ответ содержит OP и последние ответы треда. Старые ответы подгружаются
по `next_cursor`. OP не повторяется среди `replies`

Ответ треда содержит `canonical_url`. У OP и каждого ответа есть
`thread_canonical_url`, чтобы клиент не собирал ссылку из технического id

`cursor` создаёт сервер. Его не нужно читать, менять или собирать вручную.
Неверный `cursor` и недопустимый `limit` дают ответ `422`

## Счётчики досок

`GET /api/v1/boards` и `GET /api/v1/boards/{slug}` возвращают `thread_count`,
`post_count` и `active_post_count`. Это точные значения из PostgreSQL на момент
запроса. По умолчанию активность считается за 24 часа; владелец может изменить
окно от 1 до 168 часов через группу настроек `statistics`.

## Аккаунты, profile-доски и публикация

`POST /api/v1/auth/register` принимает `handle` и пароль, сохраняет bcrypt hash
и в той же транзакции создаёт profile-доску. Обычный пользователь затем входит
через существующий `POST /api/v1/auth/login`: отдельной public-auth системы нет.

Profile-доска читается через `GET /api/v1/profiles/{handle}`, меняется владельцем
через `PATCH /api/v1/profiles/{handle}` с cookie-сессией и CSRF-заголовком.
Handle хранится без `@`, неизменяем и образует URL `/@handle`.

Policy создания тредов и ответов независимы: `anyone` допускает анонимов,
`authenticated` требует сессию, `owner_only` оставляет доступ владельцу profile.
Staff сохраняет override. Ошибки имеют стабильные коды `authentication_required`
и `posting_forbidden`.

## Удаление собственных публикаций

Аноним при создании треда или ответа передаёт `deletion_password`. В базе остаётся
только отдельный bcrypt hash, а не сам пароль. Вошедший пользователь получает
внутреннее авторство публикации и удаляет только свой контент по session cookie и
CSRF - пароль ему не нужен.

```text
DELETE /api/v1/threads/{thread_id}
DELETE /api/v1/posts/{post_id}
```

Пароль передаётся только JSON body. OP нельзя удалить через маршрут поста - он
удаляется только вместе с тредом. Успешное self-delete действие и moderation log
сохраняются одной транзакцией. Перебор анонимного пароля ограничен Redis.

## Posting claims и image upload

Клиент сначала получает временное одноразовое право подготовить вложения для
одной доски:

```text
POST /api/v1/boards/{slug}/posting-claims
```

Ответ содержит opaque `token`, `expires_at` и лимит числа вложений. Token
возвращается только один раз и хранится в PostgreSQL только как hash. Claim живёт
30 минут по настройке `posting_claims.ttl_minutes`.

Первый upload-маршрут намеренно узкий:

```text
POST /api/v1/boards/{slug}/media
```

Это `multipart/form-data` с полями `posting_claim_token` и `file`. Он принимает
JPEG, PNG, static WebP, GIF, animated WebP, MP3, M4A/AAC и OGG/Opus voice. MIME-type, расширение и исходное имя файла не считаются
доказательством типа. Файл записывается кусками под серверным случайным именем
в закрытый quarantine, проверяется Pillow по содержимому и остаётся недоступным
по URL.

Worker повторно проверяет изображение, ограничивает разрешение и число пикселей.
Для GIF и animated WebP он создаёт animated WebP display с порядком кадров и
duration, а preview всегда делает статичным WebP из первого кадра. Для обычных
image display и preview также создаются через перекодирование в WebP без EXIF/ICC metadata.
Исходник из quarantine удаляется только после успешного создания производных
файлов. Оригинал и quarantine никогда не раздаются.

После атомарной привязки ready image к видимому посту движок создаёт случайный
opaque public key. Готовые derivative выдаются только так:

```text
GET /media/{public_key}/preview
GET /media/{public_key}/display
```

FastAPI снова проверяет key, `ready`, attach и видимость поста/треда, затем
возвращает `X-Accel-Redirect`. Байты читает только internal location Nginx из
`deploy/nginx.conf.example`; FastAPI не передаёт файл потоком. Hide или delete
сразу возвращает `404`, даже пока фоновая cleanup-очередь ещё удаляет файл.
Шаблон Nginx не настраивает production-сервер сам: оператор обязан указать
абсолютный `MEDIA_STORAGE_ROOT`, сохранить private permissions и не публиковать
storage через public `alias`.

Audio и voice требуют установленных оператором `ffprobe` и `ffmpeg`; это
обязательные зависимости production и CI. Сервер
проверяет container, codec, длительность, channels и sample rate, затем worker
декодирует upload целиком и создаёт только M4A/AAC для `audio` или OGG/Opus для
`voice`. Оригинал из quarantine не выдаётся и удаляется только после успешного
canonical transcoding. Готовый audio/voice использует `/media/{public_key}/content`;
Nginx поддерживает Range playback через внутренний location.

Для `audio` лимит - 30 минут, до двух каналов и 48 kHz; для `voice` - 10 минут,
ровно один канал и 48 kHz. Известный, но запрещённый container (например MP4,
WAV, FLAC, WebM или archive) возвращает `415 unsupported_media_type` без дорогого
decoder. Повреждённый допустимый audio-кандидат и случайные байты возвращают
`422 invalid_media`. `NoopMediaScanner` остаётся extension point, а не антивирусом.

Статичный image ограничен 12 000 px по каждой стороне и 20 миллионами пикселей.
Для animation действуют серверные anti-resource-abuse пределы: до 4 096 px по
стороне кадра, 8 миллионов пикселей на кадр, 300 кадров, 30 секунд и 100 миллионов
суммарно декодированных пикселей. Это защита decoder и worker, а не замена будущему
антивирусу. Общий байтовый лимит в 20 MiB выбран для совместимости с будущим
Telegram-ботом, но эти limits не являются лимитами Telegram.

В одной транзакции движок повторно проверяет policy, срок claim, account binding,
состояние `ready`, доску, лимиты и порядок вложений, затем создаёт пост, привязывает
media и помечает claim consumed. Один claim нельзя использовать дважды.

## Лимиты media

Правила media настраиваются в группе `media`. Значения по умолчанию: до 10 вложений в пост,
до 20 MiB на каждое разрешённое вложение и до 200 MiB на весь пост.

Каждая доска может переопределить эти лимиты в `/admin`. Пустое поле наследует
значение сайта.
Документы, архивы и исполняемые файлы не входят в модель движка; поддерживаются
только разрешённые типы медиа.

Сейчас upload принимает JPEG, PNG, static WebP, GIF, animated WebP, MP3, M4A/AAC
и OGG/Opus voice. В `media`
готовая animation помечается `is_animation: true`, поэтому клиент знает, что
display анимирован, а preview остаётся статичным. `audio` проходит только как
MP3/M4A/AAC, а `voice` - только OGG/Opus. Video, video note, stickers, documents,
archives и исполняемые файлы отклоняются. Telegram,
custom emoji и bot API не подключены.

Будущие форматы перечислены как roadmap, а не как готовая проверка:

| Тип | Сейчас | Будущее ограничение |
|---|---:|---|
| photo JPEG/PNG/static WebP | да | до 20 MiB |
| animation GIF / animated WebP | да | до 20 MiB и server-side animation limits |
| video MP4 | нет | до 20 MiB |
| video note | нет | MPEG-4, до 20 MiB и 60 секунд |
| music/audio MP3/M4A/AAC | да | до 20 MiB, до 30 минут, до 2 каналов, 48 kHz |
| voice OGG/Opus | да | до 20 MiB, до 10 минут, 1 канал, 48 kHz |
| static sticker | нет | PNG/WebP, до 512 KiB, одна сторона до 512 px |
| animated TGS sticker | нет | до 64 KiB, 512×512, до 3 секунд |
| WebM video sticker | нет | VP9 без аудио, до 256 KiB, до 3 секунд и 512×512 |
| Telegram custom emoji | нет | text entity с fallback, не вложение |

Общий будущий предел остаётся единым: до 10 вложений, до 20 MiB на обычное
вложение (`20_971_520` bytes) и до 200 MiB на пост (`209_715_200` bytes).
Он соответствует лимиту скачивания файлов обычным Telegram Bot API, но сам
Telegram-адаптер пока не реализован.

HTTP API уже возвращает одинаковые машинные коды ошибок для сайта и будущего
мобильного клиента. Telegram-бот позже сам преобразует эти коды в короткие
сообщения для чата.

| Ситуация | HTTP-код | Код для клиента |
|---|---:|---|
| Формат не поддерживается | 415 | `unsupported_media_type` |
| Файл больше лимита доски | 413 | `media_file_too_large` |
| Весь пост больше лимита | 413 | `media_post_too_large` |
| Слишком много вложений | 422 | `media_count_exceeded` |
| Файл повреждён | 422 | `invalid_media` |
| Вложения запрещены на доске | 403 | `media_upload_disabled` |
| Сработал антиспам или квота | 429 | `media_rate_limited` |

При такой ошибке временный файл удаляется, запись в базе и задание worker не
создаются. Сервер не показывает пользователю traceback или технические пути.

## Проверки

Перед коммитом запускай:

```bash
ruff check .
mypy app scripts
pytest
```

HTTP integration tests никогда не берут `DATABASE_URL` или `REDIS_URL` приложения.
Для них нужны отдельные пустые сервисы и только явные переменные окружения:

```bash
export TEST_DATABASE_URL='postgresql://imageboard_app:password@127.0.0.1:5432/imageboard_test'
export TEST_REDIS_URL='redis://127.0.0.1:6379/15'
pytest -m integration
```

Не указывай здесь development или production базу: suite создаёт и удаляет
отдельную PostgreSQL schema и очищает свои Redis rate-limit ключи

Для полного локального прогона скопируй `.env.test.example` в `.env.test`,
укажи отдельные тестовые сервисы и передай переменные в оболочку:

```bash
set -a
source .env.test
set +a
pytest
```

Зависимости закреплены в `requirements.lock` вместе с хешами. Для чистого
окружения установи их так:

```bash
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
```

После изменения `pyproject.toml` обнови lock-файл той же версией `pip-tools`:

```bash
pip-compile --strip-extras --extra dev --generate-hashes --output-file requirements.lock pyproject.toml
```

Для очистки истёкших staff-сессий по расписанию запускай:

```bash
python -m scripts.cleanup_sessions
python -m scripts.cleanup_posting_claims
python -m scripts.cleanup_media_files
```

После staff-login сервер устанавливает cookie `csrf_token`. Каждый изменяющий
запрос к `/api/v1/admin/...` и `POST /api/v1/auth/logout` должен передавать её
значение в заголовке `X-CSRF-Token`.

## Realtime

Маршрут `/api/v1/ws` пока намеренно не зарегистрирован. Когда его включат,
клиент сможет подписываться только на конкретную доску (`board:news`) или тред
(`thread:42`). Соединение ждёт heartbeat раз в 30 секунд, а процесс ограничен
100 соединениями и 32 подписками на клиента. После reconnect клиент всегда
заново читает данные через REST - realtime сообщает лишь о том, что они изменились.

Минимальная страница управления доступна по `/admin`. Owner и admin могут
создавать, менять и удалять доски; owner также меняет настройки сайта. Страница
использует те же cookie, csrf-токен и административные API, поэтому не хранит
пароль в браузере.

Черновые примеры Nginx, systemd и таймера очистки лежат в `deploy/`; полный
чек-лист перед публичным запуском - в `docs/production.md`.

## Миграции

Структура базы и начальные настройки меняются только новыми файлами в
`migrations/`. Уже применённую миграцию не редактируй: скрипт заметит изменение
контрольной суммы и остановится, чтобы не повредить данные

## Ближайший план

1. подключить реальный scanner, квоты, метрики, backup strategy и production Nginx
2. закрыть realtime через Redis с ограничением соединений и нормальной остановкой
3. подготовить production-развёртывание: HTTPS, reverse proxy, бэкапы, мониторинг и CDN

## Принципы

- клиенту не доверяем
- SQL-запросы передают значения только параметрами
- API отдаёт только явно разрешённые поля
- секреты не хранятся в Git
- опасные системные настройки остаются вне веб-админки
