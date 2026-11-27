"""контракты подготовки медиа без upload endpoint"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PostingClaimCreateResponse(BaseModel):
    """одноразовый token показывается только в этом ответе"""

    model_config = ConfigDict(extra="forbid")

    token: str
    expires_at: datetime
    max_attachments: int = Field(ge=1, le=20)
