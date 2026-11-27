"""правила состояний устойчивой очереди обработки media"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class MediaProcessingState(StrEnum):
    """состояние одной задачи подготовки media"""

    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class MediaTransitionError(ValueError):
    """переход состояния нарушает жизненный цикл задачи"""


@dataclass(frozen=True, slots=True)
class MediaQueueTask:
    """минимальные данные, нужные для проверки перехода"""

    state: MediaProcessingState
    attempts: int
    lease_until: datetime | None = None
    last_error: str | None = None


def ensure_media_transition(
    current: MediaProcessingState,
    target: MediaProcessingState,
) -> None:
    """разрешить только безопасные переходы очереди"""

    allowed = {
        MediaProcessingState.QUEUED: {MediaProcessingState.PROCESSING},
        MediaProcessingState.PROCESSING: {
            MediaProcessingState.QUEUED,
            MediaProcessingState.READY,
            MediaProcessingState.FAILED,
        },
        # failed возвращает оператор или служебный retry только в начало очереди
        MediaProcessingState.FAILED: {MediaProcessingState.QUEUED},
        MediaProcessingState.READY: set(),
    }
    if target not in allowed[current]:
        raise MediaTransitionError(f"{current} -> {target} is not allowed")


def claim_task(
    task: MediaQueueTask,
    *,
    now: datetime,
    lease_duration: timedelta,
    max_attempts: int,
) -> MediaQueueTask | None:
    """выдать queued задачу worker и увеличить число попыток"""

    if task.state is not MediaProcessingState.QUEUED:
        return None
    if task.attempts >= max_attempts:
        return None

    ensure_media_transition(task.state, MediaProcessingState.PROCESSING)
    return replace(
        task,
        state=MediaProcessingState.PROCESSING,
        attempts=task.attempts + 1,
        lease_until=now + lease_duration,
    )


def complete_task(task: MediaQueueTask) -> MediaQueueTask:
    """пометить успешно обработанный файл готовым"""

    ensure_media_transition(task.state, MediaProcessingState.READY)
    return replace(
        task,
        state=MediaProcessingState.READY,
        lease_until=None,
        last_error=None,
    )


def fail_task(
    task: MediaQueueTask,
    *,
    error: str,
    max_attempts: int,
) -> MediaQueueTask:
    """вернуть задачу в очередь или завершить после лимита попыток"""

    target = (
        MediaProcessingState.FAILED
        if task.attempts >= max_attempts
        else MediaProcessingState.QUEUED
    )
    ensure_media_transition(task.state, target)
    return replace(task, state=target, lease_until=None, last_error=error)


def recover_expired_task(
    task: MediaQueueTask,
    *,
    now: datetime,
    max_attempts: int,
) -> MediaQueueTask:
    """вернуть зависшую задачу из processing по завершившемуся lease"""

    if task.state is not MediaProcessingState.PROCESSING:
        return task
    if task.lease_until is None or task.lease_until > now:
        return task
    return fail_task(task, error="processing lease expired", max_attempts=max_attempts)


def retry_failed_task(task: MediaQueueTask) -> MediaQueueTask:
    """явно дать failed задаче новый полный лимит попыток"""

    ensure_media_transition(task.state, MediaProcessingState.QUEUED)
    return replace(
        task,
        state=MediaProcessingState.QUEUED,
        attempts=0,
        lease_until=None,
        last_error=None,
    )
