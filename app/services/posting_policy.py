"""проверка доступа к тредам и ответам без привязки к HTTP"""

from dataclasses import dataclass
from enum import StrEnum


class PostingPermissionCode(StrEnum):
    """машинные причины отказа будущего api"""

    AUTHENTICATION_REQUIRED = "authentication_required"
    POSTING_FORBIDDEN = "posting_forbidden"


class PostingPermissionError(PermissionError):
    """отказ с различием между отсутствием входа и запретом"""

    def __init__(self, code: PostingPermissionCode) -> None:
        self.code = code
        super().__init__(code.value)

    @property
    def http_status(self) -> int:
        """подготовить будущий transport к корректному статусу"""

        return 401 if self.code is PostingPermissionCode.AUTHENTICATION_REQUIRED else 403


@dataclass(frozen=True, slots=True)
class PostingBoard:
    """минимальные данные доски для решения о публикации"""

    board_type: str
    owner_user_id: int | None
    thread_creation_policy: str
    reply_policy: str


def check_posting_permission(
    board: PostingBoard,
    *,
    policy: str,
    actor_user_id: int | None,
    actor_role: str | None,
) -> None:
    """разрешить staff override и владельцу писать на своей profile-доске"""

    if actor_role in {"owner", "admin", "moderator"}:
        return
    if board.board_type == "profile" and actor_user_id == board.owner_user_id:
        return
    if policy == "anyone":
        return
    if policy == "authenticated" and actor_user_id is not None:
        return
    if actor_user_id is None:
        raise PostingPermissionError(PostingPermissionCode.AUTHENTICATION_REQUIRED)
    raise PostingPermissionError(PostingPermissionCode.POSTING_FORBIDDEN)
