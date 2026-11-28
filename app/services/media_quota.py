"""учёт места до записи медиа и сверка с локальной файловой системой"""

import errno
import os
import shutil
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from asyncpg import Connection, Pool

from app.core.config import Settings, get_settings
from app.services.media_storage import MediaStorage

MIB = 1024**2
PREVIEW_BYTES = 2 * MIB
MP4_WORK_BYTES = 22 * MIB
AUDIO_BYTES = 48 * MIB
VOICE_BYTES = 6 * MIB
IMAGE_BYTES = 32 * MIB
ANIMATION_BYTES = 128 * MIB
WEBM_WORK_BYTES = 2 * MIB
TGS_BYTES = 64 * 1024
STICKER_BYTES = 512 * 1024
DISK_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
BLOCK_ALLOWANCE = 64 * 1024


class MediaStorageFull(RuntimeError):
    """допуск записи закрыт до восстановления места или инвентаризации"""


class MediaOutputTooLarge(ValueError):
    """encoder превысил жёсткий предел файла"""


def work_limits(kind: str, mime: str, animation: bool) -> tuple[int, ...]:
    """вернуть пределы файлов в порядке основного результата и превью"""

    if kind in {"video", "video_note"}:
        return MP4_WORK_BYTES, PREVIEW_BYTES
    if kind == "audio":
        return (AUDIO_BYTES,)
    if kind == "voice":
        return (VOICE_BYTES,)
    if kind == "sticker":
        if mime == "video/webm":
            return WEBM_WORK_BYTES, PREVIEW_BYTES
        if animation:
            return TGS_BYTES, STICKER_BYTES
        return (STICKER_BYTES,)
    return (ANIMATION_BYTES if animation else IMAGE_BYTES), PREVIEW_BYTES


def reserved_bytes(size: int) -> int:
    """учесть округление файловой системы и служебные блоки"""

    return size + BLOCK_ALLOWANCE


def work_budget(kind: str, mime: str, animation: bool) -> int:
    """подсчитать совместный максимум временных и выходных файлов"""

    return sum(map(reserved_bytes, work_limits(kind, mime, animation)))


class MediaQuota:
    """короткие транзакции допуска для одного общего тома"""

    def __init__(self, pool: Pool, storage: MediaStorage, settings: Settings | None = None):
        self.pool = pool
        self.storage = storage
        self.settings = settings or get_settings()

    def verify_mount(self) -> None:
        """не записывать на системный диск после размонтирования production тома"""

        if self.settings.app_env != "production":
            return
        root = self.storage.root
        if sys.platform != "linux" or not root.is_mount():
            raise MediaStorageFull("media filesystem is not mounted")
        if root.stat().st_dev == Path(root.anchor).stat().st_dev:
            raise MediaStorageFull("media filesystem must be separate from the system filesystem")

    def volume_id(self, *, create: bool = False) -> UUID:
        """связать реестр с конкретным томом"""

        self.verify_mount()
        marker = self.storage.resolve(".volume-id")
        try:
            if create and not marker.exists():
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as target:
                    target.write(str(uuid4()))
                    target.flush()
                    os.fsync(target.fileno())
                self.storage.sync_directory(marker.parent)
            if marker.is_symlink():
                raise MediaStorageFull("invalid media volume marker")
            return UUID(marker.read_text(encoding="ascii"))
        except (OSError, ValueError) as error:
            raise MediaStorageFull("media inventory is missing or unreadable") from error

    def free_bytes(self) -> int:
        """проверить доступные блоки и минимальный запас inode"""

        self.verify_mount()
        try:
            if os.name == "posix":
                info = os.statvfs(self.storage.root)
                if info.f_files and info.f_favail <= 64 + self.settings.media_ingress_file_slots:
                    return 0
                return info.f_bavail * info.f_frsize
            return shutil.disk_usage(self.storage.root).free
        except OSError as error:
            raise MediaStorageFull("cannot inspect media filesystem") from error

    async def check_inventory(self) -> None:
        """проверить готовность реестра до приёма multipart тела"""

        current = await self.pool.fetchval("SELECT volume_id FROM media_disk_inventory")
        if current is None or current != self.volume_id():
            raise MediaStorageFull("media inventory belongs to another filesystem")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """открыть короткую транзакцию учёта"""

        async with self.pool.acquire() as connection, connection.transaction():
            yield connection

    async def admit(
        self, connection: Connection, increase: int, *, pending_delta: int | None = None,
    ) -> None:
        """проверить объём под общей блокировкой до увеличения резерва"""

        await self._lock_inventory(connection)
        charged, pending = await self.usage(connection)
        ingress = self.settings.media_ingress_budget
        future_writes = pending + (increase if pending_delta is None else pending_delta)
        if (
            charged + increase + ingress > self.settings.media_storage_max_bytes
            or self.free_bytes()
            < self.settings.media_storage_min_free_bytes + future_writes + ingress
        ):
            raise MediaStorageFull("media storage quota reached")

    async def usage(self, connection: Connection) -> tuple[int, int]:
        """вернуть учтённые байты и объём ещё не выполненных записей"""
        row = await connection.fetchrow(
            """SELECT coalesce(sum(bytes + work_bytes), 0) AS charged,
                      coalesce(sum(work_bytes + CASE WHEN expires_at IS NOT NULL
                                                   THEN bytes ELSE 0 END), 0) AS pending
               FROM media_disk_files"""
        )
        assert row is not None
        return int(row["charged"]), int(row["pending"])

    async def _lock_inventory(self, connection: Connection) -> None:
        inventory = await connection.fetchval(
            "SELECT volume_id FROM media_disk_inventory FOR UPDATE",
        )
        if inventory is None or inventory != self.volume_id():
            raise MediaStorageFull("media inventory belongs to another filesystem")

    async def start_work(
        self, connection: Connection, path: str, work: int, files: list[str],
    ) -> None:
        """допустить активную запись с учётом других одновременно работающих процессов"""

        await self._lock_inventory(connection)
        pending = await connection.fetchval(
            """SELECT coalesce(sum(
                   CASE WHEN expires_at IS NOT NULL THEN bytes ELSE 0 END
                   + CASE WHEN cardinality(work_paths)>0 THEN work_bytes ELSE 0 END
               ), 0) FROM media_disk_files"""
        )
        floor = self.settings.media_storage_min_free_bytes + self.settings.media_ingress_budget
        if self.free_bytes() < floor + pending + work:
            raise MediaStorageFull("not enough free space to start a reserved media job")
        await connection.execute(
            "UPDATE media_disk_files SET work_bytes=$2, work_paths=$3 WHERE path=$1",
            path, work, files,
        )

    async def reserve_source(self, path: str, maximum: int) -> None:
        """зафиксировать путь до создания исходного файла"""

        quota = reserved_bytes(maximum)
        async with self.transaction() as connection:
            await self.admit(connection, quota)
            await connection.execute(
                """INSERT INTO media_disk_files(path, owner_path, bytes, expires_at)
                   VALUES ($1, $1, $2, clock_timestamp() + interval '20 minutes')""",
                path, quota,
            )

    async def promote_source(
        self, connection: Connection, path: str, *, media_id: int, size: int, work: int,
    ) -> None:
        """закрепить место для обработки в транзакции создания queued записи"""

        held = await connection.fetchval(
            "SELECT bytes FROM media_disk_files WHERE path=$1 AND expires_at IS NOT NULL", path,
        )
        charge = reserved_bytes(size)
        if held is None or charge > held:
            raise MediaStorageFull("upload exceeded its reservation")
        # исходник уже занимает место на томе - оставляем только будущие записи
        await self.admit(connection, charge + work - held, pending_delta=work - held)
        await connection.execute(
            """UPDATE media_disk_files SET media_id=$2, bytes=$3, work_bytes=$4, expires_at=NULL
               WHERE path=$1""",
            path, media_id, charge, work,
        )

    async def discard_source(self, path: str) -> None:
        """удалить только незавершённую загрузку под блокировкой её владельца"""

        staged = await self.pool.fetchval(
            "SELECT expires_at IS NOT NULL FROM media_disk_files WHERE path=$1", path,
        )
        if staged:
            self.storage.delete_file(path)
            await self.pool.execute(
                "DELETE FROM media_disk_files WHERE path=$1 AND expires_at IS NOT NULL", path,
            )


def existing_charge(path: Path) -> int:
    """учесть обычный файл и реально выделенные блоки при инвентаризации"""

    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("inventory path is not a regular file")
    return reserved_bytes(max(info.st_size, getattr(info, "st_blocks", 0) * 512))
