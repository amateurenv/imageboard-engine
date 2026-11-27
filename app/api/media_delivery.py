"""read-only выдача готовых derivative media через internal nginx"""

from fastapi import APIRouter, HTTPException, Response

from app.api.dependencies import MediaDeliveryServiceDependency, OptionalCurrentUserDependency

router = APIRouter(prefix="/media", tags=["media-delivery"])


@router.api_route("/{public_key}/{asset_kind}", methods=["GET", "HEAD"])
async def get_media_derivative(
    public_key: str,
    asset_kind: str,
    service: MediaDeliveryServiceDependency,
    current_user: OptionalCurrentUserDependency,
) -> Response:
    """проверить доступ в postgresql и передать nginx только internal путь"""

    delivery = await service.resolve(public_key, asset_kind, current_user)
    if delivery is None:
        # одинаковый 404 не раскрывает существование key, состояние или причину закрытия
        raise HTTPException(status_code=404, detail="media not found")
    return Response(
        status_code=200,
        media_type=delivery.content_type,
        headers={
            "X-Accel-Redirect": delivery.accel_path,
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            # opaque key неизменяем после attach, поэтому browser может кэшировать derivative
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
