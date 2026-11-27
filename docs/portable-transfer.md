# перенос на другой компьютер

Архив содержит исходный код, текущий `.env` и PostgreSQL-дамп. Он содержит
секреты и данные проекта, поэтому не загружай его в публичные облака и не
отправляй незнакомым людям.

Redis не переносится как база данных. В движке он хранит временные rate-limit
ключи, очереди и сессии worker. Для чистого развёртывания безопаснее запустить
пустой Redis, чем вернуть старые временные ключи или незавершённые задания.

## на новом компьютере

1. Установи Python 3.14, PostgreSQL 17+ и Redis 7+
2. Распакуй архив и перейди в его папку
3. Проверь `.env`: адреса и пароль относятся к новому компьютеру
4. Создай пустую PostgreSQL-базу с пользователем из `.env`
5. Выполни `sh scripts/setup_portable.sh`
6. Если нужны текущие данные, выполни

   ```bash
   RESTORE_DATABASE=1 sh scripts/restore_portable_database.sh
   ```

   Скрипт очищает указанную базу. Используй только новую пустую базу

7. Примени новые миграции и запусти приложение. В том числе будут добавлены
   profile-доски, self-delete, board-local номера тредов и posting claims

   ```bash
   .venv/bin/python -m scripts.migrate
   .venv/bin/uvicorn app.main:app --reload
   ```

8. Открой `http://127.0.0.1:8000/ready` и убедись, что PostgreSQL и Redis отвечают

9. Для долгоживущей установки добавь в расписание очистку истёкших сессий и
   неиспользованных posting claims:

   ```bash
   .venv/bin/python -m scripts.cleanup_sessions
   .venv/bin/python -m scripts.cleanup_posting_claims
   .venv/bin/python -m scripts.cleanup_media_files
   ```

   Image upload принимает только JPEG, PNG и WebP. Файлы хранятся в закрытом
   quarantine до worker. Ready preview/display выдаются только через FastAPI
   check и internal Nginx template - оригиналы и quarantine никогда не раздаются.
   На новом компьютере отдельно укажи абсолютный `MEDIA_STORAGE_ROOT` в Nginx
   template и не публикуй его через public alias. Telegram media и антивирус
   ещё не настроены

10. Если используется upload, отдельно запусти worker с тем же `.env`:

   ```bash
   .venv/bin/python -m scripts.media_worker
   ```
