"""страница для локального управления api"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["admin-ui"])
ADMIN_PAGE = Path(__file__).with_name("admin.html").read_text(encoding="utf-8")


@router.get("/admin", include_in_schema=False, response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    """отдать статичную страницу управления"""

    return HTMLResponse(ADMIN_PAGE)
