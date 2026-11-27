#!/bin/sh
# восстановление дампа только в новую пустую базу

set -eu

if [ "${RESTORE_DATABASE:-}" != "1" ]; then
    echo "создай новую пустую базу и запусти RESTORE_DATABASE=1 $0"
    exit 1
fi

if [ ! -f .env ]; then
    echo "не найден .env"
    exit 1
fi

database_url=$(sed -n 's/^DATABASE_URL=//p' .env | tail -n 1)
if [ -z "$database_url" ]; then
    echo "в .env нет DATABASE_URL"
    exit 1
fi

# --clean намеренно удаляет объекты только в новой базе, поэтому выше есть защита
pg_restore --clean --if-exists --no-owner --dbname="$database_url" backups/postgres.dump
echo "дамп восстановлен"
