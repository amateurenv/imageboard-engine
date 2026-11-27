"""порт будущей антивирусной проверки до готовности media"""

from pathlib import Path
from typing import Protocol


class MediaScanner(Protocol):
    """проверить закрытый quarantine файл до создания public derivative"""

    def scan(self, path: Path) -> None:
        """завершиться без ошибки только для разрешённого файла"""


class NoopMediaScanner:
    """явно временный scanner для development без ложного заявления об антивирусе"""

    def scan(self, path: Path) -> None:
        """не проверять файл пока production scanner не подключён"""

        del path
