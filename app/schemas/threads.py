"""формы запросов и ответов для тредов"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ThreadSummary(BaseModel):
    """тред в списке доски"""

    id: int
    subject: str
    is_sticky: bool
    bumped_at: datetime


class ThreadListResponse(BaseModel):
    """ответ со списком тредов доски"""

    items: list[ThreadSummary] = Field(default_factory=list)

    # null значит, что следующей страницы больше нет
    next_cursor: str | None = None


class ThreadDetail(BaseModel):
    """данные треда"""

    id: int
    subject: str
    is_sticky: bool
    is_locked: bool
    is_archived: bool
    created_at: datetime
    bumped_at: datetime


class PostSummary(BaseModel):
    """данные одного поста"""

    id: int
    # для анонимных постов (author_name пустой в базе) подставляется localization.anonymous_name
    author_name: str
    body: str
    created_at: datetime


class ThreadWithPosts(BaseModel):
    """новый тред и его первый пост"""

    thread: ThreadDetail
    posts: list[PostSummary]


class ThreadPageResponse(BaseModel):
    """страница треда с OP и частью ответов"""

    thread: ThreadDetail

    # у старого пустого треда может не быть поста
    op: PostSummary | None

    # ответы идут по времени от старого к новому
    replies: list[PostSummary] = Field(default_factory=list)

    # cursor для более старых ответов, null если они закончились
    next_cursor: str | None = None


class ThreadCreate(BaseModel):
    """создать тред вместе с первым постом"""

    # публичная форма не принимает staff-поля вроде is_sticky
    model_config = ConfigDict(extra="forbid")

    # заголовок треда, пустой допустим
    subject: str = Field(default="", max_length=1000)

    # имя автора, пустое или None - анонимно
    author_name: str | None = Field(default=None, max_length=500)

    # тред без первого поста не создаём
    body: str = Field(min_length=1, max_length=100000)


class PostCreate(BaseModel):
    """создать пост в треде (данные из запроса)"""

    # неизвестные поля в публичном запросе считаются ошибкой
    model_config = ConfigDict(extra="forbid")

    # имя автора, пустое или None — анонимно
    author_name: str | None = Field(default=None, max_length=500)
    # без медиа пустой текст не имеет смысла
    body: str = Field(min_length=1, max_length=100000)
