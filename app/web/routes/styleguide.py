from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.web.templating import render

router = APIRouter()


@router.get("/styleguide", response_class=HTMLResponse)
def styleguide(request: Request) -> HTMLResponse:
    """Every component in every state, for design review.

    Intended for admins only. Login does not exist yet, so until phase 1 replaces this check with
    the admin dependency the page is hidden unless STYLEGUIDE_PREVIEW is on.
    """
    if not request.app.state.settings.styleguide_preview:
        raise HTTPException(status_code=404)
    return render(request, "pages/styleguide/index.html")
