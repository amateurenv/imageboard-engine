"""схемы команд и внутреннего уведомления realtime"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

type Scope = Annotated[
    str, StringConstraints(pattern=r"^(board:[a-z0-9][a-z0-9_-]{0,62}|thread:[1-9][0-9]{0,18})$")
]
type RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]


class SubscriptionCommand(BaseModel):
    """команда подписки по slug доски или внутреннему id треда"""

    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["subscribe", "unsubscribe"]
    scope: Scope


class PongCommand(BaseModel):
    """ответ клиента на текущий ping"""

    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["pong"]
    nonce: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]


COMMAND = TypeAdapter(Annotated[SubscriptionCommand | PongCommand, Field(discriminator="type")])


class RealtimeEvent(BaseModel):
    """минимальное внутреннее уведомление об изменении или отзыве доступа"""

    model_config = ConfigDict(extra="forbid", strict=True)
    board_id: RecordId
    thread_id: RecordId | None
    unavailable: bool
