"""сквозная проверка http api с настоящими postgresql и redis"""

import asyncio
import json
import os
import subprocess
import uuid
from io import BytesIO
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from redis.asyncio import Redis

from app.api.dependencies import (
    get_postgres_pool,
    get_redis_client,
    get_site_settings,
    get_ws_manager,
)
from app.core.security import CSRF_COOKIE, hash_password, hash_token
from app.core.site_settings import SiteSettings
from app.main import app
from app.repositories.media import MediaRepository
from app.repositories.posting_claims import PostingClaimsRepository
from app.repositories.users import UsersRepository
from app.services.media_policy import MediaPolicyError
from app.services.media_storage import MediaStorage
from app.ws.manager import DisabledWebSocketManager
from scripts.cleanup_media_files import cleanup_media_files
from scripts.media_worker import MAX_PROCESSING_ATTEMPTS, PROCESSING_LEASE, process_job

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def http_client(tmp_path: Path) -> AsyncClient:
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
    for migration in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
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
    app.state.media_storage_root = tmp_path / "media"
    app.state.ws_manager = DisabledWebSocketManager()
    app.dependency_overrides = {
        get_postgres_pool: lambda: pool,
        get_redis_client: lambda: redis,
        get_site_settings: lambda: settings,
        get_ws_manager: lambda: app.state.ws_manager,
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

    unsupported = await http_client.post(
        f"/api/v1/boards/{board_slug}/media",
        data={"posting_claim_token": token},
        files={"file": ("video.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4")},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["detail"]["code"] == "unsupported_media_type"
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
    assert not [path for path in storage.root.rglob("*") if path.is_file()]


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
