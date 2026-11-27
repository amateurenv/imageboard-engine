"""выдача одноразовых прав подготовки media без upload маршрута"""

from fastapi import APIRouter, HTTPException

from app.api.dependencies import OptionalCurrentUserDependency, PostingClaimsServiceDependency
from app.schemas.posting_claims import PostingClaimCreateResponse
from app.services.media_policy import MediaPolicyError
from app.services.posting_claims import PostingClaimError
from app.services.posting_policy import PostingPermissionError

router = APIRouter(prefix="/boards", tags=["posting-claims"])


@router.post("/{slug}/posting-claims", response_model=PostingClaimCreateResponse, status_code=201)
async def create_posting_claim(
    slug: str,
    service: PostingClaimsServiceDependency,
    current_user: OptionalCurrentUserDependency,
) -> PostingClaimCreateResponse:
    """выдать token для одного будущего поста без создания файла"""

    try:
        token, claim = await service.create(slug, current_user)
    except PostingPermissionError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code.value, "message": str(exc)},
        ) from exc
    except MediaPolicyError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_detail()) from exc
    except PostingClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    return PostingClaimCreateResponse(
        token=token, expires_at=claim.expires_at, max_attachments=claim.max_attachments
    )
