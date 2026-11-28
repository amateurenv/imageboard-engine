"""повторяемая очистка controlled media paths после удаления db-записей"""

import asyncio
import logging
from datetime import timedelta

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.repositories.media import MediaRepository
from app.services.media_quota import MediaQuota
from app.services.media_storage import MediaStorage

CLEANUP_LEASE = timedelta(minutes=10)
logger = logging.getLogger(__name__)


async def cleanup_media_files(
    *,
    repository: MediaRepository | None = None,
    storage: MediaStorage | None = None,
) -> int:
    """удалить все доступные задачи без recursive delete и без доверия пути из базы"""

    pool = None
    standalone = repository is None or storage is None
    if standalone:
        settings = get_settings()
        pool = await create_postgres_pool(settings)
        repository = MediaRepository(pool)
        storage = MediaStorage(settings.media_storage_root)
    completed = 0
    try:
        assert repository is not None and storage is not None
        if standalone:
            await MediaQuota(repository.pool, storage).check_inventory()
        await repository.queue_expired_uploads()
        await repository.queue_failed_derivatives()
        while task := await repository.claim_cleanup_task(lease_duration=CLEANUP_LEASE):
            try:
                with storage.guard(task.owner_path) as acquired:
                    if not acquired:
                        await repository.fail_cleanup_task(task, "media writer is still active")
                        continue
                    if not await repository.cleanup_still_allowed(task):
                        await repository.cancel_cleanup_task(task)
                        continue
                    deleting = asyncio.create_task(
                        asyncio.to_thread(storage.delete_file, task.relative_path),
                    )
                    try:
                        await asyncio.shield(deleting)
                    except asyncio.CancelledError:
                        await asyncio.gather(deleting, return_exceptions=True)
                        raise
                    await repository.complete_cleanup_task(task)
            except Exception as error:
                await repository.fail_cleanup_task(task, str(error))
                logger.warning("media cleanup task %s deferred", task.id)
                continue
            completed += 1
        print(f"cleaned media files: {completed}")
        return completed
    finally:
        if pool is not None:
            await close_postgres_pool(pool)


if __name__ == "__main__":
    asyncio.run(cleanup_media_files())
