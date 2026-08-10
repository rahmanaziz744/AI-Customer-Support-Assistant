"""Domain exceptions and the handlers that turn them into HTTP responses.

Every error response shares one envelope — `{"error": {"code", "message", "detail"}}`
— so the UI has a single shape to parse.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, client-visible failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    """The request is valid but the resource is in the wrong state for it."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"


class AgentError(AppError):
    """The agent graph failed in a way the caller should hear about."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "agent_error"


class GuardrailError(AppError):
    """A safety guardrail blocked the request outright."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "guardrail_blocked"


class UnauthorizedError(AppError):
    """The caller did not present the credential a protected route requires."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class BudgetExceededError(AppError):
    """The rolling model-spend ceiling is used up; no new agent work starts.

    429 rather than 503: the condition is caller-visible, temporary, and clears
    on its own as spend ages out of the window — the same shape as a rate limit.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "budget_exceeded"


def _envelope(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_envelope("validation_error", "Request validation failed", exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback but never leak internals to the client.
        logger.exception(
            "unhandled_exception", path=request.url.path, error=str(exc), kind=type(exc).__name__
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", "An unexpected error occurred"),
        )
