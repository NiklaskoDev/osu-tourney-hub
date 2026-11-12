"""Exception for failures whose message is written for the person who sees it."""

from fastapi import HTTPException


class UserFacingError(HTTPException):
    """An HTTP error whose `detail` is safe and useful to show on the error page.

    Any other HTTPException shows a generic text for its status, so framework internals never reach
    the user.
    """

    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
