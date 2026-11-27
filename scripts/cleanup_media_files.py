"""повторяемая очистка controlled media paths после удаления db-записей"""

import asyncio
from datetime import timedelta

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.repositories.media import MediaRepository
from app.services.media_storage import MediaStorage

CLEANUP_LEASE = timedelta(minutes=10)


async def cleanup_media_files(
    *,
    repository: MediaRepository | None = None,
    storage: MediaStorage | None = None,
) -> int:
    """удалить все доступные задачи без recursive delete и без доверия пути из базы"""

    pool = None
    if repository is None or storage is None:
        settings = get_settings()
        pool = await create_postgres_pool(settings)
        repository = MediaRepository(pool)
        storage = MediaStorage(settings.media_storage_root)
    completed = 0
    try:
        while task := await repository.claim_cleanup_task(lease_duration=CLEANUP_LEASE):
            try:
                await asyncio.to_thread(storage.delete_file, task.relative_path)
            except Exception as error:
                await repository.fail_cleanup_task(task.id, str(error))
                # не крутимся на одном недоступном диске, следующий запуск повторит задачу
                break
            await repository.complete_cleanup_task(task.id)
            completed += 1
        print(f"cleaned media files: {completed}")
        return completed
    finally:
        if pool is not None:
            await close_postgres_pool(pool)


if __name__ == "__main__":
    asyncio.run(cleanup_media_files())
