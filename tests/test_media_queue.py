"""проверка правил устойчивой очереди media без внешних сервисов"""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.media_queue import (
    MediaProcessingState,
    MediaQueueTask,
    MediaTransitionError,
    claim_task,
    complete_task,
    fail_task,
    recover_expired_task,
    retry_failed_task,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3


def queued_task() -> MediaQueueTask:
    """создать новую задачу в начале жизненного цикла"""

    return MediaQueueTask(state=MediaProcessingState.QUEUED, attempts=0)


def test_queued_task_becomes_processing_then_ready() -> None:
    """успешная обработка освобождает lease и очищает старую ошибку"""

    processing = claim_task(
        queued_task(), now=NOW, lease_duration=LEASE, max_attempts=MAX_ATTEMPTS
    )
    assert processing is not None
    ready = complete_task(processing)

    assert ready.state is MediaProcessingState.READY
    assert ready.attempts == 1
    assert ready.lease_until is None
    assert ready.last_error is None


def test_processing_error_returns_task_to_queue_before_limit() -> None:
    """ошибка до лимита оставляет задачу доступной для следующего worker"""

    processing = claim_task(
        queued_task(), now=NOW, lease_duration=LEASE, max_attempts=MAX_ATTEMPTS
    )
    assert processing is not None
    queued = fail_task(processing, error="preview failed", max_attempts=MAX_ATTEMPTS)

    assert queued.state is MediaProcessingState.QUEUED
    assert queued.attempts == 1
    assert queued.last_error == "preview failed"


def test_processing_error_becomes_failed_after_attempt_limit() -> None:
    """лимит попыток останавливает бесконечный цикл повреждённого файла"""

    processing = MediaQueueTask(state=MediaProcessingState.PROCESSING, attempts=3)
    failed = fail_task(processing, error="preview failed", max_attempts=MAX_ATTEMPTS)

    assert failed.state is MediaProcessingState.FAILED
    assert failed.last_error == "preview failed"


def test_expired_processing_lease_is_recovered() -> None:
    """после падения worker незавершённая задача возвращается в очередь"""

    abandoned = MediaQueueTask(
        state=MediaProcessingState.PROCESSING,
        attempts=1,
        lease_until=NOW - timedelta(seconds=1),
    )
    recovered = recover_expired_task(abandoned, now=NOW, max_attempts=MAX_ATTEMPTS)

    assert recovered.state is MediaProcessingState.QUEUED
    assert recovered.last_error == "processing lease expired"


def test_second_worker_cannot_claim_already_processing_task() -> None:
    """повторный захват отклоняется до начала второй обработки"""

    processing = claim_task(
        queued_task(), now=NOW, lease_duration=LEASE, max_attempts=MAX_ATTEMPTS
    )
    assert processing is not None

    assert (
        claim_task(
            processing, now=NOW, lease_duration=LEASE, max_attempts=MAX_ATTEMPTS
        )
        is None
    )


def test_failed_task_can_be_explicitly_retried() -> None:
    """ручной retry начинает новый ограниченный цикл попыток"""

    failed = MediaQueueTask(
        state=MediaProcessingState.FAILED,
        attempts=MAX_ATTEMPTS,
        last_error="decoder failed",
    )
    queued = retry_failed_task(failed)

    assert queued.state is MediaProcessingState.QUEUED
    assert queued.attempts == 0
    assert queued.last_error is None


def test_ready_task_has_no_outgoing_transition() -> None:
    """готовый файл нельзя случайно вернуть в обработку"""

    with pytest.raises(MediaTransitionError):
        complete_task(MediaQueueTask(state=MediaProcessingState.READY, attempts=1))
