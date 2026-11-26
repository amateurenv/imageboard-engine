"""проверка схем досок"""

import pytest
from pydantic import ValidationError

from app.schemas.boards import BoardListResponse, BoardSummary
from app.schemas.threads import PostCreate, ThreadCreate


def test_board_summary_accepts_valid_slug() -> None:
    """доска принимает допустимый slug"""

    board = BoardSummary(
        slug="my_board-2",
        title="Моя доска",
        description="",
        is_nsfw=False,
    )

    assert board.slug == "my_board-2"
    assert board.title == "Моя доска"
    assert board.description == ""
    assert board.is_nsfw is False


@pytest.mark.parametrize(
    "slug",
    [
        "TECH",
        "my board",
        "b*",
        "-board",
        "",
    ],
)
def test_board_summary_rejects_invalid_slug(slug: str) -> None:
    """доска не принимает недопустимый slug"""

    with pytest.raises(ValidationError):
        BoardSummary(
            slug=slug,
            title="Тест",
            description="",
            is_nsfw=False,
        )


def test_board_list_response_is_empty_by_default() -> None:
    """новый ответ со списком досок содержит пустой список"""

    response = BoardListResponse()

    assert response.items == []


def test_thread_create_requires_opening_post() -> None:
    """новый тред нельзя создать без текста первого поста"""

    with pytest.raises(ValidationError):
        ThreadCreate()


def test_post_create_rejects_empty_body() -> None:
    """текстовый пост не может быть пустым"""

    with pytest.raises(ValidationError):
        PostCreate(body="")
