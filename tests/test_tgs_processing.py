"""проверка границ и канонизации tgs до запуска нативного рендера"""

import gzip
import json
import subprocess
from pathlib import Path

import pytest

from app.services.media_policy import MediaPolicyCode, MediaPolicyError
from app.services.tgs_processing import (
    MAX_JSON_BYTES,
    MAX_TGS_BYTES,
    canonicalize_tgs,
    transcode_tgs,
)


def encode(document: dict) -> bytes:
    return gzip.compress(json.dumps(document).encode("utf-8"))


def test_canonical_tgs_preserves_animation_and_strips_metadata(
    tgs_payload: bytes, tgs_document: dict,
) -> None:
    clean, canonical, duration = canonicalize_tgs(tgs_payload)
    assert clean == gzip.decompress(canonical)
    assert canonical != tgs_payload
    assert canonical == canonicalize_tgs(canonical)[1]
    assert canonical == canonicalize_tgs(encode(dict(reversed(list(tgs_document.items())))))[1]
    document = json.loads(clean)
    assert document["layers"] == tgs_document["layers"] and document["tgs"] == 1
    assert "nm" not in document and duration == 2


@pytest.mark.parametrize(
    "field,value", [("w", 511), ("op", 181), ("fr", 61), ("fr", 0), ("ip", 120)],
)
def test_tgs_rejects_invalid_canvas_or_timing(tgs_document: dict, field: str, value: int) -> None:
    tgs_document[field] = value
    with pytest.raises(MediaPolicyError) as invalid:
        canonicalize_tgs(encode(tgs_document))
    assert invalid.value.code is MediaPolicyCode.INVALID_MEDIA


def test_tgs_accepts_exactly_three_seconds(tgs_document: dict) -> None:
    tgs_document["op"] = 180
    assert canonicalize_tgs(encode(tgs_document))[2] == 3


def test_tgs_rejects_excessive_or_ambiguous_gzip(tgs_payload: bytes) -> None:
    for payload in (
        tgs_payload[:-1], tgs_payload + tgs_payload, tgs_payload + b"\0",
        gzip.compress(b" " * (MAX_JSON_BYTES + 1)),
    ):
        with pytest.raises(MediaPolicyError) as invalid:
            canonicalize_tgs(payload)
        assert invalid.value.code is MediaPolicyCode.INVALID_MEDIA
    with pytest.raises(MediaPolicyError) as oversized:
        canonicalize_tgs(tgs_payload.ljust(MAX_TGS_BYTES + 1, b"\0"))
    assert oversized.value.code is MediaPolicyCode.MEDIA_FILE_TOO_LARGE


@pytest.mark.parametrize("document", [b'{"w":512,"w":512}', b'{"x":NaN}', b"[" * 2500])
def test_tgs_rejects_ambiguous_or_excessively_nested_json(document: bytes) -> None:
    with pytest.raises(MediaPolicyError):
        canonicalize_tgs(gzip.compress(document))


def test_tgs_rejects_external_assets_expressions_and_cycles(tgs_document: dict) -> None:
    layer = tgs_document["layers"][0]
    invalid_layers = [
        {**layer, "ks": {"o": {"a": 0, "k": 50, "x": "expression"}}},
        {**layer, "shapes": [{"ty": "rp"}]},
        {**layer, "parent": 1},
        {**layer, "ty": 0, "refId": "loop"},
    ]
    for invalid in invalid_layers:
        tgs_document["layers"] = [invalid]
        tgs_document["assets"] = [{"id": "loop", "layers": [invalid]}]
        with pytest.raises(MediaPolicyError):
            canonicalize_tgs(encode(tgs_document))
    tgs_document["layers"] = [layer]
    tgs_document["assets"] = [{"id": "image", "u": "images/", "p": "image.png"}]
    with pytest.raises(MediaPolicyError):
        canonicalize_tgs(encode(tgs_document))


def test_tgs_rejects_excessive_expanded_compositions(tgs_document: dict) -> None:
    layer = tgs_document["layers"][0]
    tgs_document["assets"] = [
        {"id": "shape", "layers": [{**layer, "ind": n} for n in range(1, 31)]},
    ]
    tgs_document["layers"] = [{"ty": 0, "ind": n, "refId": "shape"} for n in range(1, 12)]
    with pytest.raises(MediaPolicyError, match="expanded complexity"):
        canonicalize_tgs(encode(tgs_document))


def test_tgs_keeps_reachable_vector_compositions(tgs_document: dict) -> None:
    tgs_document["assets"] = [
        {"id": "unused", "layers": []},
        {"id": "shape", "layers": tgs_document["layers"]},
    ]
    tgs_document["layers"] = [{"ty": 0, "ind": 1, "refId": "shape", "hasMask": False}]
    clean = json.loads(canonicalize_tgs(encode(tgs_document))[0])
    assert [asset["id"] for asset in clean["assets"]] == ["shape"]
    assert "hasMask" not in clean["layers"][0]


def test_tgs_renderer_timeout_preserves_no_canonical_output(
    tgs_payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, preview = (tmp_path / name for name in ("source.tgs", "out.tgs", "out.webp"))
    source.write_bytes(tgs_payload)

    def timeout(command, **options):
        assert 0 < options["timeout"] <= 8 and options["env"] == {"MALLOC_ARENA_MAX": "2"}
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(MediaPolicyError, match="время рендера TGS"):
        transcode_tgs(source, target, preview)
    assert not target.exists()
