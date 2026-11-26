"""проверка cursor и страниц тредов"""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_threads_service
from app.core.pagination import (
    MAX_CURSOR_LENGTH,
    PostCursor,
    decode_post_cursor,
    decode_thread_cursor,
    encode_post_cursor,
    encode_thread_cursor,
)
from app.core.site_settings import SiteSettings
from app.main import app
from app.repositories.threads import (
    GET_THREAD_REPLIES_PAGE_SQL,
    LIST_THREADS_PAGE_SQL,
    PostRecord,
    ThreadPageRecord,
    ThreadRecord,
)
from app.schemas.threads import ThreadPageResponse
from app.services.threads import ThreadsService


def make_thread_record(thread_id: int, *, sticky: bool = False) -> ThreadRecord:
    """создать запись треда для теста"""

    return ThreadRecord(
        id=thread_id,
        subject=f"тред {thread_id}",
        is_sticky=sticky,
        is_locked=False,
        is_archived=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        bumped_at=datetime(2026, 1, thread_id, tzinfo=UTC),
    )


def make_post_record(post_id: int) -> PostRecord:
    """создать запись поста для теста"""

    return PostRecord(
        id=post_id,
        author_name=None,
        body=f"пост {post_id}",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_thread_cursor_round_trip_and_board_check() -> None:
    """cursor тредов сохраняет позицию и привязан к доске"""

    cursor = encode_thread_cursor(
        board_slug="b",
        sticky=False,
        bumped_at=datetime(2026, 1, 1, tzinfo=UTC),
        thread_id=15,
    )

    decoded = decode_thread_cursor(cursor, "b")

    assert decoded is not None
    assert decoded.id == 15
    assert decoded.board_slug == "b"

    with pytest.raises(HTTPException, match="invalid cursor"):
        decode_thread_cursor(cursor, "tech")


@pytest.mark.parametrize("cursor", ["not-base64", "e30", "a" * (MAX_CURSOR_LENGTH + 1)])
def test_invalid_thread_cursor_returns_same_error(cursor: str) -> None:
    """битый cursor не раскрывает детали и не становится sql"""

    with pytest.raises(HTTPException) as error:
        decode_thread_cursor(cursor, "b")

    assert error.value.status_code == 422
    assert error.value.detail == "invalid cursor"


def test_post_cursor_rejects_another_thread() -> None:
    """cursor одного треда нельзя применить к другому"""

    cursor = encode_post_cursor(thread_id=10, before_id=30)

    with pytest.raises(HTTPException, match="invalid cursor"):
        decode_post_cursor(cursor, 11)


def test_pagination_sql_uses_keyset_and_limit_plus_one() -> None:
    """запросы не используют медленный offset и читают лишнюю запись"""

    assert "OFFSET" not in LIST_THREADS_PAGE_SQL
    assert "LIMIT $5" in LIST_THREADS_PAGE_SQL
    assert "(bumped_at, id)" in LIST_THREADS_PAGE_SQL
    assert "OFFSET" not in GET_THREAD_REPLIES_PAGE_SQL
    assert "LIMIT $4" in GET_THREAD_REPLIES_PAGE_SQL


class FakeThreadsRepository:
    """репозиторий с подготовленными страницами"""

    # noinspection PyMethodMayBeStatic
    async def get_board_id_by_slug(self, slug: str) -> int | None:
        """вернуть активную доску"""

        return 1 if slug == "b" else None

    # noinspection PyMethodMayBeStatic
    async def list_threads_page(
        self,
        *,
        board_id: int,
        cursor_sticky: bool | None,
        cursor_bumped_at: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[ThreadRecord]:
        """вернуть limit плюс одну запись для проверки next_cursor"""

        assert board_id == 1
        assert limit == 2
        assert cursor_sticky is None
        assert cursor_bumped_at is None
        assert cursor_id is None

        return [make_thread_record(3), make_thread_record(2), make_thread_record(1)]

    # noinspection PyMethodMayBeStatic
    async def get_thread_page(
        self,
        *,
        thread_id: int,
        before_id: int | None,
        limit: int,
    ) -> ThreadPageRecord | None:
        """вернуть OP и три ответа в порядке от новых к старым"""

        assert thread_id == 7
        assert before_id is None
        assert limit == 2

        return ThreadPageRecord(
            thread=make_thread_record(7),
            op=make_post_record(1),
            replies=[make_post_record(4), make_post_record(3), make_post_record(2)],
        )


class FakeWsManager:
    """заглушка websocket для чтения"""

    async def broadcast(self, event: dict[str, object]) -> None:
        """не отправлять события в unit тесте"""


@pytest.mark.anyio
async def test_service_returns_thread_page_with_next_cursor() -> None:
    """service обрезает лишнюю запись и создаёт следующий cursor"""

    service = ThreadsService(FakeThreadsRepository(), SiteSettings(), FakeWsManager())

    response = await service.list_threads("b", None, 2)

    assert response is not None
    assert [item.id for item in response.items] == [3, 2]
    assert response.next_cursor is not None
    assert decode_thread_cursor(response.next_cursor, "b").id == 2


@pytest.mark.anyio
async def test_service_returns_op_and_replies_in_time_order() -> None:
    """OP не дублируется, а ответы приходят от старого к новому"""

    service = ThreadsService(FakeThreadsRepository(), SiteSettings(), FakeWsManager())

    response = await service.get_thread(7, None, 2)

    assert response is not None
    assert response.op is not None
    assert response.op.id == 1
    assert [reply.id for reply in response.replies] == [3, 4]
    assert response.next_cursor is not None
    assert decode_post_cursor(response.next_cursor, 7) == PostCursor(
        v=1,
        thread_id=7,
        before_id=3,
    )


class FakeThreadsService:
    """service для проверки HTTP-параметров"""

    # noinspection PyMethodMayBeStatic
    async def get_thread(
        self,
        thread_id: int,
        cursor: PostCursor | None,
        limit: int,
    ) -> ThreadPageResponse:
        """вернуть короткий ответ без postgresql"""

        return ThreadPageResponse(
            thread={
                "id": thread_id,
                "subject": "тест",
                "is_sticky": False,
                "is_locked": False,
                "is_archived": False,
                "created_at": "2026-01-01T00:00:00Z",
                "bumped_at": "2026-01-01T00:00:00Z",
            },
            op=None,
        )


def get_fake_threads_service() -> FakeThreadsService:
    """вернуть service без внешних подключений"""

    return FakeThreadsService()


@pytest.mark.anyio
async def test_thread_http_rejects_invalid_limit_and_cursor() -> None:
    """http не пропускает слишком большой limit и битый cursor"""

    app.dependency_overrides[get_threads_service] = get_fake_threads_service

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            limit_response = await client.get("/api/v1/threads/7?limit=101")
            cursor_response = await client.get("/api/v1/threads/7?cursor=broken")
    finally:
        app.dependency_overrides.pop(get_threads_service, None)

    assert limit_response.status_code == 422
    assert cursor_response.status_code == 422
    assert cursor_response.json() == {"detail": "invalid cursor"}
