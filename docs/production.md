# запуск в production

Проект пока не готов к публичному запуску. Этот документ - чек-лист для
следующего этапа, а не обещание готовности.

## что уже можно подготовить

1. Создай отдельного пользователя PostgreSQL с минимальными правами и отдельную
   Redis database
2. Укажи `APP_ENV=production` и `APP_DEBUG=false`
3. Примени миграции командой `python -m scripts.migrate`
4. Настрой HTTPS на reverse proxy и не открывай uvicorn в интернет напрямую
5. Включи резервные копии PostgreSQL и регулярно проверь восстановление на
   отдельной машине
6. Запускай `python -m scripts.cleanup_sessions` по таймеру
7. Запускай `python -m scripts.cleanup_posting_claims` и
   `python -m scripts.cleanup_media_files` по таймеру
8. Если нужен image upload, запусти отдельный process `python -m scripts.media_worker`
   с тем же закрытым `MEDIA_STORAGE_ROOT`, но не публикуй этот каталог через web server

## наблюдаемость

Логи приложения должны собираться systemd или другим менеджером процессов. До
публичного запуска добавь централизованный сбор ошибок, метрики запросов,
занятости PostgreSQL/Redis и оповещения о недоступности `/ready`.

## media - ограниченный image upload

`/api/v1/media` остаётся выключенным, но `POST /api/v1/boards/{slug}/media`
принимает JPEG, PNG, static WebP, GIF, animated WebP, MP3, M4A/AAC и OGG/Opus voice только с действующим posting claim. Файл попадает в
закрытый quarantine, проверяется по содержимому и после worker получает display
и preview WebP без metadata.

Готовое attached media получает opaque key. `GET /media/{key}/preview` и
`GET /media/{key}/display` повторно проверяют `ready`, attach и видимость в
PostgreSQL, после чего передают Nginx только `X-Accel-Redirect` на internal
location. FastAPI не читает файл, quarantine и оригиналы не имеют HTTP URL.
`deploy/nginx.conf.example` - шаблон, а не применённая конфигурация: оператор
должен заменить путь alias на абсолютный `MEDIA_STORAGE_ROOT`, закрыть права
storage и проверить Nginx отдельно.

GIF и animated WebP проходят отдельные пределы worker: до 4 096 px по стороне
кадра, 8 миллионов пикселей на кадр, 300 кадров, 30 секунд и 100 миллионов
суммарно декодированных пикселей. Display перекодируется в animated WebP, preview
остаётся статичным WebP. Это серверная защита ресурсов, а не пределы Telegram.
Обычный байтовый предел - 20 MiB на файл, до 10 вложений и 200 MiB на пост.

Audio MP3/M4A/AAC и voice OGG/Opus требуют system dependencies `ffprobe` и
`ffmpeg`, заданные через `FFPROBE_BIN` и `FFMPEG_BIN`; это обязательные
зависимости production и CI. FFprobe проверяет codec,
container, duration, channels и sample rate, а worker делает canonical derivative:
M4A/AAC для audio и OGG/Opus для voice. Лимиты Imageboard: audio до 30 минут,
voice до 10 минут, audio до двух каналов, voice строго один канал, sample rate до
48 kHz. Это не утверждения о лимитах Telegram. Production Nginx должен оставить
audio/voice locations `internal` и разрешать Range только для derivative файлов.
Известные запрещённые containers возвращают `415 unsupported_media_type`, а
повреждённые допустимые кандидаты и случайные байты - `422 invalid_media`.

До широкого public media запуска обязательно нужны:

- антивирус или отдельный безопасный scanner - он ещё не реализован;
- квоты диска, метрики, alerting и отдельная безопасная раздача через Nginx;
- операционные проверки worker, восстановления после файловой ошибки и нагрузки;
- отдельные решения для видео, video note, stickers, TGS/WebM, Telegram media и других форматов;
- публичный frontend и Telegram-бот.

`NoopMediaScanner` - только extension point для development. Он не является
антивирусом. Production scanner должен проверять quarantine-файл до worker
перехода в `ready`.

## realtime - не включать

Маршрут `/api/v1/ws` тоже остаётся выключенным. Основа умеет ограничивать
подписки и соединения, но перед включением понадобятся:

- аутентификация WebSocket и проверка `Origin`;
- проверка доступа к доске или треду перед подпиской;
- двусторонний heartbeat с отключением молчащего клиента;
- лимиты размера и частоты входящих команд, timeout медленной отправки;
- переподключение к Redis, метрики и HTTP/WebSocket-нагрузочные тесты.

## что проверять после настройки сервера

1. `/health` отвечает без базы, а `/ready` отвечает только когда доступны
   PostgreSQL и Redis
2. Nginx передаёт настоящий IP только с локального proxy, а `TRUSTED_PROXY_IPS`
   содержит только этот адрес
3. cookie сессии в production имеет `Secure`, а `APP_DEBUG=false`
4. timer очистки сессий и резервные копии действительно запускаются по расписанию
5. восстановление последней резервной копии проходит в отдельной тестовой базе
6. `GET /media/{opaque key}/display` работает только через proxy, а прямой URL
   `/_imageboard_media/...` снаружи даёт `404`
