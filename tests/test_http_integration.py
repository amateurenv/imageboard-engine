"""сквозная проверка http api с настоящими postgresql и redis"""

import asyncio
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import asyncpg
import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response, TransportError
from PIL import Image
from redis.asyncio import Redis
from redis.exceptions import RedisError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.api.dependencies import (
    get_postgres_pool,
    get_redis_client,
    get_site_settings,
)
from app.api.system import router as system_router
from app.api.v1.ws import router as ws_router
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE, SESSION_COOKIE, hash_password, hash_token
from app.core.site_settings import SiteSettings
from app.main import app
from app.repositories.media import MediaRepository
from app.repositories.posting_claims import PostingClaimsRepository
from app.repositories.users import UsersRepository
from app.services.media_policy import MEBIBYTE, MediaPolicyError, MediaType
from app.services.media_quota import MediaQuota, MediaStorageFull, reserved_bytes, work_budget
from app.services.media_storage import MediaStorage
from app.services.media_upload import MediaUploadService
from app.services.posting_claims import (
    PostingClaimError,
    PostingClaimsService,
    StickerCannotStartThread,
)
from app.services.realtime import publish_pending_events
from app.services.sticker_processing import MAX_STICKER_BYTES, STICKER_REPLY_WARNING
from app.services.tgs_processing import TGS_MIME, canonicalize_tgs
from app.services.video_processing import VideoProcessor
from app.services.video_sticker_processing import WEBM_STICKER_MIME
from app.ws.manager import WebSocketManager
from scripts.cleanup_media_files import cleanup_media_files
from scripts.inventory_media_storage import inventory_media_storage
from scripts.media_worker import MAX_PROCESSING_ATTEMPTS, PROCESSING_LEASE, process_job
from scripts.monitor_media import snapshot, warnings

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def media_files(storage: MediaStorage) -> list[Path]:
    """вернуть файлы загрузок и производных без служебных блокировок"""

    return [
        path for path in storage.root.rglob("*")
        if path.is_file()
        and path.relative_to(storage.root).parts[0] not in {".locks", ".volume-id"}
    ]


@asynccontextmanager
async def nginx_client(
    nginx: str, root: Path, tmp_path: Path, *,
    application: FastAPI = app, uvicorn_env: dict[str, str] | None = None,
):
    """пропустить запрос через реальные internal locations шаблона nginx"""

    with socket.socket() as upstream, socket.socket() as address:
        upstream.bind(("127.0.0.1", 0))
        upstream.listen()
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
        address.close()

        template = (PROJECT_ROOT / "deploy/nginx.conf.example").read_text(encoding="utf-8")
        lines = [
            line for line in template.splitlines()
            if not line.strip().startswith(("ssl_certificate", "server_name"))
        ]
        server_config = "\n".join(lines).replace(
            "listen 443 ssl http2;", f"listen 127.0.0.1:{port};",
        ).replace("/srv/imageboard-media", str(root)).replace(
            "127.0.0.1:8000", f"127.0.0.1:{upstream.getsockname()[1]}",
        )
        config = tmp_path / "nginx.conf"
        config.write_text(
            "daemon off;\npid nginx.pid;\nerror_log stderr;\nevents {}\nhttp {\n"
            "access_log off;\n"
            "client_body_temp_path body;\nproxy_temp_path proxy;\nfastcgi_temp_path fastcgi;\n"
            "uwsgi_temp_path uwsgi;\nscgi_temp_path scgi;\n"
            f"{server_config}\n}}\n", encoding="utf-8",
        )
        server = None
        task = None
        workers = None
        process = None
        try:
            if uvicorn_env is None:
                server = uvicorn.Server(uvicorn.Config(
                    application, lifespan="off", log_level="warning",
                    ws="websockets", ws_max_size=2048, ws_max_queue=4, ws_per_message_deflate=False,
                ))
                task = asyncio.create_task(server.serve(sockets=[upstream]))
            else:
                upstream_port = upstream.getsockname()[1]
                upstream.close()
                workers = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "uvicorn", "app.main:app",
                    "--host", "127.0.0.1", "--port", str(upstream_port), "--workers", "2",
                    "--ws", "websockets", "--ws-max-size", "2048", "--ws-max-queue", "4",
                    "--ws-per-message-deflate", "false", "--log-level", "warning",
                    env=uvicorn_env,
                )
            process = await asyncio.create_subprocess_exec(
                nginx, "-p", str(tmp_path) + "/", "-c", str(config),
            )
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                for _ in range(100):
                    if process.returncode is not None:
                        pytest.fail(f"nginx exited with code {process.returncode}")
                    if workers is not None and workers.returncode is not None:
                        pytest.fail(f"uvicorn exited with code {workers.returncode}")
                    try:
                        if (await client.get("/health", timeout=1)).status_code == 200:
                            break
                    except TransportError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("nginx did not start")
                yield client
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            if server is not None and task is not None:
                server.should_exit = True
                await task
            if workers is not None and workers.returncode is None:
                workers.terminate()
                await asyncio.wait_for(workers.wait(), 10)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("extension", "http_client"),
    [("webp", 23), ("tgs", 23), ("webm", 24), ("webm-nginx", 24)],
    ids=["webp-upgrade", "tgs-upgrade", "webm", "webm-nginx"], indirect=["http_client"],
)
async def test_internal_sticker_publication_visibility_and_cleanup(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
    tgs_payload: bytes, extension: str, request: pytest.FixtureRequest, tmp_path: Path,
) -> None:
    """проверить одинаковые правила публикации и выдачу производных стикера"""

    nginx = None
    if extension == "webm-nginx":
        nginx = shutil.which("nginx")
        if nginx is None:
            pytest.skip("Nginx is required")
        extension = "webm"
    animated = extension != "webp"
    pool = app.state.postgres_pool
    settings = app.state.site_settings
    users = UsersRepository(pool)
    author = await users.create_profile_user("alice", hash_password("profile-password"))
    other = await users.create_profile_user("bob", hash_password("other-password"))
    csrf = await login(http_client, "alice", "profile-password")
    thread = await http_client.post("/api/v1/boards/alice/threads", json={"body": "opening"})
    assert thread.status_code == 201, thread.text
    thread_id = thread.json()["thread"]["id"]
    other_thread = await http_client.post("/api/v1/boards/alice/threads", json={"body": "other"})
    assert other_thread.status_code == 201
    other_thread_id = other_thread.json()["thread"]["id"]
    reply_url = f"/api/v1/threads/{thread_id}/posts"
    media_url = "/api/v1/boards/alice/media"
    moderation_url = f"/api/v1/profiles/alice/threads/{thread_id}"

    image = BytesIO()
    Image.new("RGBA", (512, 128), (20, 90, 150, 67)).save(image, "WEBP")
    payload, mime = image.getvalue(), "image/webp"
    if extension == "tgs":
        payload, mime = tgs_payload, TGS_MIME
    elif extension == "webm":
        payload, mime = request.getfixturevalue("webm_payload"), WEBM_STICKER_MIME
    filename = f"sticker.{extension}"
    storage = MediaStorage(app.state.media_storage_root)
    upload = MediaUploadService(pool, app.state.redis_client, settings, storage.root)
    posting = PostingClaimsService(pool, settings)
    repository = MediaRepository(pool)

    with pytest.raises(StickerCannotStartThread) as rejected:
        await upload.stage_sticker(thread_id=None, actor=author, data=payload)
    assert rejected.value.code == "sticker_cannot_start_thread"
    assert str(rejected.value) == STICKER_REPLY_WARNING
    with pytest.raises(StickerCannotStartThread):
        await posting.publish_sticker(thread_id=None, actor=author, token="", media_id=0)
    with pytest.raises(MediaPolicyError) as invalid:
        await upload.stage_sticker(thread_id=thread_id, actor=author, data=image_bytes("WEBP"))
    assert invalid.value.code.value == "invalid_media"
    if extension == "webm":
        with pytest.raises(MediaPolicyError) as invalid:
            await upload.stage_sticker(thread_id=thread_id, actor=author, data=payload[:-1])
        assert invalid.value.code.value == "invalid_media"
        assert await pool.fetchval("SELECT count(*) FROM posting_claims") == 0
        assert await pool.fetchval("SELECT count(*) FROM media") == 0
        assert not media_files(storage)

    token, queued = await upload.stage_sticker(thread_id=thread_id, actor=author, data=payload)
    assert queued.media_kind == "sticker" and queued.processing_state.value == "queued"
    assert queued.is_animation is animated and queued.mime_type == mime
    with pytest.raises(PostingClaimError, match="posting_claim_media_invalid"):
        await posting.publish_sticker(
            thread_id=thread_id, actor=author, token=token, media_id=queued.id,
        )

    claimed = await repository.claim_by_id(
        queued.id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    await process_job(repository, queued.id, storage, expected_lease=claimed.processing_lease_until)
    ready = await repository.get_by_id(queued.id)
    assert ready is not None and ready.processing_state.value == "ready"
    if extension in {"webp", "tgs"}:
        # обновляем схему, когда производные старого формата уже готовы к публикации
        migration = PROJECT_ROOT / "migrations/024_video_stickers.sql"
        await pool.execute(migration.read_text(encoding="utf-8"))
        assert await repository.get_by_id(queued.id) == ready
    if animated:
        assert ready.preview_path == f"previews/{queued.id}.webp" and ready.display_path is None
        assert ready.canonical_path == f"stickers/{queued.id}.{extension}"
        assert ready.duration_seconds == 2 and ready.canonical_mime_type == mime
        assert ready.channels is None and ready.sample_rate is None
        canonical = storage.resolve(ready.canonical_path)
        if extension == "tgs":
            assert canonical.read_bytes() == canonicalize_tgs(payload)[1]
        else:
            assert canonical.read_bytes() != payload and canonical.stat().st_size <= 256 * 1024
        webp = storage.resolve(ready.preview_path)
    else:
        assert ready.display_path == f"display/{queued.id}.webp" and ready.preview_path is None
        webp = canonical = storage.resolve(ready.display_path)
    with Image.open(webp) as rendered:
        assert rendered.format == "WEBP" and rendered.n_frames == 1
        assert rendered.size == ((512, 512) if extension == "tgs" else (512, 128))
        if extension == "tgs":
            assert rendered.getpixel((1, 1))[3] == 0
            red, green, blue, alpha = rendered.getpixel((256, 256))
            assert red > 230 and green < 15 and blue < 15 and 126 <= alpha <= 128
        elif extension == "webm":
            assert rendered.getpixel((1, 1))[3] == 0
            assert 120 <= rendered.getpixel((60, 50))[3] <= 130
        else:
            assert rendered.getpixel((1, 1))[3] == 67
        assert webp.stat().st_size <= MAX_STICKER_BYTES
    assert await cleanup_media_files(repository=repository, storage=storage) == 1
    assert not storage.resolve(queued.path).exists()

    params = {"body": "caption", "posting_claim_token": token, "media_ids": [queued.id]}
    op = await http_client.post("/api/v1/boards/alice/threads", json=params)
    assert op.status_code == 422 and op.json()["detail"]["code"] == "sticker_cannot_start_thread"
    blocked = await http_client.post(reply_url, json=params)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "posting_claim_media_invalid"
    public_upload = await http_client.post(
        media_url, data={"posting_claim_token": token, "media_kind": "sticker"},
        files={"file": (filename, payload, mime)},
    )
    assert public_upload.status_code == 403

    claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    normal_token = claim.json()["token"]
    ordinary = await http_client.post(
        media_url, data={"posting_claim_token": normal_token, "media_kind": "sticker"},
        files={"file": (filename, payload, mime)},
    )
    if extension == "webm":
        assert ordinary.status_code == 415
        assert ordinary.json()["detail"]["code"] == "unsupported_media_type"
    elif animated:
        assert ordinary.status_code in {415, 422} and ordinary.json()["detail"]["code"] in {
            "unsupported_media_type", "invalid_media",
        }
    else:
        assert ordinary.status_code == 201 and ordinary.json()["media_kind"] == "image"
    with pytest.raises(PostingClaimError, match="posting_claim_forbidden"):
        await posting.publish_sticker(
            thread_id=thread_id, actor=other, token=token, media_id=queued.id,
        )
    with pytest.raises(PostingClaimError, match="posting_claim_media_invalid"):
        await posting.publish_sticker(
            thread_id=other_thread_id, actor=author, token=token, media_id=queued.id,
        )
    locked = await http_client.patch(moderation_url, headers=csrf, json={"is_locked": True})
    assert locked.status_code == 200
    with pytest.raises(PostingClaimError, match="sticker_thread_unavailable"):
        await posting.publish_sticker(
            thread_id=thread_id, actor=author, token=token, media_id=queued.id,
        )
    unlocked = await http_client.patch(moderation_url, headers=csrf, json={"is_locked": False})
    assert unlocked.status_code == 200

    async def publish():
        return await posting.publish_sticker(
            thread_id=thread_id, actor=author, token=token, media_id=queued.id,
        )

    attach_and_consume = PostingClaimsRepository.attach_and_consume

    async def fail_after_attach(self, connection, claim_id, post_id, media_ids, public_keys):
        await attach_and_consume(self, connection, claim_id, post_id, media_ids, public_keys)
        raise RuntimeError("simulated publication failure")

    # откат после attach должен вернуть claim, media и тред в прежнее состояние
    read_url = f"/api/v1/threads/{thread_id}"
    before = (await http_client.get(read_url)).json()
    with monkeypatch.context() as patch:
        patch.setattr(PostingClaimsRepository, "attach_and_consume", fail_after_attach)
        with pytest.raises(RuntimeError, match="simulated publication failure"):
            await publish()
    assert (await http_client.get(read_url)).json() == before
    rolled_back = await pool.fetchrow(
        """SELECT media.post_id, media.public_key, claim.consumed_at
           FROM media JOIN posting_claims AS claim ON claim.id=media.posting_claim_id
           WHERE media.id=$1""",
        queued.id,
    )
    assert rolled_back is not None and tuple(rolled_back) == (None, None, None)

    first, second = await asyncio.gather(publish(), publish(), return_exceptions=True)
    failures = [result for result in (first, second) if isinstance(result, PostingClaimError)]
    assert len(failures) == 1 and failures[0].code == "posting_claim_invalid"
    page = await http_client.get(read_url)
    replies = page.json()["replies"]
    assert len(replies) == 1 and replies[0]["body"] == ""
    (item,) = replies[0]["media"]
    assert item["media_kind"] == "sticker" and item["is_animation"] is animated
    assert item["mime_type"] == mime
    prefix = "/_imageboard_media"
    if animated:
        assert item["display_url"] is None and item["duration_seconds"] == 2
        assets = {
            "content": (mime, f"{prefix}/stickers/{queued.id}.{extension}"),
            "preview": ("image/webp", f"{prefix}/sticker-previews/{queued.id}.webp"),
        }
        unavailable = ("display",)
    else:
        assert item["preview_url"] is None and item["content_url"] is None
        assets = {"display": ("image/webp", f"{prefix}/stickers/{queued.id}.webp")}
        unavailable = ("content", "preview")
    for kind, (content_type, internal_url) in assets.items():
        for method in (http_client.get, http_client.head):
            delivered = await method(item[f"{kind}_url"])
            assert delivered.status_code == 200 and delivered.content == b""
            assert delivered.headers["content-type"] == content_type
            assert delivered.headers["x-accel-redirect"] == internal_url
            assert delivered.headers["cache-control"] == "no-store"
            assert delivered.headers["x-content-type-options"] == "nosniff"
    base_url = item[f"{next(iter(assets))}_url"].rsplit("/", 1)[0]
    for kind in unavailable:
        assert (await http_client.get(f"{base_url}/{kind}")).status_code == 404
    if nginx is not None:
        async with nginx_client(nginx, storage.root, tmp_path) as proxy:
            for kind, file in (("content", canonical), ("preview", webp)):
                url = item[f"{kind}_url"]
                for method in ("GET", "HEAD"):
                    response = await proxy.request(method, url)
                    assert response.status_code == 200
                    assert response.content == (file.read_bytes() if method == "GET" else b"")
                    assert response.headers["content-type"] == assets[kind][0]
                    assert response.headers["cache-control"] == "no-store"
                    assert response.headers["x-content-type-options"] == "nosniff"
                assert (await proxy.get(assets[kind][1])).status_code == 404
            partial = await proxy.get(item["content_url"], headers={"Range": "bytes=0-15"})
            assert partial.status_code == 206 and partial.content == canonical.read_bytes()[:16]
            assert partial.headers["content-range"] == f"bytes 0-15/{canonical.stat().st_size}"
            assert partial.headers["cache-control"] == "no-store"
            invalid_range = await proxy.get(item["content_url"], headers={"Range": "bytes=999999-"})
            assert invalid_range.status_code == 416
            hidden = await http_client.patch(moderation_url, headers=csrf, json={"is_hidden": True})
            assert hidden.status_code == 200
            for kind in assets:
                assert (await proxy.get(item[f"{kind}_url"])).status_code == 404
            assert (await http_client.delete(moderation_url, headers=csrf)).status_code == 204
            for kind in assets:
                assert (await proxy.head(item[f"{kind}_url"])).status_code == 404
    else:
        hidden = await http_client.patch(moderation_url, headers=csrf, json={"is_hidden": True})
        assert hidden.status_code == 200
    for kind in assets:
        assert (await http_client.get(item[f"{kind}_url"])).status_code == 404
    if nginx is None:
        assert (await http_client.delete(moderation_url, headers=csrf)).status_code == 204
    assert await cleanup_media_files(repository=repository, storage=storage) == len(assets)
    assert not canonical.exists() and not webp.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("extension", ["webp", "tgs", "webm"])
async def test_sticker_worker_replace_failure_keeps_quarantine_until_claim_expires(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
    tgs_payload: bytes, extension: str, request: pytest.FixtureRequest,
) -> None:
    """проверить откат готовности при файловой ошибке и очистку истёкшего claim"""

    pool = app.state.postgres_pool
    actor = await UsersRepository(pool).create_profile_user(
        "alice", hash_password("profile-password"),
    )
    await login(http_client, "alice", "profile-password")
    thread = await http_client.post("/api/v1/boards/alice/threads", json={"body": "opening"})
    assert thread.status_code == 201
    storage = MediaStorage(app.state.media_storage_root)
    upload = MediaUploadService(pool, app.state.redis_client, app.state.site_settings, storage.root)
    image = BytesIO()
    Image.new("RGBA", (240, 512), (0, 0, 0, 0)).save(image, "WEBP", lossless=True)
    payload = tgs_payload if extension == "tgs" else image.getvalue()
    if extension == "webm":
        payload = request.getfixturevalue("webm_payload")
    _, queued = await upload.stage_sticker(
        thread_id=thread.json()["thread"]["id"], actor=actor,
        data=payload,
    )
    repository = MediaRepository(pool)
    claimed = await repository.claim_by_id(
        queued.id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None

    replace = storage.replace_from_temporary

    def reject_replace(temporary: Path, relative_path: str) -> Path:
        if relative_path.startswith("stickers/"):
            return replace(temporary, relative_path)
        raise OSError("simulated storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "replace_from_temporary", reject_replace)
        with pytest.raises(OSError, match="simulated storage failure"):
            await process_job(repository, queued.id, storage)

    media = await repository.get_by_id(queued.id)
    assert media is not None and media.processing_state.value == "processing"
    assert (media.display_path, media.canonical_path, media.preview_path) == (None, None, None)
    assert media_files(storage) == [storage.resolve(media.path)]
    assert await cleanup_media_files(repository=repository, storage=storage) == 0
    animated = extension != "webp"
    path = f"stickers/{queued.id}.{extension}" if animated else f"display/{queued.id}.webp"
    if extension == "webm":
        # имитируем остановку worker после replace и до ready commit
        derivatives = [path, f"previews/{queued.id}.webp"]
        await pool.execute(
            "UPDATE media_disk_files SET work_paths=$1 WHERE path=$2", derivatives, media.path,
        )
        for derivative in derivatives:
            storage.prepare_parent(derivative).write_bytes(b"interrupted worker")
    else:
        await process_job(
            repository, queued.id, storage, expected_lease=claimed.processing_lease_until,
        )
    assert storage.resolve(path).is_file()

    await pool.execute(
        "UPDATE posting_claims SET expires_at = clock_timestamp() WHERE id = $1",
        queued.posting_claim_id,
    )
    assert await PostingClaimsRepository(pool).cleanup_expired() == 1
    assert await repository.get_by_id(queued.id) is None
    cleanup_count = await cleanup_media_files(repository=repository, storage=storage)
    assert cleanup_count == (3 if animated else 2)
    assert not media_files(storage)


@pytest.fixture
async def http_client(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> AsyncClient:
    """дать api отдельные postgres-схему и redis database"""

    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_REDIS_URL are required")

    schema = f"test_{uuid.uuid4().hex}"
    connection = await asyncpg.connect(database_url)
    redis = Redis.from_url(redis_url, decode_responses=True)
    await redis.ping()
    # тестовая redis database выделена только этому набору, убираем хвост прошлого прогона
    stale_keys = [key async for key in redis.scan_iter(match="spam:*")]
    if stale_keys:
        await redis.delete(*stale_keys)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    requested_schema = getattr(request, "param", None)
    before_quota = requested_schema == "before-quota"
    max_migration = 24 if before_quota else (
        requested_schema if isinstance(requested_schema, int) else None
    )
    for migration in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
        number = int(migration.name.split("_", 1)[0])
        if max_migration is not None and number > max_migration and (number != 25 or before_quota):
            continue
        await connection.execute(migration.read_text(encoding="utf-8"))

    pool = await asyncpg.create_pool(database_url, server_settings={"search_path": schema})
    settings = SiteSettings.model_validate(
        {
            "thread": {"max_threads_per_board_day": 100},
            "post": {"max_posts_per_thread_hour": 100},
        }
    )
    app.state.postgres_pool = pool
    app.state.redis_client = redis
    app.state.site_settings = settings
    test_mount = os.getenv("TEST_MEDIA_QUOTA_MOUNT") if requested_schema == "small-volume" else None
    app.state.media_storage_root = Path(test_mount) if test_mount else tmp_path / "media"
    storage = MediaStorage(app.state.media_storage_root)
    monkeypatch.setattr(app.state, "media_quota", MediaQuota(pool, storage), raising=False)
    if not before_quota:
        await inventory_media_storage(app.state.media_quota)
    monkeypatch.setattr(tempfile, "tempdir", str(storage.prepare_parent(".spool/ready").parent))
    app.dependency_overrides = {
        get_postgres_pool: lambda: pool,
        get_redis_client: lambda: redis,
        get_site_settings: lambda: settings,
    }

    users = UsersRepository(pool)
    owner = await users.create_user("owner", hash_password("owner-password"), "owner")
    await users.create_user("moderator", hash_password("moderator-password"), "moderator")
    assert owner.role == "owner"

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        await pool.close()
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
        stale_keys = [key async for key in redis.scan_iter(match="spam:*")]
        if stale_keys:
            await redis.delete(*stale_keys)
        await redis.aclose()


async def login(client: AsyncClient, login_name: str, password: str) -> dict[str, str]:
    """войти и вернуть заголовок csrf для изменяющих запросов"""

    response = await client.post(
        "/api/v1/auth/login",
        json={"login": login_name, "password": password},
    )
    assert response.status_code == 200
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token is not None
    return {"x-csrf-token": csrf_token}


WS_ORIGIN = "https://board.test"


@asynccontextmanager
async def realtime_application(redis: Redis):
    """поднять маршрут и relay на выделенном redis для перезапуска"""

    channel = f"test:realtime:{uuid.uuid4().hex}"
    pool = app.state.postgres_pool
    manager = WebSocketManager(redis, channel)
    relay = asyncio.create_task(publish_pending_events(pool, redis, channel))
    listener = asyncio.create_task(manager.consume_redis())
    settings = get_settings().model_copy(update={
        "realtime_enabled": True,
        "realtime_allowed_origins": WS_ORIGIN,
    })
    try:
        await asyncio.wait_for(manager.ready.wait(), 8)
        application = FastAPI()
        application.state.postgres_pool = pool
        application.state.ws_manager = manager
        application.dependency_overrides[get_settings] = lambda: settings
        application.include_router(system_router)
        application.include_router(ws_router, prefix="/api/v1")
        yield application, manager
    finally:
        relay.cancel()
        listener.cancel()
        await asyncio.gather(relay, listener, return_exceptions=True)


async def receive_realtime(client: ClientConnection) -> dict:
    """прочитать уведомление с ограниченным ожиданием"""

    return json.loads(await asyncio.wait_for(client.recv(), 8))


async def subscribe_realtime(client: ClientConnection, scope: str) -> dict:
    """передать команду подписки и дождаться ответа"""

    await client.send(json.dumps({"type": "subscribe", "scope": scope}))
    return await receive_realtime(client)


@pytest.mark.anyio
async def test_realtime_visibility_and_fanout_through_nginx(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """проверить origin, сессию, изоляцию и рассылку при параллельном чтении"""

    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("Nginx is required")

    await register(http_client, "alice")
    csrf = await login(http_client, "owner", "owner-password")
    assert (await http_client.post(
        "/api/v1/admin/boards", headers=csrf, json={"slug": "other", "title": "Other"},
    )).status_code == 200
    session = http_client.cookies[SESSION_COOKIE]
    pool = app.state.postgres_pool
    schema = await pool.fetchval("SELECT current_schema()")
    database = urlsplit(os.environ["TEST_DATABASE_URL"])
    query = "&".join(filter(None, (database.query, urlencode({"search_path": schema}))))
    channel = f"test:realtime:{uuid.uuid4().hex}"
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": database._replace(query=query).geturl(),
        "REDIS_URL": os.environ["TEST_REDIS_URL"],
        "MEDIA_STORAGE_ROOT": str(app.state.media_storage_root),
        "REALTIME_ENABLED": "true",
        "REALTIME_ALLOWED_ORIGINS": WS_ORIGIN,
        "REALTIME_CHANNEL": channel,
    }
    async with (
        nginx_client(
            nginx, app.state.media_storage_root, tmp_path, uvicorn_env=env
        ) as proxy,
        AsyncExitStack() as clients,
    ):
        async with asyncio.timeout(8):
            while dict(await app.state.redis_client.pubsub_numsub(channel))[channel] < 2:
                await asyncio.sleep(0.05)
        endpoint = str(proxy.base_url).replace("http:", "ws:") + "/api/v1/ws"
        for headers in (
            {"origin": "https://foreign.test"},
            {"additional_headers": {"Cookie": f"{SESSION_COOKIE}={session}"}},
            {"origin": WS_ORIGIN, "additional_headers": {"Cookie": f"{SESSION_COOKIE}=bad"}},
        ):
            with pytest.raises(InvalidStatus) as rejected:
                async with connect(endpoint, **headers):
                    pytest.fail("unexpected websocket upgrade")
            assert rejected.value.response.status_code == 403

        sockets = await asyncio.gather(*(
            clients.enter_async_context(connect(endpoint, origin=WS_ORIGIN)) for _ in range(24)
        ))
        scope = "board:alice"
        registered = await asyncio.gather(*(subscribe_realtime(ws, scope) for ws in sockets))
        assert registered == [{"type": "subscribed", "scope": scope}] * len(sockets)

        isolated = await clients.enter_async_context(connect(endpoint, origin=WS_ORIGIN))
        assert await subscribe_realtime(isolated, "board:other") == {
            "type": "subscribed", "scope": "board:other",
        }
        assert await subscribe_realtime(isolated, "thread:999999") == {
            "type": "error", "detail": "subscription rejected",
        }

        created = await http_client.post("/api/v1/boards/alice/threads", json={"body": "op"})
        assert created.status_code == 201
        thread_id = created.json()["thread"]["id"]
        path = f"/api/v1/threads/{thread_id}"
        thread_scope = f"thread:{thread_id}"
        reading = asyncio.gather(*(proxy.get(path, timeout=3) for _ in range(20)))
        board_changed = {"type": "changed", "scope": scope}
        assert await asyncio.gather(*(receive_realtime(ws) for ws in sockets)) == [
            board_changed
        ] * len(sockets)
        assert all(response.status_code == 200 for response in await reading)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(isolated.recv(), 0.1)

        thread_client = await clients.enter_async_context(connect(
            endpoint, origin=WS_ORIGIN,
            additional_headers={"Cookie": f"{SESSION_COOKIE}={session}"},
        ))
        assert await subscribe_realtime(thread_client, thread_scope) == {
            "type": "subscribed", "scope": thread_scope,
        }
        changed = {"type": "changed", "scope": thread_scope}
        reply = await http_client.post(f"{path}/posts", json={"body": "private-to-frame-test"})
        assert reply.status_code == 200
        post_id = reply.json()["id"]
        assert await receive_realtime(thread_client) == changed
        for hidden in (True, False):
            assert (await http_client.patch(
                f"/api/v1/profiles/alice/posts/{post_id}",
                headers=csrf, json={"is_hidden": hidden},
            )).status_code == 200
            assert await receive_realtime(thread_client) == changed
            replies = (await http_client.get(path)).json()["replies"]
            assert bool(replies) is not hidden
        assert (await http_client.request(
            "DELETE", f"/api/v1/posts/{post_id}", headers=csrf, json={},
        )).status_code == 204
        assert await receive_realtime(thread_client) == changed

        for value in (True, False):
            assert (await http_client.patch(
                f"/api/v1/admin/threads/{thread_id}",
                headers=csrf, json={"is_sticky": value, "is_locked": value},
            )).status_code == 200
            assert await receive_realtime(thread_client) == changed

        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            await connection.execute("UPDATE threads SET is_locked = true WHERE id = $1", thread_id)
            await transaction.rollback()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(thread_client.recv(), 0.8)

        moderate = f"/api/v1/profiles/alice/threads/{thread_id}"
        unavailable = {"type": "unavailable", "scope": thread_scope}
        assert (await http_client.patch(
            moderate, headers=csrf, json={"is_hidden": True}
        )).is_success
        assert await receive_realtime(thread_client) == unavailable
        assert (await http_client.get(path)).status_code == 404
        assert await subscribe_realtime(thread_client, thread_scope) == {
            "type": "error", "detail": "subscription rejected",
        }
        assert (await http_client.patch(
            moderate, headers=csrf, json={"is_hidden": False}
        )).is_success
        assert await subscribe_realtime(thread_client, thread_scope) == {
            "type": "subscribed", "scope": thread_scope,
        }
        assert (await http_client.delete(
            f"/api/v1/admin/threads/{thread_id}", headers=csrf
        )).is_success
        # поздняя доставка unhide также допустима, состояние всегда перечитывается из rest
        message = await receive_realtime(thread_client)
        if message == changed:
            message = await receive_realtime(thread_client)
        assert message == unavailable
        assert (await http_client.get(path)).status_code == 404

        assert (await http_client.patch(
            "/api/v1/admin/boards/other", headers=csrf, json={"is_active": False},
        )).status_code == 200
        assert await receive_realtime(isolated) == {"type": "unavailable", "scope": "board:other"}
        assert await subscribe_realtime(isolated, "board:other") == {
            "type": "error", "detail": "subscription rejected",
        }


@asynccontextmanager
async def isolated_redis(server: str, unix_socket: Path, redis: Redis):
    """запустить выделенный redis, который можно остановить для проверки восстановления"""

    process = await asyncio.create_subprocess_exec(
        server, "--port", "0", "--unixsocket", str(unix_socket), "--save", "", "--appendonly", "no",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.returncode is not None:
                pytest.fail("isolated redis failed to start")
            try:
                if await redis.ping():
                    break
            except RedisError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("isolated redis did not start")
        yield
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)


@pytest.mark.anyio
async def test_realtime_survives_redis_restart(http_client: AsyncClient, tmp_path: Path) -> None:
    """проверить разрыв при сбое, сохранённую очередь и rest после переподключения"""

    server = shutil.which("redis-server")
    nginx = shutil.which("nginx")
    if server is None or nginx is None:
        pytest.skip("redis-server and Nginx are required")
    csrf = await login(http_client, "owner", "owner-password")
    assert (await http_client.post(
        "/api/v1/admin/boards", headers=csrf, json={"slug": "recovery", "title": "Recovery"},
    )).is_success
    created = await http_client.post("/api/v1/boards/recovery/threads", json={"body": "op"})
    path = f"/api/v1/threads/{created.json()['thread']['id']}"
    patch = f"/api/v1/admin/threads/{created.json()['thread']['id']}"
    pool = app.state.postgres_pool
    socket_path = tmp_path / "r.sock"
    redis = Redis(unix_socket_path=str(socket_path), decode_responses=True)
    async with redis, AsyncExitStack() as runtime:
        broker = AsyncExitStack()
        async with broker:
            await broker.enter_async_context(isolated_redis(server, socket_path, redis))
            application, manager = await runtime.enter_async_context(realtime_application(redis))
            proxy = await runtime.enter_async_context(nginx_client(
                nginx, app.state.media_storage_root, tmp_path, application=application,
            ))
            endpoint = str(proxy.base_url).replace("http:", "ws:") + "/api/v1/ws"
            client = await runtime.enter_async_context(connect(endpoint, origin=WS_ORIGIN))
            scope = f"thread:{created.json()['thread']['id']}"
            assert await subscribe_realtime(client, scope) == {"type": "subscribed", "scope": scope}

        with pytest.raises(ConnectionClosed) as closed:
            while True:
                await receive_realtime(client)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1013
        assert (await http_client.patch(patch, headers=csrf, json={"is_locked": True})).is_success
        assert await pool.fetchval("SELECT count(*) FROM realtime_outbox") > 0

        async with isolated_redis(server, socket_path, redis):
            await asyncio.wait_for(manager.ready.wait(), 8)
            async with asyncio.timeout(8):
                while await pool.fetchval("SELECT count(*) FROM realtime_outbox"):
                    await asyncio.sleep(0.05)
            async with connect(endpoint, origin=WS_ORIGIN) as client:
                assert await subscribe_realtime(client, scope) == {
                    "type": "subscribed", "scope": scope,
                }
                assert (await http_client.get(path)).json()["thread"]["is_locked"]
                assert (await http_client.patch(
                    patch, headers=csrf, json={"is_locked": False}
                )).is_success
                assert await receive_realtime(client) == {"type": "changed", "scope": scope}
                assert not (await http_client.get(path)).json()["thread"]["is_locked"]


async def register(client: AsyncClient, handle: str, password: str = "profile-password") -> None:
    """зарегистрировать обычного пользователя через настоящее приложение"""

    response = await client.post(
        "/api/v1/auth/register",
        json={"handle": handle, "password": password},
    )
    assert response.status_code == 201


def image_bytes(image_format: str) -> bytes:
    """создать маленькое корректное изображение для настоящего multipart запроса"""

    result = BytesIO()
    Image.new("RGB", (8, 8), "red").save(result, image_format)
    return result.getvalue()


def animated_image_bytes(image_format: str) -> bytes:
    """создать короткую анимацию для настоящего upload и worker сценария"""

    result = BytesIO()
    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    first.save(
        result,
        image_format,
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
        optimize=False,
    )
    return result.getvalue()


def make_audio_file(tmp_path: Path, media_type: str) -> bytes:
    """сгенерировать короткий реальный source через установленный системный FFmpeg"""

    if media_type == "mp3":
        suffix, arguments = ".mp3", ["-c:a", "libmp3lame"]
    elif media_type == "m4a":
        suffix, arguments = ".m4a", ["-c:a", "aac"]
    else:
        suffix, arguments = ".ogg", ["-c:a", "libopus", "-ac", "1"]
    output = tmp_path / f"source-{media_type}{suffix}"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=0.2", *arguments, str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return output.read_bytes()


def probe_audio(path: Path) -> tuple[set[str], str]:
    """прочитать container и codec готового derivative настоящим FFprobe"""

    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=codec_name",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    return set(payload["format"]["format_name"].split(",")), payload["streams"][0]["codec_name"]


async def upload_ready_image(
    client: AsyncClient,
    board_slug: str,
    token: str,
    *,
    payload: bytes | None = None,
    filename: str = "image.jpg",
    mime_type: str = "image/jpeg",
) -> int:
    """загрузить image и вручную завершить worker для HTTP integration сценария"""

    uploaded = await client.post(
        f"/api/v1/boards/{board_slug}/media",
        data={"posting_claim_token": token},
        files={"file": (filename, image_bytes("JPEG") if payload is None else payload, mime_type)},
    )
    assert uploaded.status_code == 201
    media_id = uploaded.json()["id"]
    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    claimed = await repository.claim_by_id(
        media_id,
        max_attempts=MAX_PROCESSING_ATTEMPTS,
        lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    await process_job(repository, media_id, storage)
    return media_id


@pytest.mark.anyio
async def test_owner_flow_csrf_posts_ban_and_logout(http_client: AsyncClient) -> None:
    """owner проходит основной путь без подмены зависимостей"""

    csrf = await login(http_client, "owner", "owner-password")
    assert (await http_client.get("/api/v1/auth/me")).status_code == 200
    board_slug = f"test-{uuid.uuid4().hex}"

    denied = await http_client.post(
        "/api/v1/admin/boards", json={"slug": board_slug, "title": "Тест"}
    )
    assert denied.status_code == 403

    created_board = await http_client.post(
        "/api/v1/admin/boards",
        headers=csrf,
        json={"slug": board_slug, "title": "Тест"},
    )
    assert created_board.status_code == 200

    invalid_post = await http_client.post(f"/api/v1/boards/{board_slug}/threads", json={"body": ""})
    assert invalid_post.status_code == 422
    created_thread = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={"subject": "тема", "body": "первый пост"},
    )
    assert created_thread.status_code == 201
    thread_id = created_thread.json()["thread"]["id"]

    created_reply = await http_client.post(
        f"/api/v1/threads/{thread_id}/posts",
        json={"body": "ответ"},
    )
    assert created_reply.status_code == 200
    # заранее заполняем лимит в выделенном test Redis, чтобы проверить ответ маршрута
    await app.state.redis_client.set(f"spam:posts:{thread_id}:127.0.0.1", "100", ex=3600)
    limited_reply = await http_client.post(
        f"/api/v1/threads/{thread_id}/posts", json={"body": "ещё"}
    )
    assert limited_reply.status_code == 429
    assert (await http_client.get("/api/v1/threads/999999")).status_code == 404

    ban = await http_client.post(
        "/api/v1/admin/ip_bans",
        headers=csrf,
        json={"ip": "127.0.0.1", "reason": "проверка"},
    )
    assert ban.status_code == 200
    assert (
        await http_client.post(
            "/api/v1/admin/ip_bans", headers=csrf, json={"ip": "127.0.0.1"}
        )
    ).status_code == 409
    assert (
        await http_client.post(
            f"/api/v1/boards/{board_slug}/threads", json={"body": "заблокировано"}
        )
    ).status_code == 403

    assert (await http_client.post("/api/v1/auth/logout", headers=csrf)).status_code == 204
    assert (await http_client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_moderator_cannot_create_board_but_can_read_admin_list(
    http_client: AsyncClient,
) -> None:
    """moderator не получает права admin даже с действующей сессией"""

    csrf = await login(http_client, "moderator", "moderator-password")
    assert (await http_client.get("/api/v1/admin/boards")).status_code == 200
    response = await http_client.post(
        "/api/v1/admin/boards",
        headers=csrf,
        json={"slug": "denied", "title": "Запрещено"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_admin_appearance_write_preserves_legacy_settings(http_client: AsyncClient) -> None:
    """изменить безопасные цвета и сохранить ранее записанные поля оформления"""

    endpoint = "/api/v1/admin/settings"
    current = app.state.site_settings.appearance
    current.active_theme = "legacy"
    current.logo_path = "originals/logo.png"
    current.style_vars = {"--legacy-color": "blue"}

    csrf = await login(http_client, "owner", "owner-password")
    settings = (await http_client.get(endpoint)).json()
    appearance = settings["appearance"]
    settings["site"]["title"] = "Проверка настроек"
    assert (await http_client.put(endpoint, json=settings)).status_code == 403

    for field, value, code in [
        ("active_theme", "custom", "invalid_appearance_theme"),
        ("logo_path", "other/logo.png", "appearance_paths_read_only"),
        ("style_vars", {"--unknown-color": "#112233"}, "invalid_appearance_color"),
        ("style_vars", {"--accent-color": "red"}, "invalid_appearance_color"),
    ]:
        rejected = await http_client.put(
            endpoint,
            headers=csrf,
            json={**settings, "appearance": {**appearance, field: value}},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == code

    saved = await http_client.put(endpoint, headers=csrf, json=settings)
    assert saved.status_code == 200
    assert saved.json()["appearance"] == appearance

    appearance["active_theme"] = "light"
    appearance["style_vars"]["--accent-color"] = "#112233"
    saved = await http_client.put(endpoint, headers=csrf, json=settings)
    assert saved.status_code == 200
    assert (await http_client.get(endpoint)).json() == saved.json()
    assert saved.json()["site"]["title"] == "Проверка настроек"
    assert saved.json()["appearance"] == appearance

    csrf = await login(http_client, "moderator", "moderator-password")
    assert (await http_client.get(endpoint)).status_code == 403
    assert (await http_client.put(endpoint, headers=csrf, json=settings)).status_code == 403


@pytest.mark.anyio
async def test_profile_registration_policy_moderation_and_urls(http_client: AsyncClient) -> None:
    """profile проходит регистрацию, policy, csrf и audit через настоящие сервисы"""

    await register(http_client, "alice")
    await register(http_client, "bob")
    profile = await http_client.get("/api/v1/profiles/alice")
    assert profile.status_code == 200
    assert profile.json()["canonical_url"] == "/@alice"

    # owner_only не пускает анонима и другого вошедшего пользователя
    anonymous = await http_client.post("/api/v1/boards/alice/threads", json={"body": "нет"})
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "authentication_required"
    await login(http_client, "bob", "profile-password")
    denied = await http_client.post("/api/v1/boards/alice/threads", json={"body": "нет"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "posting_forbidden"

    alice_csrf = await login(http_client, "alice", "profile-password")
    created = await http_client.post("/api/v1/boards/alice/threads", json={"body": "owner"})
    assert created.status_code == 201
    thread_id = created.json()["thread"]["id"]
    read_thread = await http_client.get(f"/api/v1/threads/{thread_id}")
    assert read_thread.json()["thread"]["canonical_url"] == "/@alice/1"
    assert read_thread.json()["op"]["thread_canonical_url"] == "/@alice/1"

    # по умолчанию ответы требуют сессию, но не владение profile-доской
    assert (await http_client.post("/api/v1/auth/logout", headers=alice_csrf)).status_code == 204
    await login(http_client, "bob", "profile-password")
    authenticated_reply = await http_client.post(
        f"/api/v1/threads/{thread_id}/posts", json={"body": "нет"}
    )
    assert authenticated_reply.status_code == 200
    alice_csrf = await login(http_client, "alice", "profile-password")
    updated_policy = await http_client.patch(
        "/api/v1/profiles/alice",
        headers=alice_csrf,
        json={"thread_creation_policy": "authenticated", "reply_policy": "authenticated"},
    )
    assert updated_policy.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=alice_csrf)).status_code == 204

    anonymous_authenticated = await http_client.post(
        "/api/v1/boards/alice/threads", json={"body": "вход нужен"}
    )
    assert anonymous_authenticated.status_code == 401
    assert anonymous_authenticated.json()["detail"]["code"] == "authentication_required"
    bob_csrf = await login(http_client, "bob", "profile-password")
    authenticated_thread = await http_client.post(
        "/api/v1/boards/alice/threads", json={"body": "вошёл"}
    )
    assert authenticated_thread.status_code == 201
    authenticated_reply = await http_client.post(
        f"/api/v1/threads/{thread_id}/posts", json={"body": "ответ вошедшего"}
    )
    assert authenticated_reply.status_code == 200
    reply_id = authenticated_reply.json()["id"]
    assert (await http_client.post("/api/v1/auth/logout", headers=bob_csrf)).status_code == 204
    alice_csrf = await login(http_client, "alice", "profile-password")

    hidden_reply = await http_client.patch(
        f"/api/v1/profiles/alice/posts/{reply_id}",
        headers=alice_csrf,
        json={"is_hidden": True},
    )
    assert hidden_reply.status_code == 200
    visible_thread = await http_client.get(f"/api/v1/threads/{thread_id}")
    assert reply_id not in {post["id"] for post in visible_thread.json()["replies"]}

    no_csrf = await http_client.patch(
        f"/api/v1/profiles/alice/threads/{thread_id}", json={"is_hidden": True}
    )
    assert no_csrf.status_code == 403
    hidden = await http_client.patch(
        f"/api/v1/profiles/alice/threads/{thread_id}",
        headers=alice_csrf,
        json={"is_hidden": True},
    )
    assert hidden.status_code == 200
    assert (await http_client.get(f"/api/v1/threads/{thread_id}")).status_code == 404
    log = await app.state.postgres_pool.fetchrow(
        "SELECT action, metadata FROM moderation_log WHERE target_id=$1", thread_id
    )
    assert log is not None and log["action"] == "update_thread"
    assert "profile_owner" in log["metadata"] and "board_id" in log["metadata"]

    # другой владелец не управляет чужим тредом даже с корректным csrf
    await http_client.post("/api/v1/auth/logout", headers=alice_csrf)
    bob_csrf = await login(http_client, "bob", "profile-password")
    denied_update = await http_client.patch(
        f"/api/v1/profiles/alice/threads/{thread_id}",
        headers=bob_csrf,
        json={"is_hidden": False},
    )
    assert denied_update.status_code == 403


@pytest.mark.anyio
async def test_public_catalog_isolated_and_staff_override(http_client: AsyncClient) -> None:
    """каталог скрывает profile, а staff сохраняет override owner-модерации"""

    await register(http_client, "alice")
    owner_csrf = await login(http_client, "owner", "owner-password")
    public = await http_client.post(
        "/api/v1/admin/boards", headers=owner_csrf, json={"slug": "public", "title": "Public"}
    )
    assert public.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=owner_csrf)).status_code == 204
    # параллельные анонимные запросы должны получить разные локальные номера доски
    created_threads = await asyncio.gather(
        *[
            http_client.post(
                "/api/v1/boards/public/threads",
                    json={"body": f"public-{number}", "deletion_password": "public-pass"},
            )
            for number in range(1, 4)
        ]
    )
    assert all(response.status_code == 201 for response in created_threads)
    public_urls = {
        (
            await http_client.get(f"/api/v1/threads/{response.json()['thread']['id']}")
        ).json()["thread"]["canonical_url"]
        for response in created_threads
    }
    assert public_urls == {"/public/1", "/public/2", "/public/3"}
    catalog = await http_client.get("/api/v1/boards")
    assert {item["slug"] for item in catalog.json()["items"]} == {"public"}

    alice_csrf = await login(http_client, "alice", "profile-password")
    public_thread_id = created_threads[0].json()["thread"]["id"]
    public_denied = await http_client.patch(
        f"/api/v1/profiles/public/threads/{public_thread_id}",
        headers=alice_csrf,
        json={"is_locked": True},
    )
    assert public_denied.status_code == 403
    created = await http_client.post("/api/v1/boards/alice/threads", json={"body": "owner"})
    assert created.status_code == 201
    thread_id = created.json()["thread"]["id"]
    profile_url = (await http_client.get(f"/api/v1/threads/{thread_id}")).json()["thread"][
        "canonical_url"
    ]
    assert profile_url == "/@alice/1"
    assert profile_url != "/public/1"
    moderator_csrf = await login(http_client, "moderator", "moderator-password")
    override = await http_client.patch(
        f"/api/v1/profiles/alice/threads/{thread_id}",
        headers=moderator_csrf,
        json={"is_locked": True},
    )
    assert override.status_code == 200


@pytest.mark.anyio
async def test_self_delete_for_anonymous_and_accounts(http_client: AsyncClient) -> None:
    """self-delete не раскрывает авторов, чистит каскад и пишет audit log"""

    board_slug = f"self-{uuid.uuid4().hex[:12]}"
    owner_csrf = await login(http_client, "owner", "owner-password")
    created_board = await http_client.post(
        "/api/v1/admin/boards",
        headers=owner_csrf,
        json={"slug": board_slug, "title": "Self delete"},
    )
    assert created_board.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=owner_csrf)).status_code == 204

    missing_thread = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads", json={"body": "без пароля"}
    )
    assert missing_thread.status_code == 422
    assert missing_thread.json()["detail"]["code"] == "deletion_password_required"

    target = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={"body": "цель", "deletion_password": "target-pass"},
    )
    assert target.status_code == 201
    target_thread_id = target.json()["thread"]["id"]
    target_op_id = target.json()["posts"][0]["id"]
    missing_reply = await http_client.post(
        f"/api/v1/threads/{target_thread_id}/posts", json={"body": "без пароля"}
    )
    assert missing_reply.status_code == 422
    assert missing_reply.json()["detail"]["code"] == "deletion_password_required"
    reply = await http_client.post(
        f"/api/v1/threads/{target_thread_id}/posts",
        json={"body": "удалить меня", "deletion_password": "reply-pass"},
    )
    assert reply.status_code == 200
    reply_id = reply.json()["id"]
    public_reply = (await http_client.get(f"/api/v1/threads/{target_thread_id}")).json()
    assert "author_user_id" not in str(public_reply)
    assert "deletion_password" not in str(public_reply)

    wrong_password = await http_client.request(
        "DELETE", f"/api/v1/threads/{target_thread_id}", json={"deletion_password": "wrong"}
    )
    assert wrong_password.status_code == 403
    delete_reply = await http_client.request(
        "DELETE", f"/api/v1/posts/{reply_id}", json={"deletion_password": "reply-pass"}
    )
    assert delete_reply.status_code == 204
    after_reply_delete = await http_client.get(f"/api/v1/threads/{target_thread_id}")
    assert reply_id not in {item["id"] for item in after_reply_delete.json()["replies"]}
    op_delete = await http_client.request(
        "DELETE", f"/api/v1/posts/{target_op_id}", json={"deletion_password": "target-pass"}
    )
    assert op_delete.status_code == 409
    assert op_delete.json()["detail"]["code"] == "opening_post_requires_thread_delete"

    source = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={"body": f">>{target_op_id} source", "deletion_password": "source-pass"},
    )
    assert source.status_code == 201
    source_thread_id = source.json()["thread"]["id"]
    source_op_id = source.json()["posts"][0]["id"]
    await app.state.postgres_pool.execute(
        "INSERT INTO media (post_id, path, size, mime_type) VALUES ($1, $2, $3, $4)",
        source_op_id,
        "originals/test.jpg",
        1,
        "image/jpeg",
    )
    delete_source = await http_client.request(
        "DELETE", f"/api/v1/threads/{source_thread_id}", json={"deletion_password": "source-pass"}
    )
    assert delete_source.status_code == 204
    counts = await app.state.postgres_pool.fetchrow(
        "SELECT (SELECT count(*) FROM threads WHERE id=$1) AS threads, "
        "(SELECT count(*) FROM posts WHERE thread_id=$1) AS posts, "
        "(SELECT count(*) FROM media WHERE post_id=$2) AS media, "
        "(SELECT count(*) FROM post_references WHERE source_post_id=$2) AS refs",
        source_thread_id,
        source_op_id,
    )
    assert dict(counts) == {"threads": 0, "posts": 0, "media": 0, "refs": 0}
    audit = await app.state.postgres_pool.fetchrow(
        "SELECT action, metadata FROM moderation_log "
        "WHERE target_type='thread' AND target_id=$1",
        source_thread_id,
    )
    assert audit is not None and audit["action"] == "self_delete_thread"
    assert "anonymous_self" in audit["metadata"] and "source-pass" not in audit["metadata"]

    rate_limited = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={"body": "rate", "deletion_password": "rate-pass"},
    )
    rate_thread_id = rate_limited.json()["thread"]["id"]
    for _ in range(5):
        denied = await http_client.request(
            "DELETE", f"/api/v1/threads/{rate_thread_id}", json={"deletion_password": "wrong"}
        )
        assert denied.status_code == 403
    blocked = await http_client.request(
        "DELETE", f"/api/v1/threads/{rate_thread_id}", json={"deletion_password": "wrong"}
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "deletion_password_rate_limited"

    await register(http_client, "alice")
    alice_csrf = await login(http_client, "alice", "profile-password")
    anonymous_as_account = await http_client.request(
        "DELETE", f"/api/v1/threads/{target_thread_id}", headers=alice_csrf, json={}
    )
    assert anonymous_as_account.status_code == 403
    account_thread = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads", json={"body": "account"}
    )
    assert account_thread.status_code == 201
    account_thread_id = account_thread.json()["thread"]["id"]
    account_reply = await http_client.post(
        f"/api/v1/threads/{account_thread_id}/posts", json={"body": "account reply"}
    )
    assert account_reply.status_code == 200
    account_reply_id = account_reply.json()["id"]
    await register(http_client, "bob")
    bob_csrf = await login(http_client, "bob", "profile-password")
    foreign_delete = await http_client.request(
        "DELETE", f"/api/v1/threads/{account_thread_id}", headers=bob_csrf, json={}
    )
    assert foreign_delete.status_code == 403
    alice_csrf = await login(http_client, "alice", "profile-password")
    own_reply_delete = await http_client.request(
        "DELETE", f"/api/v1/posts/{account_reply_id}", headers=alice_csrf, json={}
    )
    assert own_reply_delete.status_code == 204
    own_delete = await http_client.request(
        "DELETE", f"/api/v1/threads/{account_thread_id}", headers=alice_csrf, json={}
    )
    assert own_delete.status_code == 204


@pytest.mark.anyio
async def test_posting_claim_consumes_ready_media_atomically(http_client: AsyncClient) -> None:
    """claim хранит token hash и один раз прикрепляет ready media по порядку"""

    board_slug = f"claim-{uuid.uuid4().hex[:11]}"
    owner_csrf = await login(http_client, "owner", "owner-password")
    board = await http_client.post(
        "/api/v1/admin/boards",
        headers=owner_csrf,
        json={"slug": board_slug, "title": "Claims"},
    )
    assert board.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=owner_csrf)).status_code == 204

    claim_response = await http_client.post(f"/api/v1/boards/{board_slug}/posting-claims")
    assert claim_response.status_code == 201
    claim_data = claim_response.json()
    assert set(claim_data) == {"token", "expires_at", "max_attachments"}
    token = claim_data["token"]
    claim = await app.state.postgres_pool.fetchrow(
        "SELECT id, board_id, token_hash FROM posting_claims WHERE token_hash=$1",
        hash_token(token),
    )
    assert claim is not None and claim["token_hash"] != token
    media_rows = await app.state.postgres_pool.fetch(
        """INSERT INTO media (path,size,mime_type,board_id,posting_claim_id,processing_state)
           VALUES ($1,$2,'image/jpeg',$3,$4,'ready'),($5,$6,'video/mp4',$3,$4,'ready')
           RETURNING id""",
        "originals/claim-a.jpg", 10, claim["board_id"], claim["id"],
        "originals/claim-b.mp4", 20,
    )
    media_ids = [row["id"] for row in media_rows]
    created = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={
            "body": "with media",
            "deletion_password": "claim-delete",
            "posting_claim_token": token,
            "media_ids": media_ids,
        },
    )
    assert created.status_code == 201
    post_id = created.json()["posts"][0]["id"]
    attached = await app.state.postgres_pool.fetch(
        "SELECT id, post_id, attachment_position FROM media "
        "WHERE id=ANY($1::bigint[]) ORDER BY attachment_position",
        media_ids,
    )
    assert [(row["id"], row["post_id"], row["attachment_position"]) for row in attached] == [
        (media_ids[0], post_id, 0), (media_ids[1], post_id, 1)
    ]
    assert await app.state.postgres_pool.fetchval(
        "SELECT consumed_at IS NOT NULL FROM posting_claims WHERE id=$1", claim["id"]
    )
    reused = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={
            "body": "reuse",
            "deletion_password": "claim-delete",
            "posting_claim_token": token,
            "media_ids": media_ids,
        },
    )
    assert reused.status_code == 403

    await register(http_client, "alice")
    await login(http_client, "alice", "profile-password")
    account_claim = await http_client.post(f"/api/v1/boards/{board_slug}/posting-claims")
    account_token = account_claim.json()["token"]
    account_claim_id = await app.state.postgres_pool.fetchval(
        "SELECT id FROM posting_claims WHERE token_hash=$1", hash_token(account_token)
    )
    await app.state.postgres_pool.execute(
        """INSERT INTO media (path,size,mime_type,board_id,posting_claim_id,processing_state)
           VALUES ('originals/account.jpg',1,'image/jpeg',$1,$2,'ready')""",
        claim["board_id"], account_claim_id,
    )
    account_media_id = await app.state.postgres_pool.fetchval(
        "SELECT id FROM media WHERE posting_claim_id=$1", account_claim_id
    )
    await register(http_client, "bob")
    await login(http_client, "bob", "profile-password")
    foreign = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={
            "body": "foreign",
            "posting_claim_token": account_token,
            "media_ids": [account_media_id],
        },
    )
    assert foreign.status_code == 403
    await app.state.postgres_pool.execute(
        "UPDATE posting_claims SET expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
        account_claim_id,
    )
    await login(http_client, "alice", "profile-password")
    expired = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={
            "body": "expired",
            "posting_claim_token": account_token,
            "media_ids": [account_media_id],
        },
    )
    assert expired.status_code == 403
    assert await PostingClaimsRepository(app.state.postgres_pool).cleanup_expired() == 1
    assert await app.state.postgres_pool.fetchval(
        "SELECT count(*) FROM media WHERE id=$1", account_media_id
    ) == 0


@pytest.mark.anyio
async def test_image_upload_quarantine_worker_attach_and_cleanup(http_client: AsyncClient) -> None:
    """image upload остаётся закрытым до worker и удаляется через db cleanup queue"""

    board_slug = f"images-{uuid.uuid4().hex[:10]}"
    owner_csrf = await login(http_client, "owner", "owner-password")
    board = await http_client.post(
        "/api/v1/admin/boards",
        headers=owner_csrf,
        json={"slug": board_slug, "title": "Images"},
    )
    assert board.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=owner_csrf)).status_code == 204

    claim = await http_client.post(f"/api/v1/boards/{board_slug}/posting-claims")
    assert claim.status_code == 201
    token = claim.json()["token"]

    uploads = []
    for filename, declared_mime, payload in [
        ("not-trusted.bin", "text/plain", image_bytes("JPEG")),
        ("picture.png", "application/octet-stream", image_bytes("PNG")),
        ("picture.webp", "image/webp", image_bytes("WEBP")),
        ("animation.jpg", "text/plain", animated_image_bytes("GIF")),
        ("animation.gif", "application/octet-stream", animated_image_bytes("WEBP")),
    ]:
        response = await http_client.post(
            f"/api/v1/boards/{board_slug}/media",
            data={"posting_claim_token": token},
            files={"file": (filename, payload, declared_mime)},
        )
        assert response.status_code == 201
        assert set(response.json()) == {
            "id", "size", "mime_type", "processing_state", "created_at"
            , "media_kind"
        }
        assert response.json()["processing_state"] == "queued"
        uploads.append(response.json()["id"])

    broken_mp4 = await http_client.post(
        f"/api/v1/boards/{board_slug}/media",
        data={"posting_claim_token": token},
        files={"file": ("video.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4")},
    )
    assert broken_mp4.status_code == 422
    assert broken_mp4.json()["detail"]["code"] == "invalid_media"
    for payload in [
        b"RIFF\x00\x00\x00\x00WAVE",
        b"fLaC\x00\x00\x00\x22",
        b"\x1aE\xdf\xa3\x93B\x82\x88webm",
    ]:
        rejected = await http_client.post(
            f"/api/v1/boards/{board_slug}/media",
            data={"posting_claim_token": token},
            files={"file": ("fake.mp3", payload, "audio/mpeg")},
        )
        assert rejected.status_code == 415
        assert rejected.json()["detail"]["code"] == "unsupported_media_type"
    broken = await http_client.post(
        f"/api/v1/boards/{board_slug}/media",
        data={"posting_claim_token": token},
        files={"file": ("pretend.jpg", b"not an image", "image/jpeg")},
    )
    assert broken.status_code == 422
    assert broken.json()["detail"]["code"] == "invalid_media"
    for payload in [b"ID3broken", b"\x00\x00\x00\x18ftypM4A broken", b"OggSbroken"]:
        broken_audio = await http_client.post(
            f"/api/v1/boards/{board_slug}/media",
            data={"posting_claim_token": token},
            files={"file": ("wrong.jpg", payload, "image/jpeg")},
        )
        assert broken_audio.status_code == 422
        assert broken_audio.json()["detail"]["code"] == "invalid_media"

    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    for media_id in uploads:
        claimed = await repository.claim_by_id(
            media_id,
            max_attempts=MAX_PROCESSING_ATTEMPTS,
            lease_duration=PROCESSING_LEASE,
        )
        assert claimed is not None
        quarantine_path = storage.resolve(claimed.path)
        assert quarantine_path.exists()
        await process_job(repository, media_id, storage)
        ready = await repository.get_by_id(media_id)
        assert ready is not None and ready.processing_state.value == "ready"
        assert ready.display_path is not None and ready.preview_path is not None
        assert storage.resolve(ready.display_path).is_file()
        assert storage.resolve(ready.preview_path).is_file()
        assert await cleanup_media_files(repository=repository, storage=storage) == 1
        assert not quarantine_path.exists()

    created = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={
            "body": "images",
            "deletion_password": "image-delete",
            "posting_claim_token": token,
            "media_ids": uploads,
        },
    )
    assert created.status_code == 201
    thread_id = created.json()["thread"]["id"]
    post_media = created.json()["posts"][0]["media"]
    assert len(post_media) == 5
    assert [item["is_animation"] for item in post_media] == [False, False, False, True, True]
    display = await http_client.get(post_media[0]["display_url"])
    preview = await http_client.head(post_media[0]["preview_url"])
    assert display.status_code == 200
    assert display.headers["x-accel-redirect"].startswith("/_imageboard_media/display/")
    assert display.headers["content-type"] == "image/webp"
    assert "quarantine" not in str(display.headers)
    assert preview.status_code == 200
    assert preview.headers["x-accel-redirect"].startswith("/_imageboard_media/previews/")
    assert (await http_client.get(f"/media/{uploads[0]}/display")).status_code == 404
    deleted = await http_client.request(
        "DELETE", f"/api/v1/threads/{thread_id}", json={"deletion_password": "image-delete"}
    )
    assert deleted.status_code == 204
    assert (await http_client.get(post_media[0]["display_url"])).status_code == 404
    assert await app.state.postgres_pool.fetchval("SELECT count(*) FROM media") == 0
    await cleanup_media_files(repository=repository, storage=storage)
    assert not media_files(storage)


@pytest.mark.anyio
async def test_parallel_media_upload_reserves_work_before_acceptance(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """две загрузки не получают один резерв, а worker использует уже выделенное место"""

    csrf = await login(http_client, "owner", "owner-password")
    board = await http_client.post(
        "/api/v1/admin/boards", headers=csrf, json={"slug": "quota", "title": "Quota"},
    )
    assert board.status_code == 200
    claim = await http_client.post("/api/v1/boards/quota/posting-claims")
    token = claim.json()["token"]
    quota = app.state.media_quota
    payload = image_bytes("PNG")
    work = work_budget("image", "image/png", False)
    quota.settings = quota.settings.model_copy(update={
        "media_storage_max_bytes": quota.settings.media_ingress_budget
        + 2 * reserved_bytes(len(payload)) + work,
    })

    async def upload() -> Response:
        return await http_client.post(
            "/api/v1/boards/quota/media", data={"posting_claim_token": token},
            files={"file": ("image.png", payload, "image/png")},
        )

    responses = sorted(await asyncio.gather(upload(), upload()), key=lambda r: r.status_code)
    accepted, rejected = responses
    assert accepted.status_code == 201, accepted.text
    assert rejected.status_code == 503, rejected.text
    assert rejected.json()["detail"]["code"] == "media_storage_full"
    assert len(media_files(quota.storage)) == 1
    media_id = accepted.json()["id"]
    assert await quota.pool.fetchval(
        "SELECT work_bytes FROM media_disk_files WHERE media_id=$1", media_id,
    ) == work
    repository = MediaRepository(quota.pool)
    claimed = await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    with monkeypatch.context() as patch:
        patch.setattr(MediaQuota, "free_bytes", lambda _: 0)
        with pytest.raises(MediaStorageFull):
            await process_job(repository, media_id, quota.storage)
    await repository.defer_processing(
        media_id, claimed.processing_lease_until, "media_storage_full",
    )
    deferred = await repository.get_by_id(media_id)
    assert deferred is not None and deferred.processing_attempts == 0
    assert deferred.processing_state.value == "queued"
    assert quota.storage.resolve(deferred.path).is_file()
    assert await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    ) is None
    await quota.pool.execute(
        "UPDATE media SET processing_next_attempt_at=clock_timestamp() WHERE id=$1", media_id,
    )
    claimed = await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    await process_job(
        repository, media_id, quota.storage, expected_lease=claimed.processing_lease_until,
    )
    ready = await repository.get_by_id(media_id)
    assert ready is not None and ready.processing_state.value == "ready"
    assert ready.display_path is not None
    await repository.delete(media_id)
    quota.storage.delete_file(ready.display_path)
    assert (await upload()).status_code == 503
    assert await cleanup_media_files(repository=repository, storage=quota.storage) == 3
    assert await cleanup_media_files(repository=repository, storage=quota.storage) == 0
    assert await quota.pool.fetchval("SELECT count(*) FROM media_disk_files") == 0
    assert (await upload()).status_code == 201


@pytest.mark.anyio
async def test_media_quota_promotes_materialized_source_at_free_space_boundary(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """проверка свободных блоков не списывает записанный исходник повторно"""

    quota = app.state.media_quota
    path = quota.storage.new_quarantine_path(".upload")
    payload = image_bytes("PNG")
    await quota.reserve_source(path, len(payload))
    quota.storage.prepare_parent(path).write_bytes(payload)
    work = work_budget("image", "image/png", False)
    floor = quota.settings.media_storage_min_free_bytes + quota.settings.media_ingress_budget
    available = floor + work - 1
    monkeypatch.setattr(quota, "free_bytes", lambda: available)
    async with quota.transaction() as connection:
        with pytest.raises(MediaStorageFull):
            await quota.promote_source(connection, path, media_id=1, size=len(payload), work=work)
        available += 1
        await quota.promote_source(connection, path, media_id=1, size=len(payload), work=work)
    row = await quota.pool.fetchrow("SELECT * FROM media_disk_files WHERE path=$1", path)
    assert row["bytes"] == reserved_bytes(len(payload))
    assert row["work_bytes"] == work and row["expires_at"] is None


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != "linux", reason="requires the Linux worker filesystem guard")
async def test_media_worker_recovers_after_process_stop(http_client: AsyncClient) -> None:
    """новый процесс удаляет незавершённый файл и обрабатывает задачу после lease"""
    quota = app.state.media_quota
    repository = MediaRepository(quota.pool)
    source = quota.storage.new_quarantine_path(".upload")
    payload = image_bytes("PNG")
    await quota.reserve_source(source, len(payload))
    quota.storage.prepare_parent(source).write_bytes(payload)
    media = await repository.create(path=source, size=len(payload), mime_type="image/png")
    work = work_budget("image", "image/png", False)
    async with quota.transaction() as connection:
        await quota.promote_source(
            connection, source, media_id=media.id, size=len(payload), work=work,
        )
    schema = await quota.pool.fetchval("SELECT current_schema()")
    database = urlsplit(os.environ["TEST_DATABASE_URL"])
    query = "&".join(filter(None, (database.query, urlencode({"search_path": schema}))))
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": database._replace(query=query).geturl(),
        "REDIS_URL": os.environ["TEST_REDIS_URL"],
        "MEDIA_STORAGE_ROOT": str(quota.storage.root),
    }
    paused_worker = """
import asyncio
from threading import Event
from scripts import media_worker

def pause_after_partial_write(source, display, preview, is_animation):
    display.write_bytes(b"partial")
    Event().wait()

media_worker.make_derivatives = pause_after_partial_write
asyncio.run(media_worker.run_worker())
"""
    before = await snapshot(quota)
    assert before["queued"] == 1
    assert before["quota_pending_bytes"] == work
    assert before["quota_charged_bytes"] == reserved_bytes(len(payload)) + work

    @asynccontextmanager
    async def subprocess_worker(*args: str):
        process = await asyncio.create_subprocess_exec(
            sys.executable, *args, cwd=PROJECT_ROOT, env=env,
        )
        try:
            yield process
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise

    async with subprocess_worker("-c", paused_worker) as process:
        async with asyncio.timeout(15):
            while True:
                assert process.returncode is None
                paths = await quota.pool.fetchval(
                    "SELECT work_paths FROM media_disk_files WHERE path=$1", source,
                )
                if paths and quota.storage.resolve(paths[0]).exists():
                    partial = quota.storage.resolve(paths[0])
                    break
                await asyncio.sleep(0.05)

    assert partial.read_bytes() == b"partial"
    await quota.pool.execute(
        "UPDATE media SET processing_lease_until=now()-interval '1 second' WHERE id=$1",
        media.id,
    )
    stopped = await snapshot(quota)
    assert stopped["processing"] == stopped["expired_processing_leases"] == 1
    assert "expired_processing_leases" in warnings(stopped)
    async with subprocess_worker("-m", "scripts.media_worker") as process:
        async with asyncio.timeout(15):
            while True:
                assert process.returncode is None
                ready = await repository.get_by_id(media.id)
                if ready is not None and ready.processing_state.value == "ready":
                    break
                await asyncio.sleep(0.05)

    assert ready.processing_attempts == 2
    assert not partial.exists()
    assert ready.display_path and ready.preview_path
    for path in (ready.display_path, ready.preview_path):
        with Image.open(quota.storage.resolve(path)) as derivative:
            assert derivative.format == "WEBP"
    processed = await snapshot(quota)
    assert processed["processing"] == processed["failed"] == processed["quota_pending_bytes"] == 0
    assert processed["cleanup_pending"] == 1
    await quota.pool.execute(
        """UPDATE media_cleanup_tasks SET created_at=now()-interval '16 minutes',
           last_error='temporary failure' WHERE relative_path=$1""",
        source,
    )
    pending = await snapshot(quota)
    assert pending["old_cleanup"] == pending["cleanup_errors"] == 1
    assert {"old_cleanup", "cleanup_errors"}.issubset(warnings(pending))
    assert await cleanup_media_files(repository=repository, storage=quota.storage) == 1
    assert not quota.storage.resolve(source).exists()
    cleaned = await snapshot(quota)
    assert cleaned["cleanup_pending"] == cleaned["cleanup_errors"] == 0
    assert cleaned["quota_charged_bytes"] < processed["quota_charged_bytes"]


@pytest.mark.anyio
async def test_media_quota_cleans_reused_derivative_after_second_worker_crash(
    http_client: AsyncClient,
) -> None:
    """два исчерпанных lease на одном конечном пути освобождают оба резерва"""

    quota = app.state.media_quota
    repository = MediaRepository(quota.pool)
    source = quota.storage.new_quarantine_path(".upload")
    payload = image_bytes("PNG")
    work = work_budget("image", "image/png", False)
    await quota.reserve_source(source, len(payload))
    quota.storage.prepare_parent(source).write_bytes(payload)
    media = await repository.create(path=source, size=len(payload), mime_type="image/png")
    async with quota.transaction() as connection:
        await quota.promote_source(
            connection, source, media_id=media.id, size=len(payload), work=work,
        )
    final = f"display/{media.id}.webp"

    for attempt in range(2):
        claimed = await repository.claim_by_id(
            media.id, max_attempts=1, lease_duration=timedelta(seconds=-1),
        )
        assert claimed is not None
        async with quota.transaction() as connection:
            if attempt:
                await quota.admit(connection, work)
            await quota.start_work(connection, source, work, [final])
        quota.storage.prepare_parent(final).write_bytes(b"interrupted before ready")
        recovered = await repository.recover_expired(max_attempts=1)
        assert len(recovered) == 1 and recovered[0].processing_state.value == "failed"
        assert await cleanup_media_files(repository=repository, storage=quota.storage) == 1
        assert not quota.storage.resolve(final).exists()
        assert quota.storage.resolve(source).read_bytes() == payload
        assert await quota.pool.fetchval(
            "SELECT work_bytes FROM media_disk_files WHERE path=$1", source,
        ) == 0
        if not attempt:
            assert await repository.retry_failed(media.id) is not None


@pytest.mark.anyio
@pytest.mark.parametrize("http_client", ["before-quota"], indirect=True)
async def test_media_quota_inventory_keeps_existing_files(http_client: AsyncClient) -> None:
    """обновление 024 сохраняет известные и неизвестные файлы и возвращает место после cleanup"""

    quota = app.state.media_quota
    source = "quarantine/legacy.upload"
    quota.storage.prepare_parent(source).write_bytes(b"existing source")
    unknown = quota.storage.resolve("keep.bin")
    unknown.write_bytes(b"unknown")
    media_id = await quota.pool.fetchval(
        "INSERT INTO media(path,size,mime_type) VALUES ($1,15,'image/png') RETURNING id", source,
    )
    migration = PROJECT_ROOT / "migrations/025_media_disk_quota.sql"
    await quota.pool.execute(migration.read_text(encoding="utf-8"))
    response = await http_client.post("/api/v1/boards/quota/media")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "media_storage_full"
    assert await inventory_media_storage(quota) == (2, 1, 0)
    await quota.pool.execute(
        "INSERT INTO media_cleanup_tasks (relative_path) VALUES ('keep.bin')",
    )
    assert await quota.pool.fetchval(
        "SELECT work_bytes > 0 FROM media_disk_files WHERE media_id=$1", media_id,
    )
    stale = quota.storage.new_quarantine_path(".upload")
    await quota.reserve_source(stale, 16)
    quota.storage.prepare_parent(stale).write_bytes(b"stale")
    await quota.pool.execute(
        "UPDATE media_disk_files SET expires_at=clock_timestamp() WHERE path=$1", stale,
    )
    repository = MediaRepository(quota.pool)
    assert await cleanup_media_files(repository=repository, storage=quota.storage) == 1
    assert not quota.storage.resolve(stale).exists()
    assert quota.storage.resolve(source).read_bytes() == b"existing source"
    assert unknown.read_bytes() == b"unknown"
    assert await quota.pool.fetchval("SELECT count(*) FROM media_disk_files") == 2
    unknown.unlink()
    await inventory_media_storage(quota, reconcile=True)
    assert await quota.pool.fetchval("SELECT count(*) FROM media_disk_files") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("http_client", ["small-volume"], indirect=True)
async def test_media_quota_returns_503_on_low_space_or_write_failure(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """переполнение не оставляет частичной загрузки и не отнимает запись после очистки"""

    csrf = await login(http_client, "owner", "owner-password")
    assert (await http_client.post(
        "/api/v1/admin/boards", headers=csrf, json={"slug": "disk", "title": "Disk"},
    )).status_code == 200
    claim = await http_client.post("/api/v1/boards/disk/posting-claims")
    token = claim.json()["token"]
    quota = app.state.media_quota
    quota.settings = quota.settings.model_copy(update={
        "media_storage_min_free_bytes": 2 * MEBIBYTE,
        "media_upload_max_file_bytes": 2 * MEBIBYTE,
        "media_upload_concurrency": 1,
    })

    async def upload() -> Response:
        return await http_client.post(
            "/api/v1/boards/disk/media", data={"posting_claim_token": token},
            files={"file": ("image.png", image_bytes("PNG"), "image/png")},
        )

    if os.getenv("TEST_MEDIA_QUOTA_MOUNT"):
        assert quota.storage.root.is_mount()
        quota.settings = quota.settings.model_copy(update={"app_env": "production"})
        space = quota.free_bytes()
        assert 0 < space <= 64 * MEBIBYTE
        filler = quota.storage.resolve("test-fill")
        try:
            with filler.open("xb") as output:
                with pytest.raises(OSError) as kernel_error:
                    os.posix_fallocate(output.fileno(), 0, space + MEBIBYTE)
                assert kernel_error.value.errno == errno.ENOSPC
                output.truncate(0)
                os.posix_fallocate(output.fileno(), 0, space - MEBIBYTE)
                rejected = await upload()
        finally:
            filler.unlink(missing_ok=True)
    else:
        def fail_sync(_: Path) -> None:
            raise OSError(errno.ENOSPC, "disk is full")

        with monkeypatch.context() as patch:
            patch.setattr(quota.storage, "sync_file", fail_sync)
            rejected = await upload()
    assert rejected.status_code == 503, rejected.text
    assert rejected.json()["detail"]["code"] == "media_storage_full"
    assert await quota.pool.fetchval("SELECT count(*) FROM media_disk_files") == 0
    assert not media_files(quota.storage)
    assert (await upload()).status_code == 201


@pytest.mark.anyio
async def test_media_upload_through_nginx_with_chunked_body(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """передать multipart без content-length через настоящий reverse proxy"""

    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("Nginx is required")
    csrf = await login(http_client, "owner", "owner-password")
    assert (await http_client.post(
        "/api/v1/admin/boards", headers=csrf, json={"slug": "chunked", "title": "Chunked"},
    )).status_code == 200
    quota = app.state.media_quota
    async with nginx_client(nginx, quota.storage.root, tmp_path) as proxy:
        claim = await proxy.post("/api/v1/boards/chunked/posting-claims")
        assert claim.status_code == 201, claim.text
        token = claim.json()["token"].encode()
        body = (
            b'--quota\r\nContent-Disposition: form-data; name="posting_claim_token"\r\n\r\n'
            + token
            + b'\r\n--quota\r\nContent-Disposition: form-data; name="file"; filename="test.jpg"\r\n'
            + b'Content-Type: image/jpeg\r\n\r\n'
        )

        async def stream(padding: bytes):
            yield body
            yield image_bytes("JPEG") + padding
            yield b"\r\n--quota--\r\n"

        headers = {"Content-Type": "multipart/form-data; boundary=quota"}
        uploaded = await proxy.post(
            "/api/v1/boards/chunked/media", content=stream(b""), headers=headers,
        )
        assert uploaded.status_code == 201, uploaded.text
        quota.settings = quota.settings.model_copy(update={"media_upload_max_file_bytes": 512})
        rejected = await proxy.post(
            "/api/v1/boards/chunked/media", content=stream(b"x" * 65536), headers=headers,
        )
        assert rejected.status_code == 413, rejected.text
        assert rejected.json()["detail"]["code"] == "media_file_too_large"
        assert not any(path.is_file() for path in (quota.storage.root / ".nginx-body").rglob("*"))


@pytest.mark.anyio
async def test_worker_retries_then_fails_modified_animation_without_derivatives(
    http_client: AsyncClient,
) -> None:
    """повреждённый quarantine не делает partial derivative и остаётся в retry lifecycle"""

    board_slug = f"animation-retry-{uuid.uuid4().hex[:8]}"
    csrf = await login(http_client, "owner", "owner-password")
    created_board = await http_client.post(
        "/api/v1/admin/boards",
        headers=csrf,
        json={"slug": board_slug, "title": "Animation retry"},
    )
    assert created_board.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=csrf)).status_code == 204

    claim = await http_client.post(f"/api/v1/boards/{board_slug}/posting-claims")
    assert claim.status_code == 201
    uploaded = await http_client.post(
        f"/api/v1/boards/{board_slug}/media",
        data={"posting_claim_token": claim.json()["token"]},
        files={"file": ("animation.gif", animated_image_bytes("GIF"), "image/gif")},
    )
    assert uploaded.status_code == 201
    media_id = uploaded.json()["id"]

    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    record = await repository.get_by_id(media_id)
    assert record is not None
    storage.resolve(record.path).write_bytes(b"not an image after upload")

    for attempt in range(MAX_PROCESSING_ATTEMPTS):
        claimed = await repository.claim_by_id(
            media_id,
            max_attempts=MAX_PROCESSING_ATTEMPTS,
            lease_duration=PROCESSING_LEASE,
        )
        assert claimed is not None
        with pytest.raises(MediaPolicyError):
            await process_job(repository, media_id, storage)
        result = await repository.fail_processing(
            media_id,
            max_attempts=MAX_PROCESSING_ATTEMPTS,
            error="test decoder failure",
        )
        assert result is not None
        if attempt < MAX_PROCESSING_ATTEMPTS - 1:
            assert result.processing_state.value == "queued"
        else:
            assert result.processing_state.value == "failed"
        assert not storage.resolve(f"display/{media_id}.webp").exists()
        assert not storage.resolve(f"previews/{media_id}.webp").exists()


@pytest.mark.anyio
async def test_audio_content_delivery_requires_ready_visible_attachment(
    http_client: AsyncClient,
) -> None:
    """canonical audio выдаётся через content, а delete немедленно закрывает opaque URL"""

    board_slug = f"audio-delivery-{uuid.uuid4().hex[:8]}"
    csrf = await login(http_client, "owner", "owner-password")
    board = await http_client.post(
        "/api/v1/admin/boards",
        headers=csrf,
        json={"slug": board_slug, "title": "Audio delivery"},
    )
    assert board.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=csrf)).status_code == 204
    created = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={"body": "audio", "deletion_password": "audio-delete"},
    )
    assert created.status_code == 201
    thread_id = created.json()["thread"]["id"]
    post_id = created.json()["posts"][0]["id"]
    public_key = "audioMediaKey_4x9JRfdTQeW2pNu8LmcZVabH7sK1"
    media_id = await app.state.postgres_pool.fetchval(
        """INSERT INTO media (
               post_id,path,size,mime_type,processing_state,public_key,media_kind,
               canonical_path,canonical_mime_type,duration_seconds
           ) VALUES ($1,'quarantine/private',12,'audio/mpeg','ready',$2,'audio',
                     'audio/pending.m4a','audio/mp4',12)
           RETURNING id""",
        post_id,
        public_key,
    )
    assert media_id is not None
    await app.state.postgres_pool.execute(
        "UPDATE media SET canonical_path=$2 WHERE id=$1", media_id, f"audio/{media_id}.m4a"
    )

    page = await http_client.get(f"/api/v1/threads/{thread_id}")
    media = page.json()["op"]["media"]
    assert media == [
        {
            "media_kind": "audio",
            "preview_url": None,
            "display_url": None,
            "content_url": f"/media/{public_key}/content",
            "mime_type": "audio/mp4",
            "is_animation": False,
            "duration_seconds": 12,
        }
    ]
    content = await http_client.get(media[0]["content_url"])
    head = await http_client.head(media[0]["content_url"])
    assert content.status_code == 200
    assert content.headers["content-type"] == "audio/mp4"
    assert content.headers["x-accel-redirect"] == f"/_imageboard_media/audio/{media_id}.m4a"
    assert head.status_code == 200
    assert "quarantine" not in str(content.headers)
    assert (await http_client.get(f"/media/{public_key}/display")).status_code == 404

    deleted = await http_client.request(
        "DELETE", f"/api/v1/threads/{thread_id}", json={"deletion_password": "audio-delete"}
    )
    assert deleted.status_code == 204
    assert (await http_client.get(media[0]["content_url"])).status_code == 404


@pytest.mark.anyio
async def test_real_ffmpeg_audio_upload_worker_attach_and_delivery(
    http_client: AsyncClient, tmp_path: Path
) -> None:
    """mp3 m4a и ogg/opus проходят реальный upload worker и protected delivery lifecycle"""

    board_slug = f"audio-e2e-{uuid.uuid4().hex[:8]}"
    csrf = await login(http_client, "owner", "owner-password")
    board = await http_client.post(
        "/api/v1/admin/boards", headers=csrf, json={"slug": board_slug, "title": "Audio E2E"}
    )
    assert board.status_code == 200
    assert (await http_client.post("/api/v1/auth/logout", headers=csrf)).status_code == 204
    claim = await http_client.post(f"/api/v1/boards/{board_slug}/posting-claims")
    assert claim.status_code == 201
    token = claim.json()["token"]

    uploaded_ids: list[int] = []
    for media_type, filename, client_mime in [
        ("mp3", "not-trusted.bin", "application/octet-stream"),
        ("m4a", "wrong-extension.mp3", "audio/mpeg"),
        ("opus", "not-trusted.jpg", "text/plain"),
    ]:
        response = await http_client.post(
            f"/api/v1/boards/{board_slug}/media",
            data={"posting_claim_token": token},
            files={"file": (filename, make_audio_file(tmp_path, media_type), client_mime)},
        )
        assert response.status_code == 201
        uploaded_ids.append(response.json()["id"])

    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    for media_id, expected in zip(
        uploaded_ids,
        [("m4a", "aac"), ("m4a", "aac"), ("ogg", "opus")],
        strict=True,
    ):
        queued = await repository.get_by_id(media_id)
        assert queued is not None
        quarantine_path = storage.resolve(queued.path)
        assert quarantine_path.is_file()
        claimed = await repository.claim_by_id(
            media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE
        )
        assert claimed is not None
        await process_job(repository, media_id, storage)
        ready = await repository.get_by_id(media_id)
        assert ready is not None and ready.processing_state.value == "ready"
        assert ready.canonical_path is not None
        actual_containers, actual_codec = probe_audio(storage.resolve(ready.canonical_path))
        assert expected[0] in actual_containers
        assert actual_codec == expected[1]
        assert await cleanup_media_files(repository=repository, storage=storage) == 1
        assert not quarantine_path.exists()

    created = await http_client.post(
        f"/api/v1/boards/{board_slug}/threads",
        json={
            "body": "real audio",
            "deletion_password": "audio-e2e-delete",
            "posting_claim_token": token,
            "media_ids": uploaded_ids,
        },
    )
    assert created.status_code == 201
    thread_id = created.json()["thread"]["id"]
    media = created.json()["posts"][0]["media"]
    assert [item["media_kind"] for item in media] == ["audio", "audio", "voice"]
    assert all(item["content_url"] is not None for item in media)
    assert all(item["preview_url"] is None and item["display_url"] is None for item in media)
    ranged = await http_client.get(media[0]["content_url"], headers={"range": "bytes=0-4"})
    assert ranged.status_code == 200
    assert ranged.headers["x-accel-redirect"].startswith("/_imageboard_media/audio/")
    assert "quarantine" not in str(ranged.headers)

    deleted = await http_client.request(
        "DELETE", f"/api/v1/threads/{thread_id}", json={"deletion_password": "audio-e2e-delete"}
    )
    assert deleted.status_code == 204
    assert (await http_client.get(media[0]["content_url"])).status_code == 404


@pytest.mark.anyio
async def test_media_delivery_closes_after_profile_hide_and_delete(
    http_client: AsyncClient,
) -> None:
    """owner hide/delete сразу закрывает delivery до физической cleanup очереди"""

    await register(http_client, "alice")
    csrf = await login(http_client, "alice", "profile-password")
    thread_claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert thread_claim.status_code == 201
    thread_media_id = await upload_ready_image(
        http_client,
        "alice",
        thread_claim.json()["token"],
        payload=animated_image_bytes("GIF"),
        filename="thread.gif",
        mime_type="image/gif",
    )
    created = await http_client.post(
        "/api/v1/boards/alice/threads",
        json={
            "body": "image thread",
            "posting_claim_token": thread_claim.json()["token"],
            "media_ids": [thread_media_id],
        },
    )
    assert created.status_code == 201
    thread_id = created.json()["thread"]["id"]
    thread_url = created.json()["posts"][0]["media"][0]["display_url"]
    assert created.json()["posts"][0]["media"][0]["is_animation"] is True
    assert (await http_client.get(thread_url)).status_code == 200

    hidden_thread = await http_client.patch(
        f"/api/v1/profiles/alice/threads/{thread_id}",
        headers=csrf,
        json={"is_hidden": True},
    )
    assert hidden_thread.status_code == 200
    assert (await http_client.get(thread_url)).status_code == 404

    restored_thread = await http_client.patch(
        f"/api/v1/profiles/alice/threads/{thread_id}",
        headers=csrf,
        json={"is_hidden": False},
    )
    assert restored_thread.status_code == 200
    assert (await http_client.get(thread_url)).status_code == 200

    reply_claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert reply_claim.status_code == 201
    reply_media_id = await upload_ready_image(http_client, "alice", reply_claim.json()["token"])
    reply = await http_client.post(
        f"/api/v1/threads/{thread_id}/posts",
        json={
            "body": "image reply",
            "posting_claim_token": reply_claim.json()["token"],
            "media_ids": [reply_media_id],
        },
    )
    assert reply.status_code == 200
    reply_id = reply.json()["id"]
    reply_url = reply.json()["media"][0]["preview_url"]
    assert (await http_client.get(reply_url)).status_code == 200
    hidden_reply = await http_client.patch(
        f"/api/v1/profiles/alice/posts/{reply_id}",
        headers=csrf,
        json={"is_hidden": True},
    )
    assert hidden_reply.status_code == 200
    assert (await http_client.get(reply_url)).status_code == 404

    deleted_thread = await http_client.delete(
        f"/api/v1/profiles/alice/threads/{thread_id}", headers=csrf
    )
    assert deleted_thread.status_code == 204
    assert (await http_client.get(thread_url)).status_code == 404


def make_video_file(
    path: Path, *, size: str = "160x120", fps: int = 24, duration: int = 1,
    audio: bool = False, codec: str = "libx264",
) -> bytes:
    """создать короткий MP4 с настоящим потоком и предсказуемой metadata"""

    audio_args = (
        ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac"]
        if audio else []
    )
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"color=red:s={size}:r={fps}:d={duration}", *audio_args,
            "-metadata", "title=private tag", "-metadata:s:v:0", "handler_name=private handler",
            "-c:v", codec, "-threads", "1", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return path.read_bytes()


async def upload_video(
    client: AsyncClient, board: str, token: str, payload: bytes,
) -> Response:
    """публичная форма не может задать тип даже для квадратного MP4"""

    return await client.post(
        f"/api/v1/boards/{board}/media",
        data={"posting_claim_token": token, "media_kind": "video_note"},
        files={"file": ("note.png", payload, "image/png")},
    )


@pytest.mark.anyio
async def test_video_upload_worker_visibility_and_cleanup(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """обычный и квадратный MP4 проходят общий lifecycle с настоящим FFmpeg"""

    await register(http_client, "alice")
    csrf = await login(http_client, "alice", "profile-password")
    claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert claim.status_code == 201
    token = claim.json()["token"]
    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    media_ids = []
    for audio, dimensions in [(True, "1920x1080"), (False, "160x160")]:
        src = make_video_file(tmp_path / "source.mp4", size=dimensions, audio=audio)
        uploaded = await upload_video(http_client, "alice", token, src)
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["media_kind"] == "video"
        assert uploaded.json()["mime_type"] == "video/mp4"
        assert not {"path", "content_url", "preview_url"} & uploaded.json().keys()
        media_id = uploaded.json()["id"]
        media_ids.append(media_id)
        premature = await http_client.post(
            "/api/v1/boards/alice/threads",
            json={"body": "test", "posting_claim_token": token, "media_ids": [media_id]},
        )
        assert premature.status_code == 403
        assert premature.json()["detail"]["code"] == "posting_claim_media_invalid"

        claimed = await repository.claim_by_id(
            media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
        )
        assert claimed is not None
        assert claimed.sample_rate == (44_100 if audio else None)
        await process_job(repository, media_id, storage)
        ready = await repository.get_by_id(media_id)
        assert ready is not None and ready.processing_state.value == "ready"
        assert ready.canonical_path and ready.preview_path
        assert ready.channels == (1 if audio else None)
        assert ready.sample_rate == (48_000 if audio else None)
        canonical = storage.resolve(ready.canonical_path)
        payload = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json",
             str(canonical)], timeout=10,
        ))
        codecs = [stream["codec_name"] for stream in payload["streams"]]
        assert codecs == (["h264", "aac"] if audio else ["h264"])
        if audio:
            assert payload["streams"][1]["profile"] == "LC"
            assert int(payload["streams"][1]["sample_rate"]) == ready.sample_rate
        assert payload["streams"][0]["pix_fmt"] == "yuv420p"
        assert payload["streams"][0]["r_frame_rate"] == "30/1"
        assert "private tag" not in str(payload) and "private handler" not in str(payload)
        encoded = canonical.read_bytes()
        assert 0 <= encoded.find(b"moov") < encoded.find(b"mdat")
        with Image.open(storage.resolve(ready.preview_path)) as poster:
            assert poster.format == "WEBP" and poster.n_frames == 1
            assert poster.width <= 320 and poster.height <= 180
        assert storage.resolve(ready.path).is_file()
        assert await cleanup_media_files(repository=repository, storage=storage) == 1
        assert not storage.resolve(ready.path).exists()

    attached = await http_client.post(
        "/api/v1/boards/alice/threads",
        json={"body": "videos", "posting_claim_token": token, "media_ids": media_ids},
    )
    assert attached.status_code == 201
    thread_id = attached.json()["thread"]["id"]
    assets = attached.json()["posts"][0]["media"]
    assert len(assets) == 2
    for item, media_id in zip(assets, media_ids, strict=True):
        assert item["display_url"] is None and item["duration_seconds"] == 1
        content = await http_client.get(item["content_url"], headers={"Range": "bytes=0-15"})
        assert content.status_code == 200 and content.content == b""
        assert content.headers["content-type"] == "video/mp4"
        assert content.headers["x-accel-redirect"] == f"/_imageboard_media/video/{media_id}.mp4"
        assert content.headers["cache-control"] == "no-store"
        preview = await http_client.head(item["preview_url"])
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/webp"
        assert preview.headers["x-accel-redirect"] == (
            f"/_imageboard_media/video-previews/{media_id}.webp"
        )
        assert preview.headers["cache-control"] == "no-store"
        display = await http_client.get(item["content_url"].replace("/content", "/display"))
        assert display.status_code == 404

    path = f"/api/v1/profiles/alice/threads/{thread_id}"
    hidden = await http_client.patch(path, headers=csrf, json={"is_hidden": True})
    assert hidden.status_code == 200
    for item in assets:
        for url in [item["preview_url"], item["content_url"]]:
            hidden_asset = await http_client.get(url)
            assert hidden_asset.status_code == 404
            assert hidden_asset.headers["cache-control"] == "no-store"
    assert (await http_client.delete(path, headers=csrf)).status_code == 204
    assert await cleanup_media_files(repository=repository, storage=storage) == 4
    assert not media_files(storage)


@pytest.mark.anyio
async def test_video_upload_rejects_bad_inputs_and_enforces_byte_limit(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """MP4 проверяется по фактическому container, виду потока и параметрам"""

    await register(http_client, "alice")
    await login(http_client, "alice", "profile-password")
    claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert claim.status_code == 201 and claim.json()["max_attachments"] == 10
    token = claim.json()["token"]
    source = tmp_path / "source.mp4"
    valid = make_video_file(source)
    for payload, status, code in [
        (valid[:-10], 422, "invalid_media"),
        (b"\x1aE\xdf\xa3\x93B\x82\x88webm", 415, "unsupported_media_type"),
        (make_video_file(source, codec="mpeg4"), 415, "unsupported_media_type"),
        (make_video_file(source, fps=31), 422, "invalid_media"),
        (make_video_file(source, size="1922x32"), 422, "invalid_media"),
        (make_video_file(source, duration=121, fps=1), 422, "invalid_media"),
    ]:
        rejected = await upload_video(http_client, "alice", token, payload)
        assert rejected.status_code == status, rejected.text
        assert rejected.json()["detail"]["code"] == code
        assert not list(app.state.media_storage_root.rglob("*.upload"))

    padding = 20 * MEBIBYTE - len(valid)
    exact = valid + padding.to_bytes(4, "big") + b"free" + bytes(padding - 8)
    assert (await upload_video(http_client, "alice", token, exact)).status_code == 201
    rejected = await upload_video(http_client, "alice", token, exact + b"\0")
    assert rejected.status_code == 413
    assert rejected.json()["detail"]["code"] == "media_file_too_large"
    assert len(list(app.state.media_storage_root.rglob("*.upload"))) == 1


@pytest.mark.anyio
async def test_video_upload_uses_board_attachment_count_and_total_limits(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """байты видео и изображений считаются вместе в пределах одной доски"""

    csrf = await login(http_client, "owner", "owner-password")
    board = await http_client.post(
        "/api/v1/admin/boards",
        headers=csrf,
        json={
            "slug": "limited",
            "title": "Limited",
            "media_overrides": {
                "max_attachments_per_post": 2,
                "max_attachment_size_mib": 2,
                "max_total_size_mib": 2,
            },
        },
    )
    assert board.status_code == 200
    claim = await http_client.post("/api/v1/boards/limited/posting-claims")
    assert claim.status_code == 201 and claim.json()["max_attachments"] == 2
    token = claim.json()["token"]
    source = make_video_file(tmp_path / "source.mp4")
    padding = MEBIBYTE + 8
    video = source + padding.to_bytes(4, "big") + b"free" + bytes(padding - 8)
    assert (await upload_video(http_client, "limited", token, video)).status_code == 201
    rejected = await upload_video(http_client, "limited", token, video)
    assert rejected.status_code == 413
    assert rejected.json()["detail"]["code"] == "media_post_too_large"
    image = await http_client.post(
        "/api/v1/boards/limited/media",
        data={"posting_claim_token": token},
        files={"file": ("image.png", image_bytes("PNG"), "image/png")},
    )
    assert image.status_code == 201
    rejected = await upload_video(http_client, "limited", token, source)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "media_count_exceeded"
    assert len(list(app.state.media_storage_root.rglob("*.upload"))) == 2


@pytest.mark.anyio
async def test_video_worker_retries_corrupt_samples_without_leaving_derivatives(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """повреждение кадров после upload оставляет только источник для повторной попытки"""

    await register(http_client, "alice")
    await login(http_client, "alice", "profile-password")
    claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert claim.status_code == 201
    payload = make_video_file(tmp_path / "source.mp4")
    uploaded = await upload_video(http_client, "alice", claim.json()["token"], payload)
    assert uploaded.status_code == 201
    media_id = uploaded.json()["id"]
    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    claimed = await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    original = storage.resolve(claimed.path)
    start = payload.index(b"mdat") + 4
    end = payload.index(b"moov") - 4
    original.write_bytes(payload[:start] + b"\xff" * (end - start) + payload[end:])
    with pytest.raises(MediaPolicyError):
        await process_job(repository, media_id, storage)
    retry = await repository.fail_processing(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, error="damaged video",
        lease=claimed.processing_lease_until,
    )
    assert retry is not None and retry.processing_state.value == "queued"
    assert media_files(storage) == [original]

    original.write_bytes(payload)
    claimed = await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    await process_job(repository, media_id, storage)
    ready = await repository.get_by_id(media_id)
    assert ready is not None and ready.processing_state.value == "ready"


@pytest.mark.anyio
async def test_video_preview_replace_failure_rolls_back_ready_and_cleanup(
    http_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ошибка публикации превью оставляет источник для следующей попытки"""

    await register(http_client, "alice")
    await login(http_client, "alice", "profile-password")
    claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert claim.status_code == 201
    payload = make_video_file(tmp_path / "source.mp4")
    uploaded = await upload_video(http_client, "alice", claim.json()["token"], payload)
    assert uploaded.status_code == 201
    media_id = uploaded.json()["id"]
    repository = MediaRepository(app.state.postgres_pool)
    storage = MediaStorage(app.state.media_storage_root)
    claimed = await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    original = storage.resolve(claimed.path)
    replace = storage.replace_from_temporary

    def fail_preview(temporary_path: Path, relative_path: str) -> Path:
        if relative_path == f"previews/{media_id}.webp":
            assert storage.resolve(f"video/{media_id}.mp4").is_file()
            raise OSError("preview replace failed")
        return replace(temporary_path, relative_path)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "replace_from_temporary", fail_preview)
        with pytest.raises(OSError, match="preview replace failed"):
            await process_job(repository, media_id, storage)

    processing = await repository.get_by_id(media_id)
    assert processing is not None and processing.processing_state.value == "processing"
    assert processing.canonical_path is None and processing.preview_path is None
    assert await cleanup_media_files(repository=repository, storage=storage) == 0
    assert media_files(storage) == [original]
    retry = await repository.fail_processing(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, error="preview replace failed",
        lease=claimed.processing_lease_until,
    )
    assert retry is not None and retry.processing_state.value == "queued"
    claimed = await repository.claim_by_id(
        media_id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert claimed is not None
    await process_job(repository, media_id, storage, expected_lease=claimed.processing_lease_until)
    ready = await repository.get_by_id(media_id)
    assert ready is not None and ready.processing_state.value == "ready"
    assert await cleanup_media_files(repository=repository, storage=storage) == 1
    assert not original.exists()


@pytest.mark.anyio
async def test_video_note_worker_requires_current_lease_and_public_claim_cannot_attach(
    http_client: AsyncClient, tmp_path: Path,
) -> None:
    """доверенный kind поддерживается worker, но публичный claim отклоняет готовый кружок"""

    await register(http_client, "alice")
    await login(http_client, "alice", "profile-password")
    claim = await http_client.post("/api/v1/boards/alice/posting-claims")
    assert claim.status_code == 201
    token = claim.json()["token"]
    pool = app.state.postgres_pool
    stored_claim = await PostingClaimsRepository(pool).get_claim(hash_token(token))
    assert stored_claim is not None
    repository = MediaRepository(pool)
    storage = MediaStorage(app.state.media_storage_root)
    data = make_video_file(tmp_path / "square.mp4", size="160x160")
    relative_path = storage.new_quarantine_path(".mp4")
    quota = MediaQuota(pool, storage)
    await quota.reserve_source(relative_path, len(data))
    original = storage.prepare_parent(relative_path)
    original.write_bytes(data)
    note = await repository.create(
        path=relative_path, size=len(data), mime_type="video/mp4", media_kind="video_note",
        board_id=stored_claim.board_id, posting_claim_id=stored_claim.id, duration_seconds=1,
    )
    async with quota.transaction() as connection:
        await quota.promote_source(
            connection, relative_path, media_id=note.id, size=len(data),
            work=work_budget(note.media_kind, note.mime_type, note.is_animation),
        )
    stale = await repository.claim_by_id(
        note.id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=timedelta(seconds=-1),
    )
    assert stale is not None
    with pytest.raises(RuntimeError, match="lease was lost"):
        await process_job(repository, note.id, storage)
    assert media_files(storage) == [original]
    assert await repository.recover_expired(max_attempts=MAX_PROCESSING_ATTEMPTS)

    current = await repository.claim_by_id(
        note.id, max_attempts=MAX_PROCESSING_ATTEMPTS, lease_duration=PROCESSING_LEASE,
    )
    assert current is not None
    assert await repository.fail_processing(
        note.id, max_attempts=MAX_PROCESSING_ATTEMPTS, error="stale",
        lease=stale.processing_lease_until,
    ) is None
    await process_job(repository, note.id, storage)
    ready = await repository.get_by_id(note.id)
    assert ready is not None and ready.processing_state.value == "ready"
    assert ready.canonical_path and ready.preview_path
    VideoProcessor().inspect(storage.resolve(ready.canonical_path), MediaType.VIDEO_NOTE)
    rejected = await http_client.post(
        "/api/v1/boards/alice/threads",
        json={"body": "note", "posting_claim_token": token, "media_ids": [note.id]},
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "posting_claim_media_invalid"
    assert await pool.fetchval("SELECT public_key FROM media WHERE id = $1", note.id) is None

    thread = await http_client.post("/api/v1/boards/alice/threads", json={"body": "trusted note"})
    assert thread.status_code == 201
    thread_id = thread.json()["thread"]["id"]
    post_id = thread.json()["posts"][0]["id"]
    public_key = uuid.uuid4().hex
    async with pool.acquire() as connection:
        async with connection.transaction():
            await PostingClaimsRepository(pool).attach_and_consume(
                connection, stored_claim.id, post_id, [note.id], {note.id: public_key},
            )
    page = await http_client.get(f"/api/v1/threads/{thread_id}")
    media = page.json()["op"]["media"][0]
    assert media["media_kind"] == "video_note"
    assert media["preview_url"] == f"/media/{public_key}/preview"
    assert media["content_url"] == f"/media/{public_key}/content"
    assert (await http_client.get(media["preview_url"])).status_code == 200
    assert (await http_client.get(media["content_url"])).status_code == 200

    await repository.delete(note.id)
    assert await cleanup_media_files(repository=repository, storage=storage) == 3
    assert not media_files(storage)
