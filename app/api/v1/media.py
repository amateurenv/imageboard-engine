"""multipart upload для доски с действующим posting claim"""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.api.dependencies import (
    IpBansRepositoryDependency,
    MediaUploadServiceDependency,
    OptionalCurrentUserDependency,
    SpamServiceDependency,
    check_ip_bans,
    get_client_ip,
)
from app.api.media_ingress import MediaIngressRoute
from app.schemas.media import MediaUploadResponse
from app.services.media_policy import MediaPolicyError
from app.services.posting_claims import PostingClaimError
from app.services.posting_policy import PostingPermissionError

router = APIRouter(prefix="/boards", tags=["media"], route_class=MediaIngressRoute)


@router.post("/{slug}/media", response_model=MediaUploadResponse, status_code=201)
async def upload_image(
    request: Request,
    slug: str,
    file: Annotated[UploadFile, File()],
    service: MediaUploadServiceDependency,
    ip_bans_repository: IpBansRepositoryDependency,
    spam_service: SpamServiceDependency,
    current_user: OptionalCurrentUserDependency,
    posting_claim_token: Annotated[str | None, Form()] = None,
) -> MediaUploadResponse:
    """принять поддерживаемое вложение в закрытый quarantine"""

    await check_ip_bans(request, ip_bans_repository)
    try:
        await spam_service.check_media_upload_limit(get_client_ip(request), slug)
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        raise HTTPException(
            status_code=429,
            detail={"code": "media_rate_limited"},
        ) from exc
    try:
        if posting_claim_token is None:
            raise PostingClaimError("posting_claim_required")
        media = await service.upload(
            board_slug=slug,
            token=posting_claim_token,
            file=file,
            actor=current_user,
            content_length=request.headers.get("content-length"),
        )
    except PostingPermissionError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code.value, "message": str(exc)},
        ) from exc
    except MediaPolicyError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_detail()) from exc
    except PostingClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc

    return MediaUploadResponse(
        id=media.id,
        size=media.size,
        mime_type=media.mime_type,
        processing_state=media.processing_state.value,
        media_kind=media.media_kind,
        created_at=media.created_at,
    )
