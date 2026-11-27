"""unit-проверки прав self-delete без postgresql и redis"""

from datetime import UTC, datetime

import pytest

from app.core.security import hash_password
from app.repositories.threads import SelfDeletePostRecord, SelfDeleteThreadRecord
from app.repositories.users import UserRecord
from app.services.self_delete import SelfDeleteError, SelfDeleteService
from app.services.threads import DeletionPasswordError, ThreadsService


def account(user_id: int) -> UserRecord:
    """собрать обычного пользователя без staff override"""

    return UserRecord(user_id, f"user-{user_id}", "user", datetime.now(UTC))


def anonymous_post() -> SelfDeletePostRecord:
    """дать анонимный не-op ответ с безопасным hash"""

    return SelfDeletePostRecord(2, 1, 1, None, hash_password("delete-me"), False)


def test_anonymous_password_is_checked_without_account_fallback() -> None:
    """неверный пароль и session другого человека не дают удалить анонимный ответ"""

    target = anonymous_post()
    assert SelfDeleteService._authorize(target, None, "delete-me") == "anonymous_self"
    with pytest.raises(SelfDeleteError, match="self_delete_forbidden"):
        SelfDeleteService._authorize(target, None, "wrong")
    with pytest.raises(SelfDeleteError, match="self_delete_forbidden"):
        SelfDeleteService._authorize(target, account(7), None)


def test_account_ownership_and_anonymous_hash_rules_are_separate() -> None:
    """account удаляет только свой пост, у анонима всегда отдельный hash"""

    owned = SelfDeleteThreadRecord(1, 1, 1, 7, None)
    assert SelfDeleteService._authorize(owned, account(7), None) == "account_self"
    with pytest.raises(SelfDeleteError, match="self_delete_forbidden"):
        SelfDeleteService._authorize(owned, account(8), None)
    with pytest.raises(DeletionPasswordError):
        ThreadsService._deletion_password_hash(None, None)
    first_hash = ThreadsService._deletion_password_hash(None, "same-password")
    second_hash = ThreadsService._deletion_password_hash(None, "same-password")
    assert first_hash is not None and second_hash is not None and first_hash != second_hash
