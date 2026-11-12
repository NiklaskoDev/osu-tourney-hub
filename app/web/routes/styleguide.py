from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.web.deps import require_admin
from app.web.templating import render

router = APIRouter()


@router.get("/styleguide", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def styleguide(request: Request) -> HTMLResponse:
    """Every component in every state, for design review. Administrators only."""
    return render(request, "pages/styleguide/index.html")
