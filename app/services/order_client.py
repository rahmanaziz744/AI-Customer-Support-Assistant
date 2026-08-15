"""HTTP client for the order system.

The agent talks to orders over HTTP rather than reaching into the database
directly, even though the mock is mounted in this same app. That keeps the
seam honest: pointing `ORDER_API_BASE_URL` at a real commerce backend is a
configuration change, not a rewrite.
"""

from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class OrderNotFound(LookupError):
    """No order exists with the given reference."""


class OrderAPIError(RuntimeError):
    """The order system rejected the request or was unreachable."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


_default_transport: httpx.AsyncBaseTransport | None = None


def set_default_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Override (or with None, restore) the transport every client uses.

    The graph nodes construct `OrderClient()` with no arguments, so a per-call
    `transport=` cannot reach them. This is the seam — the same shape as
    `app.agents.llm.set_model_factory` — that lets the test suite bind the
    client to the ASGI app instead of a socket, so the whole graph runs without
    anything listening on a port.
    """
    global _default_transport
    _default_transport = transport


class OrderClient:
    """Thin async wrapper over the order endpoints.

    `transport` exists so tests can bind the client straight to the ASGI app
    without binding a socket; `set_default_transport` does the same for the
    clients that nodes construct themselves.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.order_api_base_url).rstrip("/")
        self._transport = transport if transport is not None else _default_transport
        self._timeout = timeout or settings.order_api_timeout_seconds

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"/mock-api/orders{path}"
        try:
            async with self._client() as client:
                response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("order_api_unreachable", url=url, error=str(exc))
            raise OrderAPIError(f"Order system unreachable: {exc}") from exc

        if response.status_code == 404:
            raise OrderNotFound(f"Order not found at {path}")

        if response.status_code >= 400:
            payload = _safe_json(response)
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise OrderAPIError(
                error.get("message") or f"Order system returned {response.status_code}",
                status_code=response.status_code,
                detail=error.get("detail"),
            )

        return response.json()

    async def get_order(self, order_ref: str) -> dict[str, Any]:
        return await self._request("GET", f"/{order_ref}")

    async def list_actions(self, order_ref: str) -> list[dict[str, Any]]:
        """Refunds and replacements already executed against this order."""
        result = await self._request("GET", f"/{order_ref}/actions")
        return result if isinstance(result, list) else []

    async def issue_refund(
        self,
        order_ref: str,
        amount: Decimal | float | str,
        reason: str,
        *,
        ticket_id: str | None = None,
        approved_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/{order_ref}/refund",
            json={
                "amount": str(amount),
                "reason": reason,
                "ticket_id": ticket_id,
                "approved_by": approved_by,
                "idempotency_key": idempotency_key,
            },
        )

    async def issue_replacement(
        self,
        order_ref: str,
        reason: str,
        *,
        ticket_id: str | None = None,
        approved_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/{order_ref}/replacement",
            json={
                "reason": reason,
                "ticket_id": ticket_id,
                "approved_by": approved_by,
                "idempotency_key": idempotency_key,
            },
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}
