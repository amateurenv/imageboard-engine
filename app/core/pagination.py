"""кодирование и проверка cursor для постраничного чтения"""

import base64
import binascii
import json
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# длинный cursor не нужен и не должен занимать память сервера
MAX_CURSOR_LENGTH = 512


class ThreadCursor(BaseModel):
    """позиция следующей страницы тредов одной доски"""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    board_slug: str = Field(min_length=1, max_length=63)
    sticky: bool
    bumped_at: datetime
    id: int = Field(gt=0)

    @field_validator("bumped_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """не принимать время без часового пояса"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone is required")

        return value


class PostCursor(BaseModel):
    """позиция старых ответов одного треда"""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    thread_id: int = Field(gt=0)
    before_id: int = Field(gt=0)


def encode_thread_cursor(
    *,
    board_slug: str,
    sticky: bool,
    bumped_at: datetime,
    thread_id: int,
) -> str:
    """создать cursor после последнего треда страницы"""

    return _encode(
        ThreadCursor(
            v=1,
            board_slug=board_slug,
            sticky=sticky,
            bumped_at=bumped_at,
            id=thread_id,
        )
    )


def encode_post_cursor(*, thread_id: int, before_id: int) -> str:
    """создать cursor перед самым старым ответом страницы"""

    return _encode(PostCursor(v=1, thread_id=thread_id, before_id=before_id))


def decode_thread_cursor(cursor: str | None, board_slug: str) -> ThreadCursor | None:
    """прочитать cursor тредов и сверить доску"""

    if cursor is None:
        return None

    try:
        decoded = ThreadCursor.model_validate(_decode(cursor))
    except (ValidationError, ValueError, TypeError):
        raise _invalid_cursor() from None

    if decoded.board_slug != board_slug:
        raise _invalid_cursor()

    return decoded


def decode_post_cursor(cursor: str | None, thread_id: int) -> PostCursor | None:
    """прочитать cursor ответов и сверить тред"""

    if cursor is None:
        return None

    try:
        decoded = PostCursor.model_validate(_decode(cursor))
    except (ValidationError, ValueError, TypeError):
        raise _invalid_cursor() from None

    if decoded.thread_id != thread_id:
        raise _invalid_cursor()

    return decoded


def _encode(model: BaseModel) -> str:
    """сериализовать данные cursor в короткую url-safe строку"""

    raw = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(cursor: str) -> dict[str, object]:
    """декодировать строку cursor без доверия её содержимому"""

    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("invalid cursor length")

    padding = "=" * (-len(cursor) % 4)

    try:
        raw = base64.b64decode(
            (cursor + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise ValueError("invalid cursor encoding") from None

    if not isinstance(data, dict):
        raise ValueError("cursor must contain an object")

    return data


def _invalid_cursor() -> HTTPException:
    """вернуть одинаковую ошибку для всех неверных cursor"""

    return HTTPException(status_code=422, detail="invalid cursor")
