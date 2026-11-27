"""canonical url без зависимости от маршрутов fastapi"""


def board_canonical_url(board_type: str, slug: str) -> str:
    """построить публичный адрес обычной или личной доски"""

    return f"/@{slug}" if board_type == "profile" else f"/{slug}"


def thread_canonical_url(board_type: str, slug: str, thread_number: int) -> str:
    """построить адрес треда по номеру внутри доски"""

    return f"{board_canonical_url(board_type, slug)}/{thread_number}"
