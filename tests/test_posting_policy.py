"""проверка независимых policy тредов и ответов"""

import pytest

from app.services.posting_policy import (
    PostingBoard,
    PostingPermissionCode,
    PostingPermissionError,
    check_posting_permission,
)


def profile_board() -> PostingBoard:
    """личная доска с разными правилами тредов и ответов"""

    return PostingBoard("profile", 7, "owner_only", "authenticated")


def test_owner_can_write_despite_policy() -> None:
    """владелец profile-доски всегда сохраняет право публикации"""

    check_posting_permission(
        profile_board(), policy="owner_only", actor_user_id=7, actor_role="user"
    )


def test_anonymous_authentication_requirement_is_distinct() -> None:
    """клиент видит необходимость входа отдельно от запрета"""

    with pytest.raises(PostingPermissionError) as error:
        check_posting_permission(
            profile_board(), policy="authenticated", actor_user_id=None, actor_role=None
        )

    assert error.value.code is PostingPermissionCode.AUTHENTICATION_REQUIRED


def test_authenticated_non_owner_cannot_bypass_owner_only() -> None:
    """вошедший пользователь не становится владельцем доски"""

    with pytest.raises(PostingPermissionError) as error:
        check_posting_permission(
            profile_board(), policy="owner_only", actor_user_id=8, actor_role="user"
        )

    assert error.value.code is PostingPermissionCode.POSTING_FORBIDDEN


def test_authenticated_non_owner_can_write_when_policy_allows_it() -> None:
    """обычная сессия достаточна для правила authenticated"""

    check_posting_permission(
        profile_board(), policy="authenticated", actor_user_id=8, actor_role="user"
    )


def test_staff_overrides_any_board_policy() -> None:
    """модерация не теряет доступ к закрытой profile-доске"""

    check_posting_permission(
        profile_board(), policy="owner_only", actor_user_id=8, actor_role="moderator"
    )
