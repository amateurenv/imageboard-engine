"""изолированный linux-процесс для одного кадра из проверенного Lottie JSON"""

import ctypes
import resource
import sys
from array import array
from typing import BinaryIO

SIDE = 512
MAX_INPUT = 1024 * 1024


def _bind(library: ctypes.CDLL) -> None:
    """проверить наличие функций C API и задать размеры аргументов и указателей"""

    library.lottie_animation_from_data.argtypes = [ctypes.c_char_p] * 3
    library.lottie_animation_from_data.restype = ctypes.c_void_p
    library.lottie_animation_destroy.argtypes = [ctypes.c_void_p]
    library.lottie_animation_destroy.restype = None
    library.lottie_animation_get_size.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
    ]
    library.lottie_animation_get_size.restype = None
    library.lottie_animation_get_totalframe.argtypes = [ctypes.c_void_p]
    library.lottie_animation_get_totalframe.restype = ctypes.c_size_t
    library.lottie_animation_render.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
    ]
    library.lottie_animation_render.restype = None


def _render(library: ctypes.CDLL, data: bytes, output: BinaryIO) -> None:
    """отрендерить начальный кадр в статический WebP с альфа-каналом"""

    from PIL import Image

    animation = library.lottie_animation_from_data(data, b"", b"")
    if not animation:
        raise ValueError("rlottie rejected animation")
    try:
        width, height = ctypes.c_size_t(), ctypes.c_size_t()
        library.lottie_animation_get_size(animation, ctypes.byref(width), ctypes.byref(height))
        frames = library.lottie_animation_get_totalframe(animation)
        if (width.value, height.value) != (SIDE, SIDE) or not 1 <= frames <= 180:
            raise ValueError("rlottie reported invalid animation bounds")
        pixels = (ctypes.c_uint32 * (SIDE * SIDE))()
        library.lottie_animation_render(animation, 0, pixels, SIDE, SIDE, SIDE * 4)
        # rlottie пишет premultiplied ARGB32 - WebP нужен обычный RGBA
        raw = bytes(pixels)
        if sys.byteorder != "little":
            words: array[int] = array("I")
            words.frombytes(raw)
            words.byteswap()
            raw = words.tobytes()
        with Image.frombytes("RGBa", (SIDE, SIDE), raw, "raw", "BGRa") as premultiplied:
            with premultiplied.convert("RGBA") as frame:
                frame.save(output, "WEBP", quality=85, method=4)
    finally:
        library.lottie_animation_destroy(animation)


def main() -> int:
    """ограничить ресурсы до загрузки нативного кода и чтения входа"""

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024, 512 * 1024))
    for soname in ("librlottie.so.0-1", "librlottie.so.0", "librlottie.so"):
        try:
            library = ctypes.CDLL(soname)
            _bind(library)
            break
        except (OSError, AttributeError):
            continue
    else:
        return 2
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        return 1
    _render(library, data, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
