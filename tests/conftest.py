"""общие тестовые данные для статичных и анимированных media"""

import gzip
import json

import pytest


@pytest.fixture
def tgs_document() -> dict:
    """создать анимацию с движущимся полупрозрачным кругом"""

    return {
        "v": "5.7.4", "fr": 60, "ip": 0, "op": 120, "w": 512, "h": 512,
        "tgs": 1, "nm": "removed metadata", "ddd": 0, "assets": [],
        "layers": [{
            "ddd": 0, "ind": 1, "ty": 4, "sr": 1, "ip": 0, "op": 120, "st": 0, "bm": 0,
            "ks": {
                "o": {"a": 0, "k": 50},
                "r": {"a": 0, "k": 0},
                "p": {"a": 1, "k": [
                    {"t": 0, "s": [256, 256, 0], "e": [320, 256, 0],
                     "i": {"x": 0.5, "y": 0.5}, "o": {"x": 0.5, "y": 0.5}},
                    {"t": 120, "s": [320, 256, 0]},
                ]},
                "a": {"a": 0, "k": [0, 0, 0]},
                "s": {"a": 0, "k": [100, 100, 100]},
            },
            "shapes": [
                {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [100, 100]}, "d": 1},
                {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]}, "o": {"a": 0, "k": 100}, "r": 1},
            ],
        }],
    }


@pytest.fixture
def tgs_payload(tgs_document: dict) -> bytes:
    return gzip.compress(json.dumps(tgs_document).encode("utf-8"), mtime=12345)
