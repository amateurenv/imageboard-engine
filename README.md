# Imageboard Engine

Настраиваемый движок имиджборды на FastAPI. Проект находится в ранней разработке
и пока не предназначен для production.

## Локальный запуск

Требуется Python 3.14 или новее.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

После запуска проверка доступна по адресу <http://127.0.0.1:8000/health>.
Интерактивная документация API: <http://127.0.0.1:8000/docs>.

## Проверки

```bash
pytest
ruff check .
```
