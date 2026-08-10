"""FastAPI application entrypoint."""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

from app import __version__
from app.api import health, mock_orders, tickets
from app.core.budget import budget_status
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import limiter

logger = get_logger(__name__)


async def _budget_snapshot_loop(interval: float) -> None:
    """Emit one `budget_snapshot` log line per interval.

    With `LOG_FORMAT=json` these are structured records, so a CloudWatch Logs
    metric filter on `$.daily_spend_usd` turns model spend into a graphable
    metric that alarms can watch — without a boto3 dependency, an IAM policy
    for PutMetricData, or a per-call metric charge.

    Reads uncached: this is the one caller that wants a true current figure
    rather than a cheap one, and it runs once a minute.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_scope() as db:
                status = await budget_status(db, use_cache=False)
            logger.info(
                "budget_snapshot",
                daily_spend_usd=float(status.spend_usd),
                budget_usd=float(status.limit_usd),
                budget_ratio=round(status.ratio, 4),
                budget_enforced=status.enforced,
                budget_exhausted=status.exhausted,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A snapshot is diagnostics; losing one must not take down the app.
            logger.warning("budget_snapshot_failed", error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    settings = get_settings()
    if settings.langsmith_tracing and settings.langsmith_api_key:
        # LangChain reads these from the environment at call time.
        import os

        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        logger.info("langsmith_enabled", project=settings.langsmith_project)

    # Create the LangGraph checkpoint tables before serving traffic. Doing this
    # lazily inside a request deadlocks on a fresh database — see
    # app/agents/graph.ensure_checkpointer_schema.
    try:
        from app.agents.graph import ensure_checkpointer_schema

        await ensure_checkpointer_schema()
    except Exception as exc:  # pragma: no cover - startup diagnostics
        logger.error("checkpointer_schema_failed", error=str(exc))

    snapshot: asyncio.Task | None = None
    if settings.budget_snapshot_seconds > 0:
        snapshot = asyncio.create_task(
            _budget_snapshot_loop(settings.budget_snapshot_seconds)
        )

    logger.info(
        "app_startup",
        version=__version__,
        model=settings.agent_model,
        daily_budget_usd=settings.daily_budget_usd,
        max_concurrent_runs=settings.max_concurrent_runs,
    )
    yield

    if snapshot is not None:
        snapshot.cancel()
        with suppress(asyncio.CancelledError):
            await snapshot
    logger.info("app_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AI Customer Support Agent",
        version=__version__,
        description=(
            "Triages support tickets against company policy: classify, retrieve policy, "
            "check refund eligibility, draft a reply, and route to a human for approval."
        ),
        lifespan=lifespan,
    )

    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "rate_limited",
                    "message": "Too many requests",
                    "detail": str(exc.detail),
                }
            },
        )

    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bind a request id to every log line emitted while handling the request."""
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed", method=request.method, path=request.url.path
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        # Health checks are polled constantly; logging them buries real traffic.
        if not request.url.path.startswith("/health"):
            logger.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        response.headers["x-request-id"] = request_id
        return response

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(tickets.router)
    app.include_router(mock_orders.router)

    return app


app = create_app()
