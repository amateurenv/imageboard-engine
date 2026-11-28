# Imageboard Engine

Настраиваемый движок имиджборды на Python и FastAPI

## Статус

Это локальный backend-прототип. Ветка разработки `main3` включает проверенную
основу публикаций и модерации, медиаобработку до WebM-видеостикеров и миграции
`001`–`024`. Для публичного запуска ещё нужны эксплуатационные проверки и
инфраструктура из [плана развёртывания](docs/production.md).

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
- cookie-сессии и CSRF-защита административных изменений, модерации и настроек
  профиля, self-delete по аккаунту и выхода
- owner-модерация profile-досок: pin, lock, hide и delete с moderation log
- self-delete анонимных и account-публикаций без раскрытия автора
- posting claims для атомарной привязки готовых media к будущему треду или ответу
- публичная загрузка JPEG, PNG, WebP и GIF по claim в закрытый quarantine
- worker создаёт безопасный WebP display и статичный WebP preview через server-side re-encode
- выдача только ready derivatives через FastAPI-проверку и internal Nginx
- audio MP3/M4A/AAC и voice OGG/Opus через private quarantine и canonical transcoding
- видео MP4/H.264 с AAC или без звука, статичный WebP preview и HTTP Range через Nginx
- внутренняя обработка `video_note` и внутренние ответы со статическим WebP,
  TGS или WebM-стикером
- защита от потерянных частичных обновлений досок и тредов
- ограничение bcrypt-пароля 72 UTF-8 байтами и контролируемый `409` для повторного IP-ban
- команды очистки истёкших сессий, claims и файлов
- GitHub Actions с Ruff, MyPy и pytest при push и pull request

## Что пока не следует считать готовым

- управление profile-доской доступно через API, публичного интерфейса нет
- полноценной админ-панели пока нет: минимальная страница `/admin` управляет
  входом, досками и настройками через существующее API
- CDN и production Nginx пока не настроены - есть только tracked template
- для Telegram-публикации видеокружков и стикеров нужен адаптер с привязкой аккаунта
- сканер вложений, дисковые квоты, метрики worker, WebSocket и публичный frontend
  остаются в плане
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
  ws/             основа realtime
deploy/           примеры nginx и systemd
migrations/       SQL-миграции
scripts/          служебные скрипты
tests/            автоматические проверки
```

Основной поток запроса проходит от маршрута через сервис и репозиторий к
PostgreSQL. Сервис отвечает за policy и транзакцию. PostgreSQL хранит сессии,
контент и очередь медиа, Redis обслуживает rate limit и будит worker.

## Локальный запуск

Нужны Python 3.14, PostgreSQL и Redis. Проверенный Linux-стек использует
Ubuntu 24.04, PostgreSQL 16 и Redis 7. Для полного медиацикла установи FFmpeg
с FFprobe, `librlottie0-1`, `util-linux` и Nginx. Требования к кодекам и
выдаче перечислены в [инструкции по развёртыванию](docs/production.md).

```bash
git clone https://github.com/amateurenv/imageboard-engine.git
cd imageboard-engine
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
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
или staff через `PATCH /api/v1/profiles/{handle}` с cookie-сессией и
CSRF-заголовком. Handle хранится без `@`, неизменяем и образует canonical URL
`/@handle`. Canonical URL досок и тредов предназначены для будущего frontend.
Маршруты REST для чтения и изменения тредов принимают `thread_id`.
Маршруты `PATCH` и `DELETE` под `/api/v1/profiles/{handle}/threads/{thread_id}`
и `/api/v1/profiles/{handle}/posts/{post_id}` управляют состоянием публикаций.

Policy создания тредов и ответов независимы: `anyone` допускает анонимов,
`authenticated` требует сессию, `owner_only` оставляет доступ владельцу profile.
По умолчанию профиль использует `owner_only` для тредов и `authenticated`
для ответов. Владелец профиля и staff имеют право публикации по своей роли.
Отказы возвращают `401 authentication_required` и `403 posting_forbidden`.

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

## Posting claims и загрузка медиа

Клиент сначала получает временное одноразовое право подготовить вложения для
одной доски:

```text
POST /api/v1/boards/{slug}/posting-claims
```

Ответ содержит opaque `token`, `expires_at` и лимит числа вложений. Token
возвращается только один раз и хранится в PostgreSQL только как hash. Claim входит
в контекст одной доски и авторизованного аккаунта, если его создал вошедший
пользователь. Срок по умолчанию 30 минут, допустимый диапазон 1–120 минут.

Первый upload-маршрут намеренно узкий:

```text
POST /api/v1/boards/{slug}/media
```

Это `multipart/form-data` с полями `posting_claim_token` и `file`. Он принимает
JPEG, PNG, static WebP, GIF, animated WebP, MP3, M4A/AAC, OGG/Opus voice и MP4
с H.264 и необязательным AAC. Файл записывается кусками под случайным именем в
закрытый quarantine. Pillow и FFprobe определяют формат по содержимому.

Worker повторно проверяет изображение, ограничивает разрешение и число пикселей.
Для GIF и animated WebP он создаёт animated WebP display с порядком кадров и
duration, а preview всегда делает статичным WebP из первого кадра. Для обычных
image display и preview также создаются через перекодирование в WebP без EXIF/ICC metadata.
Исходник из quarantine удаляется только после успешного создания производных
файлов. Оригинал и quarantine никогда не раздаются.

После атомарной привязки ready media к видимому посту движок создаёт случайный
opaque public key. Готовые derivative выдаются только так:

```text
GET, HEAD /media/{public_key}/preview
GET, HEAD /media/{public_key}/display
GET, HEAD /media/{public_key}/content
```

FastAPI снова проверяет key, `ready`, attach и видимость доски, поста и треда,
затем возвращает `X-Accel-Redirect`. Байты читает internal location Nginx из
`deploy/nginx.conf.example`. После скрытия или удаления новый запрос к серверу
возвращает `404`, даже пока cleanup-очередь удаляет файл. Изображения, audio и
voice отдаются с `public, max-age=31536000, immutable`, поэтому уже закешированные
копии могут оставаться у клиента. Видео, стикеры и их превью используют `no-store`.
Шаблон Nginx не настраивает production-сервер сам: оператор обязан указать
абсолютный `MEDIA_STORAGE_ROOT`, сохранить private permissions и не публиковать
storage через public `alias`.

Audio, voice и video требуют установленных оператором `ffprobe` и `ffmpeg`.
Это обязательные зависимости production и CI. Для audio и voice сервер
проверяет container, codec, длительность, channels и sample rate, затем worker
декодирует upload целиком и создаёт только M4A/AAC для `audio` или OGG/Opus для
`voice`. Оригинал из quarantine не выдаётся и удаляется только после успешного
canonical transcoding. Готовый audio/voice использует `/media/{public_key}/content`;
Nginx поддерживает Range playback через внутренний location.

Для `audio` лимит - 30 минут, до двух каналов и 48 kHz; для `voice` - 10 минут,
ровно один канал и 48 kHz. Известный запрещённый контейнер или неподдерживаемые
дорожки MP4 возвращают `415 unsupported_media_type`. Повреждённые кандидаты,
случайные байты и превышение технических лимитов возвращают `422 invalid_media`.
Текущий `NoopMediaScanner` пропускает медиа без антивирусной проверки.

Worker создаёт MP4 с H.264/yuv420p, необязательной дорожкой AAC-LC на 48 кГц и
`faststart`, а также статичный WebP из первого кадра. Он проверяет готовый
MP4 и сохраняет его фактические параметры. Запись становится `ready` после
публикации обоих файлов. Клиент получает `content_url` и `preview_url`.
Nginx обслуживает Range для MP4. Видео и его превью выдаются с
`Cache-Control: no-store`, чтобы повторный запрос прошёл проверку видимости.
Для `video_note` worker использует квадратный кадр до 640×640 и длительность
до 60 секунд. Публичная форма принимает
квадратный MP4 как обычное видео, а публикация кружков зарезервирована для
будущего Telegram-бота. [Telegram Bot API](https://core.telegram.org/bots/api#sendvideonote)

Внутренний тип `sticker` принимает статический WebP до 512 КиБ, TGS до 64 КиБ
и WebM до 256 КиБ.
Для WebP длинная сторона
должна быть ровно 512 px. Worker перекодирует файл с сохранением размеров и
альфа-канала, убирает исходные метаданные и сохраняет один WebP display.
Непрозрачный WebP тоже допустим и сохраняет непрозрачность.
Будущий Telegram-адаптер сможет поставить стикер в очередь от связанного
аккаунта и после `ready` опубликовать его отдельным ответом в выбранном треде.
Ответ содержит ровно один стикер и пустой текст. Попытка открыть тред получает
`sticker_cannot_start_thread`. Обычный WebP из публичной формы остаётся
`image`. Публичные маршруты не загружают и не прикрепляют `sticker`.

TGS должен содержать один gzip-поток с разрешённым подмножеством Lottie.
Лимиты движка составляют 512×512 px, до 3 секунд, до 60 кадров/с и до 1 МиБ
распакованного JSON. Worker повторно проверяет вход, записывает канонический
TGS и рендерит первый кадр в прозрачный WebP в отдельном процессе с лимитами.
На Ubuntu 24.04 воркеру и CI нужны пакеты `librlottie0-1` и `util-linux`
с `/usr/bin/prlimit`.
При обновлении примени миграцию `023_animated_stickers`.
[Требования Telegram к анимированным стикерам](https://core.telegram.org/stickers#animated-sticker-requirements)

WebM-стикер содержит одну дорожку VP9 без звука. Длинная сторона ровно 512 px.
Допустимы до 3 секунд, до 30 кадров/с и до 90 кадров. Worker перекодирует WebM
через FFmpeg с `libvpx-vp9`, сохраняет альфа-канал при его наличии и записывает
первый кадр результата в статичный WebP. При обновлении примени миграцию
`024_video_stickers` и добавь WebM location из шаблона Nginx. Публичный upload
отклоняет WebM с `415 unsupported_media_type`.

После публикации все три формата получают `media_kind: "sticker"`. У WebP есть
`display_url`. TGS и WebM получают `is_animation: true`, `preview_url`,
`content_url` с MIME `application/x-tgsticker` или `video/webm` и
`duration_seconds`, округлённую вверх. Будущий клиент сможет использовать эти
поля для циклического воспроизведения. Все эти файлы выдаются с
`Cache-Control: no-store` после проверки видимости поста и треда.

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

В `media` готовая анимация помечается `is_animation: true`.
`preview_url` всегда ведёт на статичный WebP.

| Тип | Публичная загрузка | Лимиты |
|---|---:|---|
| photo JPEG/PNG/static WebP | да | до 20 MiB |
| animation GIF / animated WebP | да | до 20 MiB и server-side animation limits |
| video MP4 | да | до 20 MiB, 120 секунд, 1920×1080 или 1080×1920, 30 кадров/с, 3 600 видеопакетов |
| video note | будущий Telegram-бот | до 20 MiB, 60 секунд, квадрат до 640×640, 30 кадров/с, 1 800 видеопакетов |
| music/audio MP3/M4A/AAC | да | до 20 MiB, до 30 минут, до 2 каналов, 48 kHz |
| voice OGG/Opus | да | до 20 MiB, до 10 минут, 1 канал, 48 kHz |
| static sticker | внутренний сервис для будущего Telegram-бота | статический WebP, до 512 КиБ, длинная сторона ровно 512 px |
| animated TGS sticker | внутренний сервис для будущего Telegram-бота | TGS до 64 КиБ, 512×512, до 3 секунд и 60 кадров/с, до 1 МиБ JSON |
| WebM video sticker | внутренний сервис для будущего Telegram-бота | VP9 без звука, до 256 КиБ, 3 секунд, 30 кадров/с и 90 кадров, длинная сторона ровно 512 px |
| Telegram custom emoji | запланировано | текстовая сущность с fallback |

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
| Попытка начать тред стикером | 422 | `sticker_cannot_start_thread` |
| Вложения запрещены на доске | 403 | `media_upload_disabled` |
| Сработал rate limit загрузки | 429 | `media_rate_limited` |

При отказе до фиксации загрузки приложение пытается удалить временный файл.
Если ошибка найдена на этапе обработки, worker повторяет попытку в пределах
лимита, затем переводит вложение в `failed`. Ошибки HTTP-загрузки возвращают
код и сообщение без технических путей. Отдельного публичного маршрута опроса
состояния worker пока нет.

## Проверки

Перед коммитом запускай:

```bash
ruff check .
mypy app scripts
pytest
git diff --check
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

Для очистки истёкших сессий, claims и файлов по расписанию запускай:

```bash
python -m scripts.cleanup_sessions
python -m scripts.cleanup_posting_claims
python -m scripts.cleanup_media_files
```

После входа сервер устанавливает читаемую cookie `csrf_token`. Передавай её
в `X-CSRF-Token` при изменениях через административные и profile-маршруты,
self-delete по аккаунту и выходе.

На финальном ревью WebM в WSL Ubuntu 24.04 с Python 3.14.10 и FFmpeg 6.1.1
прошли Ruff, MyPy по 85 файлам и `git diff --check`. Полный pytest дал 222
прошедших теста, отдельный integration-прогон — 24. Повтор через Nginx 1.24
подтвердил GET/HEAD, Range `206` и `416`, заголовки и `404` после модерации.

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

Текущая схема включает `024_video_stickers`. Применяй миграции через
`python -m scripts.migrate`. Структура базы и начальные настройки меняются
новыми файлами в `migrations/`. Мигратор проверяет контрольные суммы ранее
применённых файлов и останавливается, если они изменились.

## Ближайший план

1. Завершить эксплуатацию медиа с реальным сканером, квотами, метриками, проверкой восстановления worker и Nginx.
2. Проверить realtime под нагрузкой, с аутентификацией, проверкой подписок и восстановлением Redis.
3. Расширить `/admin` на стабильных API и добавить безопасную визуальную настройку.
4. Подготовить production-развёртывание с HTTPS, reverse proxy, восстановлением из бэкапа и мониторингом.

Публичный frontend, Telegram и Mini App, стикерпаки и CDN остаются отдельными этапами.

## Принципы

- клиенту не доверяем
- SQL-запросы передают значения только параметрами
- API отдаёт только явно разрешённые поля
- секреты не хранятся в Git
- опасные системные настройки остаются вне веб-админки
