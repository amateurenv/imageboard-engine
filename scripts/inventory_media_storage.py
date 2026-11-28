"""первичный учёт файлов при остановленной записи и очистке"""

import argparse
import asyncio
import os
import stat
from pathlib import Path

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.services.media_quota import MediaQuota, existing_charge, work_budget
from app.services.media_storage import MediaStorage


def _raise_walk_error(error: OSError) -> None:
    raise error


async def inventory_media_storage(
    quota: MediaQuota, *, reconcile: bool = False,
) -> tuple[int, int, int]:
    """учесть существующие файлы без изменения публикаций и без удаления"""

    storage = quota.storage
    quota.verify_mount()
    storage.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with quota.transaction() as connection:
        await connection.execute("LOCK TABLE media_disk_inventory IN ACCESS EXCLUSIVE MODE")
        inventory = await connection.fetchval("SELECT volume_id FROM media_disk_inventory")
        if inventory is not None and not reconcile:
            raise RuntimeError("media storage is already inventoried")
        volume = quota.volume_id(create=inventory is None)
        if inventory is not None and volume != inventory:
            raise RuntimeError("media storage is mounted from a different volume")
        rows = await connection.fetch(
            """SELECT id, path, size, media_kind, mime_type, is_animation, processing_state,
                      display_path, preview_path, canonical_path FROM media"""
        )
        known: dict[str, tuple[str, int | None, int]] = {}
        for row in rows:
            path = row["path"]
            work = (
                work_budget(row["media_kind"], row["mime_type"], row["is_animation"])
                if row["processing_state"] in {"queued", "processing"} else 0
            )
            known[path] = (path, row["id"], work)
            for key in ("display_path", "preview_path", "canonical_path"):
                if row[key]:
                    known[row[key]] = (path, row["id"], 0)
        for task in await connection.fetch(
            """SELECT relative_path FROM media_cleanup_tasks WHERE completed_at IS NULL
               AND NOT EXISTS(SELECT 1 FROM media_disk_files
                              WHERE path=relative_path AND NOT managed)"""
        ):
            path = task["relative_path"]
            known.setdefault(path, (path, None, 0))
        managed_work: set[str] = set()
        for entry in await connection.fetch(
            "SELECT path, owner_path, media_id, work_paths FROM media_disk_files WHERE managed"
        ):
            known.setdefault(entry["path"], (entry["owner_path"], entry["media_id"], 0))
            managed_work.update(entry["work_paths"])

        seen: set[str] = set()
        entries = []
        unknown = 0
        root_device = storage.root.stat().st_dev
        for dirname, dirs, files in os.walk(storage.root, onerror=_raise_walk_error):
            parent = Path(dirname)
            for name in dirs:
                info = (parent / name).lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_device:
                    raise ValueError(f"unexpected entry in media storage: {parent / name}")
            for name in files:
                file = parent / name
                path = file.relative_to(storage.root).as_posix()
                if path == ".volume-id":
                    continue
                charge = existing_charge(file)
                if path in managed_work:
                    continue
                if path not in known:
                    if path.startswith(".locks/") and not file.stat().st_size:
                        continue
                    unknown += 1
                    print(f"unknown file kept: {path!r}")
                owner, media_id, work = known.get(path, (path, None, 0))
                entries.append((path, owner, media_id, charge, work, path in known))
                seen.add(path)
        missing = known.keys() - seen
        for path in sorted(missing):
            owner, media_id, work = known[path]
            if work:
                entries.append((path, owner, media_id, 0, work, True))
                print(f"queued source missing: {path!r}")
        await connection.executemany(
            """INSERT INTO media_disk_files(path, owner_path, media_id, bytes, work_bytes, managed)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT(path) DO UPDATE SET bytes=EXCLUDED.bytes
               WHERE NOT media_disk_files.managed""",
            entries,
        )
        if inventory is None:
            await connection.execute(
                "INSERT INTO media_disk_inventory(volume_id) VALUES ($1)", volume,
            )
        else:
            await connection.execute(
                "DELETE FROM media_disk_files WHERE NOT managed AND NOT (path=ANY($1::text[]))",
                list(seen),
            )
    return len(seen), unknown, len(missing)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    pool = await create_postgres_pool(settings)
    try:
        inventoried, unknown, missing = await inventory_media_storage(
            MediaQuota(pool, MediaStorage(settings.media_storage_root), settings),
            reconcile=args.reconcile,
        )
        print(f"inventory complete: {inventoried} files, {unknown} unknown kept, {missing} absent")
    finally:
        await close_postgres_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
