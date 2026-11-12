"""Error pages. Users see a plain sentence; details stay in the server log."""

from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web.errors import UserFacingError
from app.web.templating import render

GENERIC: dict[int, tuple[str, str]] = {
    400: ("Request not valid", "The request could not be understood. Go back and try again."),
    401: ("Log in required", "Log in with osu! to continue."),
    403: ("Not allowed", "You do not have permission to do this."),
    404: ("Page not found", "This page does not exist, or you do not have access to it."),
    405: ("Not allowed", "This address does not accept that kind of request."),
    409: ("Not possible right now", "The request conflicts with the current state."),
    422: (
        "Request not valid",
        "Some of the information sent was not valid. Go back and try again.",
    ),
    429: ("Too many requests", "Wait a minute, then try again."),
    502: ("osu! did not respond", "osu! did not answer. Try again in a few minutes."),
}
FALLBACK = ("Something went wrong", "Try again in a moment.")


def login_url(request: Request) -> str:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return "/auth/login?" + urlencode({"next": target})


def error_page(
    request: Request, status: int, message: str | None = None, headers: dict[str, str] | None = None
) -> HTMLResponse:
    title, default_message = GENERIC.get(status, FALLBACK)
    return render(
        request,
        "pages/error.html",
        {"status": status, "title": title, "message": message or default_message},
        status_code=status,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 401 and request.method == "GET":
            return RedirectResponse(login_url(request), status_code=303)
        message = exc.detail if isinstance(exc, UserFacingError) else None
        return error_page(request, exc.status_code, message, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        in_path = any(error["loc"][:1] == ("path",) for error in exc.errors())
        return error_page(request, 404 if in_path else 422)
