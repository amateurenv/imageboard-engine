"""контролируемая работа с локальными файлами media"""

import os
import stat
from pathlib import Path, PurePosixPath
from uuid import uuid4


class MediaStorageError(ValueError):
    """путь или операция не соответствуют изолированному storage"""


class MediaStorage:
    """создавать и удалять только обычные файлы внутри одного корня"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        """вернуть абсолютный корень только для внутреннего кода"""

        return self._root

    def new_quarantine_path(self, suffix: str) -> str:
        """создать непредсказуемое относительное имя без исходного filename"""

        return f"quarantine/{uuid4().hex}{suffix}"

    def resolve(self, relative_path: str) -> Path:
        """разрешить только нормальный относительный путь внутри storage root"""

        relative = PurePosixPath(relative_path)
        if (
            not relative_path
            or relative.is_absolute()
            or ".." in relative.parts
            or "." in relative.parts
        ):
            raise MediaStorageError("media path is outside storage root")
        candidate = self._root / Path(*relative.parts)
        # проверяем symlink только у папок, но не у конечного файла: lstat ниже
        # обязан увидеть попытку подменить сам cleanup target ссылкой
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(self._root)
        except ValueError as error:
            raise MediaStorageError("media path is outside storage root") from error
        return resolved_parent / candidate.name

    def prepare_parent(self, relative_path: str) -> Path:
        """создать приватную папку и вернуть проверенный путь файла"""

        path = self.resolve(relative_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        return path

    def open_new(self, relative_path: str) -> tuple[int, Path]:
        """открыть новый файл без перехода по symlink и без перезаписи"""

        path = self.prepare_parent(relative_path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        return descriptor, path

    def replace_from_temporary(self, temporary_path: Path, relative_path: str) -> Path:
        """атомарно опубликовать готовый внутренний файл в разрешённом месте"""

        destination = self.prepare_parent(relative_path)
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
        return destination

    def temporary_path(self, relative_path: str) -> Path:
        """дать одноразовый путь рядом с будущим файлом для atomic replace"""

        destination = self.prepare_parent(relative_path)
        return destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")

    def delete_file(self, relative_path: str) -> bool:
        """удалить только обычный файл, не symlink и не каталог"""

        path = self.resolve(relative_path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        if not stat.S_ISREG(info.st_mode):
            raise MediaStorageError("cleanup target is not a regular file")
        path.unlink()
        return True
