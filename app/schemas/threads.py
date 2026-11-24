"""формы запросов и ответов для тредов"""

from datetime import datetime

from pydantic import BaseModel, Field


class ThreadSummary(BaseModel):
    """тред в списке доски"""

    id: int
    subject: str
    is_sticky: bool
    bumped_at: datetime


class ThreadListResponse(BaseModel):
    """ответ со списком тредов доски"""

    items: list[ThreadSummary] = Field(default_factory=list)


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
    """тред и все его посты по порядку"""

    thread: ThreadDetail
    posts: list[PostSummary]


class ThreadCreate(BaseModel):
    """создать тред (данные из запроса)"""

    # заголовок треда, пустой допустим
    subject: str = Field(default="", max_length=1000)


class PostCreate(BaseModel):
    """создать пост в треде (данные из запроса)"""

    # имя автора, пустое или None — анонимно
    author_name: str | None = Field(default=None, max_length=500)
    body: str = Field(max_length=100000)
