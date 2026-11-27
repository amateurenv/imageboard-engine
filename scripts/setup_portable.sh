#!/bin/sh
# подготовка копии движка на другом mac или linux-компьютере

set -eu

if ! command -v python3.14 >/dev/null 2>&1; then
    echo "нужен python3.14 или новее"
    exit 1
fi

# virtual environment нельзя переносить между компьютерами: создаём его заново
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .

echo "среда готова"
echo "создай PostgreSQL и Redis, проверь .env, затем запусти:"
echo ".venv/bin/python -m scripts.migrate"
echo ".venv/bin/uvicorn app.main:app --reload"
