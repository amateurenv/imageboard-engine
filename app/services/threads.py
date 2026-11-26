"""правила работы с тредами и постами"""

import re

from app.core.pagination import (
    PostCursor,
    ThreadCursor,
    encode_post_cursor,
    encode_thread_cursor,
)
from app.core.site_settings import SiteSettings
from app.repositories.threads import PostRecord, ThreadRecord, ThreadsRepository
from app.schemas.threads import (
    PostCreate,
    PostSummary,
    ThreadCreate,
    ThreadDetail,
    ThreadListResponse,
    ThreadPageResponse,
    ThreadSummary,
    ThreadWithPosts,
)
from app.ws.manager import WebSocketManager

# паттерн ссылок на посты: >>123
POST_REFERENCE_PATTERN = re.compile(r">>(\d+)")


class ThreadsService:
    """подготовка данных тредов и постов для публичного api"""

    def __init__(
        self,
        repository: ThreadsRepository,
        site_settings: SiteSettings,
        ws_manager: WebSocketManager,
    ) -> None:
        # сервис получает репозиторий, настройки сайта и менеджер realtime-событий
        self._repository = repository
        self._site_settings = site_settings
        self._ws_manager = ws_manager

    async def list_threads(
        self,
        board_slug: str,
        cursor: ThreadCursor | None,
        limit: int,
    ) -> ThreadListResponse | None:
        """вернуть страницу тредов доски, None если доски нет"""

        board_id = await self._repository.get_board_id_by_slug(board_slug)
        if board_id is None:
            return None

        records = await self._repository.list_threads_page(
            board_id=board_id,
            cursor_sticky=None if cursor is None else cursor.sticky,
            cursor_bumped_at=None if cursor is None else cursor.bumped_at,
            cursor_id=None if cursor is None else cursor.id,
            limit=limit,
        )
        page_records = records[:limit]
        next_cursor = None

        if len(records) > limit and page_records:
            last_record = page_records[-1]
            next_cursor = encode_thread_cursor(
                board_slug=board_slug,
                sticky=last_record.is_sticky,
                bumped_at=last_record.bumped_at,
                thread_id=last_record.id,
            )

        items = [
            ThreadSummary(
                id=record.id,
                subject=record.subject,
                is_sticky=record.is_sticky,
                bumped_at=record.bumped_at,
            )
            for record in page_records
        ]

        return ThreadListResponse(items=items, next_cursor=next_cursor)

    async def get_thread(
        self,
        thread_id: int,
        cursor: PostCursor | None,
        limit: int,
    ) -> ThreadPageResponse | None:
        """вернуть OP и страницу последних или более старых ответов"""

        page = await self._repository.get_thread_page(
            thread_id=thread_id,
            before_id=None if cursor is None else cursor.before_id,
            limit=limit,
        )
        if page is None:
            return None

        reply_records = page.replies[:limit]
        next_cursor = None
        if len(page.replies) > limit and reply_records:
            next_cursor = encode_post_cursor(
                thread_id=thread_id,
                before_id=reply_records[-1].id,
            )

        # база вернула ответы от новых к старым, интерфейс получает привычный порядок
        replies = [
            self._to_post_summary(post)
            for post in reversed(reply_records)
        ]

        return ThreadPageResponse(
            thread=self._to_detail(page.thread),
            op=None if page.op is None else self._to_post_summary(page.op),
            replies=replies,
            next_cursor=next_cursor,
        )

    async def create_thread(
        self, board_slug: str, thread_create: ThreadCreate
    ) -> ThreadWithPosts | None:
        """создать тред с первым постом и проверкой настроек"""

        board_id = await self._repository.get_board_id_by_slug(board_slug)
        if board_id is None:
            return None

        self._check_subject_length(thread_create.subject)
        self._check_post_length(thread_create.body)
        self._check_author_name(thread_create.author_name)

        creation_result = await self._repository.create_thread(
            board_id=board_id,
            subject=thread_create.subject,
            author_name=thread_create.author_name or None,
            body=thread_create.body,
        )

        return ThreadWithPosts(
            thread=self._to_detail(creation_result.thread),
            posts=[self._to_post_summary(creation_result.post)],
        )

    async def create_post(
        self, thread_id: int, post_create: PostCreate
    ) -> PostSummary | None:
        """создать пост в треде: вставка, бамп треда, ссылки >>N"""

        self._check_post_length(post_create.body)
        self._check_author_name(post_create.author_name)

        reference_ids = self._extract_references(post_create.body)

        creation_result = await self._repository.create_post(
            thread_id=thread_id,
            author_name=post_create.author_name or None,
            body=post_create.body,
            reference_ids=reference_ids,
        )
        if creation_result is None:
            return None

        # транслируем realtime-событие только после успешного коммита поста
        post_summary = self._to_post_summary(creation_result.post)
        await self._ws_manager.broadcast(
            {
                "event": "post_created",
                "thread_id": thread_id,
                "post": post_summary.model_dump(mode="json"),
            }
        )
        if creation_result.was_bumped:
            await self._ws_manager.broadcast({"event": "thread_bumped", "thread_id": thread_id})

        return post_summary

    def _check_subject_length(self, subject: str) -> None:
        """проверить длину заголовка по thread.max_subject_length"""

        max_length = self._site_settings.thread.max_subject_length
        if len(subject) > max_length:
            raise ValueError(f"subject too long: {len(subject)} > {max_length}")

    def _check_post_length(self, body: str) -> None:
        """проверить длину поста по post.max_post_length"""

        max_length = self._site_settings.post.max_post_length
        if len(body) > max_length:
            raise ValueError(f"body too long: {len(body)} > {max_length}")

    def _check_author_name(self, author_name: str | None) -> None:
        """проверить длину имени по post.max_author_name_length"""

        if author_name:
            max_length = self._site_settings.post.max_author_name_length
            if len(author_name) > max_length:
                raise ValueError(
                    f"author_name too long: {len(author_name)} > {max_length}"
                )

    def _extract_references(self, body: str) -> list[int]:
        """вернуть уникальные числа из >>N в теле поста"""

        references = [int(ref) for ref in POST_REFERENCE_PATTERN.findall(body)]
        return list(dict.fromkeys(references))

    def _to_detail(self, thread: ThreadRecord) -> ThreadDetail:
        """запись репозитория в схему подробного ответа"""

        return ThreadDetail(
            id=thread.id,
            subject=thread.subject,
            is_sticky=thread.is_sticky,
            is_locked=thread.is_locked,
            is_archived=thread.is_archived,
            created_at=thread.created_at,
            bumped_at=thread.bumped_at,
        )

    def _to_post_summary(self, post: PostRecord) -> PostSummary:
        """данные поста, для анонимов — имя из localization"""

        return PostSummary(
            id=post.id,
            author_name=post.author_name or self._site_settings.localization.anonymous_name,
            body=post.body,
            created_at=post.created_at,
        )
