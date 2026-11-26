"""загрузка настроек сайта из postgresql"""

import json
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.core.site_settings import SiteSettings, get_default_site_settings


# строка с key и value, которую возвращает postgresql
class SiteSettingsRow(Protocol):
    def __getitem__(self, key: str) -> str: ...


# загрузчику нужен только асинхронный fetch
class SiteSettingsLoadPool(Protocol):
    async def fetch(self, query: str) -> Sequence[SiteSettingsRow]: ...


class SiteSettingsSaveConnection(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[None]: ...

    async def execute(self, query: str, *args: object) -> str: ...


class SiteSettingsSavePool(Protocol):
    def acquire(self) -> AbstractAsyncContextManager[SiteSettingsSaveConnection]: ...


async def load_site_settings(pool: SiteSettingsLoadPool) -> SiteSettings:
    """загрузить настройки сайта и проверить их"""

    # берём все группы настроек одним запросом
    rows = await pool.fetch(
        """
        SELECT key, value
        FROM site_settings
        ORDER BY key
        """
    )

    # начинаем с настроек из python
    settings_data = get_default_site_settings().model_dump()

    for row in rows:
        # asyncpg отдаёт jsonb как текст, json.loads превращает его в dict
        settings_data[row["key"]] = json.loads(row["value"])

    # Pydantic проверит типы, лимиты и неизвестные поля
    return SiteSettings.model_validate(settings_data)


async def save_site_settings(pool: SiteSettingsSavePool, settings: SiteSettings) -> None:
    """сохранить все группы настроек в postgresql"""

    # все группы либо сохранятся вместе, либо база не изменится
    async with pool.acquire() as connection:
        async with connection.transaction():
            for key, value in settings.model_dump().items():
                await connection.execute(
                    """
                    INSERT INTO site_settings (key, value)
                    VALUES ($1, $2::jsonb)
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = now()
                    """,
                    key,
                    json.dumps(value, ensure_ascii=False),
                )
