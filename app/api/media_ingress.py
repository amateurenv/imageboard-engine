"""допуск multipart запросов до создания временных файлов"""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import ExitStack
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message

from app.services.media_quota import DISK_FULL, MediaQuota, MediaStorageFull


def storage_error(error: BaseException) -> bool:
    """найти отказ диска, в том числе обёрнутый multipart парсером"""

    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, MediaStorageFull) or (
            isinstance(cause, OSError) and cause.errno in DISK_FULL
        ):
            return True
        cause = cause.__cause__
    return False


def _check_body_size(size: int, limit: int) -> None:
    if size > limit:
        raise HTTPException(413, detail={
            "code": "media_file_too_large",
            "message": "размер multipart-запроса больше эксплуатационного лимита",
            "limit_bytes": limit,
        })


class MediaIngressRoute(APIRoute):
    """удерживать слот до закрытия multipart файлов"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            quota: MediaQuota | None = getattr(request.app.state, "media_quota", None)
            if quota is None:
                return await handler(request)
            try:
                settings = quota.settings
                try:
                    declared = int(request.headers.get("content-length", "0"))
                except ValueError:
                    declared = 0
                _check_body_size(declared, settings.media_upload_body_bytes)
                await quota.check_inventory()
                if quota.free_bytes() < (
                    settings.media_storage_min_free_bytes + settings.media_ingress_budget
                ):
                    raise MediaStorageFull("multipart free space reserve reached")
                with ExitStack() as slot:
                    for index in range(settings.media_upload_concurrency):
                        if slot.enter_context(quota.storage.guard(f"ingress:{index}")):
                            break
                        slot.close()
                    else:
                        raise MediaStorageFull("all multipart slots are occupied")
                    deadline = (
                        asyncio.get_running_loop().time() + settings.media_upload_timeout_seconds
                    )
                    received = 0

                    async def receive() -> Message:
                        nonlocal received
                        try:
                            async with asyncio.timeout_at(deadline):
                                message = await request.receive()
                        except TimeoutError as error:
                            raise HTTPException(
                                408, detail={"code": "media_upload_timeout"},
                            ) from error
                        received += len(message.get("body", b""))
                        _check_body_size(received, settings.media_upload_body_bytes)
                        return message

                    wrapped = Request(request.scope, receive)
                    try:
                        return await handler(wrapped)
                    finally:
                        await wrapped.close()
            except Exception as error:
                if storage_error(error):
                    raise HTTPException(503, detail={"code": "media_storage_full"}) from error
                raise

        return guarded
