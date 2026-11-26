"""проверка загрузки настроек из postgresql"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from app.core.site_settings import get_default_site_settings
from app.core.site_settings_repository import (
    SiteSettingsLoadPool,
    SiteSettingsRow,
    load_site_settings,
    save_site_settings,
)


# строка postgresql для теста
class FakeSettingsRow:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value

    def __getitem__(self, key: str) -> str:
        if key == "key":
            return self.key

        if key == "value":
            return self.value

        raise KeyError(key)


# маленькая версия пула postgresql для теста
class FakePool(SiteSettingsLoadPool):
    def __init__(self, rows: Sequence[SiteSettingsRow]) -> None:
        self.rows = rows

    async def fetch(self, query: str) -> Sequence[SiteSettingsRow]:
        # загрузчик должен читать именно таблицу site_settings
        assert "FROM site_settings" in query

        return self.rows


class FakeSaveConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transactions += 1
        yield

    async def execute(self, query: str, *args: object) -> str:
        self.executed.append((query, args))
        return "INSERT 0 1"


class FakeSavePool:
    def __init__(self) -> None:
        self.connection = FakeSaveConnection()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeSaveConnection]:
        yield self.connection


@pytest.mark.anyio
async def test_load_site_settings_from_database() -> None:
    """настройки из postgresql превращаются в SiteSettings"""

    pool = FakePool(
        [
            FakeSettingsRow(
                "thread",
                '{"max_subject_length": 150}',
            ),
            FakeSettingsRow(
                "post",
                '{"max_post_length": 5000}',
            ),
            FakeSettingsRow(
                "localization",
                '{"default_locale": "en", "anonymous_name": "Anonymous"}',
            ),
            FakeSettingsRow(
                "appearance",
                '{"active_theme": "light"}',
            ),
        ]
    )

    settings = await load_site_settings(pool)

    assert settings.thread.max_subject_length == 150
    assert settings.post.max_post_length == 5000
    assert settings.post.max_author_name_length == 50
    assert settings.localization.default_locale == "en"
    assert settings.localization.anonymous_name == "Anonymous"
    assert settings.appearance.active_theme == "light"


@pytest.mark.anyio
async def test_load_site_settings_reject_invalid_database_value() -> None:
    """испорченные настройки из postgresql не проходят проверку"""

    pool = FakePool(
        [
            FakeSettingsRow(
                "post",
                '{"max_post_length": -1}',
            ),
        ]
    )

    with pytest.raises(ValidationError):
        await load_site_settings(pool)


@pytest.mark.anyio
async def test_save_site_settings_uses_one_transaction() -> None:
    """сохранение групп настроек выполняется одной транзакцией"""

    pool = FakeSavePool()

    await save_site_settings(pool, get_default_site_settings())

    assert pool.connection.transactions == 1
    assert len(pool.connection.executed) == 5
    assert all("updated_at = now()" in query for query, _ in pool.connection.executed)
