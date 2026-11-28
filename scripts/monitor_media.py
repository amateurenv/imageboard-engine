"""снять сводку эксплуатации медиа без записи в бд и хранилище"""

import asyncio
import json
from datetime import timedelta

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.repositories.media import queue_metrics
from app.services.media_quota import MediaQuota, MediaStorageFull
from app.services.media_storage import MediaStorage

BACKLOG_AGE = timedelta(minutes=15)


async def snapshot(quota: MediaQuota) -> dict[str, int]:
    """посчитать текущие очереди, ошибки и запас для новых записей"""
    await quota.check_inventory()
    async with quota.pool.acquire() as connection, connection.transaction(
        isolation="repeatable_read", readonly=True,
    ):
        charged, pending = await quota.usage(connection)
        queues = await queue_metrics(connection, BACKLOG_AGE)
    settings = quota.settings
    free = quota.free_bytes()
    ingress = settings.media_ingress_budget
    return {
        "disk_usable_free_bytes": free,
        "disk_min_free_bytes": settings.media_storage_min_free_bytes,
        "disk_headroom_bytes": free - settings.media_storage_min_free_bytes - pending - ingress,
        "quota_charged_bytes": charged,
        "quota_pending_bytes": pending,
        "quota_ingress_budget_bytes": ingress,
        "quota_limit_bytes": settings.media_storage_max_bytes,
        "quota_headroom_bytes": settings.media_storage_max_bytes - charged - ingress,
        **queues,
    }


def warnings(metrics: dict[str, int]) -> list[str]:
    """выбрать причины для предупреждения по текущему снимку"""
    result = [key for key in (
        "old_queued", "old_cleanup", "expired_processing_leases", "expired_cleanup_leases",
        "failed", "processing_retry_errors", "cleanup_errors",
    ) if metrics[key]]
    if metrics["disk_headroom_bytes"] <= 0:
        result.append("disk_headroom_exhausted")
    if metrics["quota_headroom_bytes"] * 10 <= metrics["quota_limit_bytes"]:
        result.append("quota_at_least_90_percent_used")
    return result


async def main() -> int:
    try:
        settings = get_settings()
        pool = await create_postgres_pool(settings)
        try:
            quota = MediaQuota(pool, MediaStorage(settings.media_storage_root), settings)
            metrics = await snapshot(quota)
        finally:
            await close_postgres_pool(pool)
    except MediaStorageFull as error:
        report = {"status": "error", "error": str(error)}
    except Exception as error:
        report = {"status": "error", "error": type(error).__name__}
    else:
        alerts = warnings(metrics)
        print(json.dumps({"status": "warning" if alerts else "ok", "warnings": alerts, **metrics}))
        return int(bool(alerts))
    print(json.dumps(report))
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
