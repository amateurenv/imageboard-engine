"""проверка и канонизация gzip-анимаций для внутреннего потока стикеров"""

import gzip
import json
import math
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError
from app.services.sticker_processing import STICKER_SIDE

TGS_MIME = "application/x-tgsticker"
MAX_TGS_BYTES = 64 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_NODES = 100_000
MAX_JSON_DEPTH = 32
MAX_EXPANDED_LAYERS = 256
MAX_EXPANDED_NODES = 200_000
MAX_COMPOSITION_DEPTH = 8
RENDER_TIMEOUT_SECONDS = 8
SHAPE_TYPES = {"gr", "tr", "sh", "rc", "el", "fl", "st", "gf", "tm"}
FORBIDDEN_KEYS = {
    "ef", "masksProperties", "hasMask", "tt", "td", "tp", "tm",
    "fonts", "chars", "ext", "events", "__proto__", "prototype", "constructor",
}
METADATA_KEYS = {"nm", "mn", "ln", "cl", "meta"}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _check_tree(document: dict[str, Any]) -> None:
    """ограничить дерево до обхода слоёв и исключить выражения и внешние ресурсы"""

    pending = [(document, 0, "")]
    visited = 0
    while pending:
        value, depth, key = pending.pop()
        visited += 1
        if visited > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON complexity limit")
        if isinstance(value, dict):
            for name in METADATA_KEYS:
                value.pop(name, None)
            if value.get("hasMask") is False:
                value.pop("hasMask")
            if FORBIDDEN_KEYS.intersection(value):
                raise ValueError("unsupported Lottie property")
            if "ty" in value and type(value["ty"]) not in (int, str):
                raise ValueError("invalid Lottie type")
            if isinstance(value.get("ty"), str) and value["ty"] not in SHAPE_TYPES:
                raise ValueError("unsupported Lottie shape")
            # rlottie читает свойства фигуры только после её дискриминатора ty
            ordered = sorted(value.items(), key=lambda item: (item[0] != "ty", item[0]))
            value.clear()
            value.update(ordered)
            pending.extend((child, depth + 1, name) for name, child in value.items())
        elif isinstance(value, list):
            pending.extend((child, depth + 1, "") for child in value)
        elif isinstance(value, str):
            if (
                key not in {"id", "refId", "ty", "v", "n"}
                or not 1 <= len(value) <= 128 or not value.isascii() or not value.isprintable()
                or value in {"__proto__", "prototype", "constructor"}
            ):
                raise ValueError("unsupported Lottie string")
        elif type(value) in (int, float):
            if not -1_000_000 <= value <= 1_000_000:
                raise ValueError("Lottie number out of bounds")
        elif type(value) is not bool:
            raise ValueError("invalid Lottie value")


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("invalid Lottie array")
    return value


def _tree_size(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(_tree_size(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_tree_size(item) for item in value)
    return 1


def _check_shapes(value: Any) -> None:
    for shape in _objects(value):
        kind = shape.get("ty")
        if not isinstance(kind, str) or kind not in SHAPE_TYPES:
            raise ValueError("unsupported Lottie shape")
        if kind == "gr":
            _check_shapes(shape.get("it"))


def _check_compositions(document: dict[str, Any]) -> None:
    """посчитать реально разворачиваемые слои и узлы с учётом повторных ссылок"""

    compositions: dict[str, list[dict[str, Any]]] = {}
    for asset in _objects(document.get("assets", [])):
        name = asset.get("id")
        if (
            not isinstance(name, str) or name in compositions
            or asset.keys() - {"id", "layers", "w", "h", "fr"}
            or any(
                type(asset[field]) is not int or not 1 <= asset[field] <= 4096
                for field in ("w", "h") if field in asset
            )
        ):
            raise ValueError("only embedded vector compositions are supported")
        compositions[name] = _objects(asset.get("layers"))

    layer_count = node_count = 0
    referenced: set[str] = set()

    def visit(layers: list[dict[str, Any]], stack: tuple[str, ...]) -> None:
        nonlocal layer_count, node_count
        if len(stack) > MAX_COMPOSITION_DEPTH:
            raise ValueError("Lottie composition depth limit")
        parents: dict[int, int | None] = {}
        for layer in layers:
            layer_count += 1
            node_count += _tree_size(layer)
            if layer_count > MAX_EXPANDED_LAYERS or node_count > MAX_EXPANDED_NODES:
                raise ValueError("Lottie expanded complexity limit")
            kind = layer.get("ty")
            if (
                type(kind) is not int or kind not in {0, 3, 4}
                or layer.get("ddd", 0) != 0 or layer.get("ao", 0) != 0
                or layer.get("sr", 1) != 1
            ):
                raise ValueError("unsupported Lottie layer")
            index, parent = layer.get("ind"), layer.get("parent")
            if type(index) is not int or index in parents:
                raise ValueError("invalid Lottie layer index")
            if parent is not None and type(parent) is not int:
                raise ValueError("invalid Lottie layer parent")
            parents[index] = parent
            if kind == 4:
                _check_shapes(layer.get("shapes"))
            if kind == 0:
                reference = layer.get("refId")
                if (
                    not isinstance(reference, str) or reference not in compositions
                    or reference in stack
                ):
                    raise ValueError("invalid Lottie composition reference")
                referenced.add(reference)
                visit(compositions[reference], (*stack, reference))
        for parent in parents.values():
            depth = 0
            while parent is not None:
                depth += 1
                if depth > MAX_COMPOSITION_DEPTH or parent not in parents:
                    raise ValueError("invalid Lottie parent chain")
                parent = parents[parent]

    root_layers = _objects(document.get("layers"))
    if not root_layers:
        raise ValueError("missing Lottie layers")
    visit(root_layers, ())
    # канонический файл содержит только композиции, до которых можно дойти из корня
    document["assets"] = [
        asset for asset in document.get("assets", []) if asset["id"] in referenced
    ]


def canonicalize_tgs(data: bytes) -> tuple[bytes, bytes, int]:
    """вернуть JSON для рендера, канонический TGS и длительность с округлением вверх"""

    if len(data) > MAX_TGS_BYTES:
        raise MediaPolicyError(
            code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
            message="размер TGS больше 64 КиБ",
            http_status=413,
            limit_bytes=MAX_TGS_BYTES,
        )
    try:
        # ровно один gzip stream - без дописывания второго документа после первого
        decoder = zlib.decompressobj(wbits=31)
        raw = decoder.decompress(data, MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError("invalid or oversized gzip")
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant,
        )
        if not isinstance(document, dict):
            raise ValueError("invalid Lottie root")
        _check_tree(document)
        if (
            document.get("w") != STICKER_SIDE or document.get("h") != STICKER_SIDE
            or document.get("ddd", 0) != 0 or not isinstance(document.get("v"), str)
        ):
            raise ValueError("TGS requires a 512x512 vector canvas")
        fps = document.get("fr")
        start = document.get("ip")
        end = document.get("op")
        if (
            not isinstance(fps, int | float) or isinstance(fps, bool)
            or not isinstance(start, int | float) or isinstance(start, bool)
            or not isinstance(end, int | float) or isinstance(end, bool)
            or not 1 <= fps <= 60
            or not 0 <= start < end
            or int(start) != start or int(end) != end
            or end - start > 3 * fps
        ):
            raise ValueError("TGS frame range or duration exceeds limits")
        _check_compositions(document)
        normalized = {
            "v": document["v"], "fr": fps, "ip": int(start), "op": int(end),
            "w": STICKER_SIDE, "h": STICKER_SIDE, "tgs": 1,
            "layers": document["layers"], "assets": document["assets"],
        }
        clean = json.dumps(normalized, separators=(",", ":")).encode("ascii")
        canonical = gzip.compress(clean, compresslevel=9, mtime=0)
        if len(clean) > MAX_JSON_BYTES or len(canonical) > MAX_TGS_BYTES:
            raise ValueError("canonical TGS exceeds limits")
    except (ValueError, RecursionError, zlib.error) as error:
        raise MediaPolicy.invalid_media(f"некорректный TGS: {error}") from error
    return clean, canonical, math.ceil((end - start) / fps)


def transcode_tgs(source: Path, target: Path, preview: Path) -> int:
    """записать TGS и один прозрачный кадр после проверки в отдельном процессе"""

    with source.open("rb") as original:
        clean, canonical, duration = canonicalize_tgs(original.read(MAX_TGS_BYTES + 1))
    try:
        with preview.open("xb") as output:
            subprocess.run(
                # glibc берёт размер стека потоков из лимита на момент exec
                [
                    "/usr/bin/prlimit", "--stack=1048576:1048576", "--",
                    sys.executable, "-I", str(Path(__file__).with_name("tgs_renderer.py")),
                ],
                input=clean, stdout=output, stderr=subprocess.DEVNULL,
                env={"MALLOC_ARENA_MAX": "2"},
                timeout=RENDER_TIMEOUT_SECONDS, check=True,
            )
    except FileNotFoundError as error:
        if error.filename != "/usr/bin/prlimit":
            raise
        raise MediaPolicyError(
            code=MediaPolicyCode.MEDIA_PROCESSING_UNAVAILABLE,
            message="воркеру нужна утилита prlimit для рендера TGS",
            http_status=503,
        ) from error
    except subprocess.TimeoutExpired as error:
        raise MediaPolicy.invalid_media("превышено время рендера TGS") from error
    except subprocess.CalledProcessError as error:
        if error.returncode == 2:
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_PROCESSING_UNAVAILABLE,
                message="воркеру нужна системная библиотека rlottie",
                http_status=503,
            ) from error
        raise MediaPolicy.invalid_media("не удалось отрендерить TGS") from error
    target.write_bytes(canonical)
    return duration
