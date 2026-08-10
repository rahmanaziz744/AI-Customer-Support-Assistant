"""Client-IP derivation and limiter keying.

The interesting case is adversarial: `X-Forwarded-For` is written by the client
first and appended to by each proxy, so a caller can prepend anything. These
tests pin the property that matters — the bucket a request lands in cannot be
chosen by the caller.
"""

import httpx
import pytest
from fastapi import FastAPI, Request
from slowapi import Limiter
from starlette.requests import Request as StarletteRequest

from app.core.config import get_settings
from app.core.rate_limit import client_ip

PEER = "10.0.0.9"


def make_request(xff: str | None = None, peer: str = PEER) -> StarletteRequest:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 12345),
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
    )


@pytest.fixture
def hops(monkeypatch):
    """Set TRUSTED_PROXY_HOPS on the cached settings for one test."""

    def _set(value: int) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxy_hops", value)

    return _set


class TestClientIp:
    def test_no_proxy_uses_socket_peer(self, hops):
        hops(0)
        assert client_ip(make_request()) == PEER

    def test_no_proxy_ignores_forwarded_header(self, hops):
        """With hops=0 nothing in front is trusted, so the header is not read."""
        hops(0)
        assert client_ip(make_request("1.2.3.4")) == PEER

    def test_single_hop_reads_last_entry(self, hops):
        hops(1)
        assert client_ip(make_request("203.0.113.7")) == "203.0.113.7"

    def test_two_hops_reads_second_from_right(self, hops):
        """Caddy appends the client, nginx then appends Caddy."""
        hops(2)
        assert client_ip(make_request("203.0.113.7, 172.18.0.2")) == "203.0.113.7"

    def test_forged_prefix_is_ignored(self, hops):
        """The caller opened the header with a lie; it stays to the left."""
        hops(2)
        forged = make_request("1.2.3.4, 203.0.113.7, 172.18.0.2")
        assert client_ip(forged) == "203.0.113.7"

    def test_long_forged_prefix_still_ignored(self, hops):
        hops(2)
        req = make_request("9.9.9.9, 8.8.8.8, 7.7.7.7, 203.0.113.7, 172.18.0.2")
        assert client_ip(req) == "203.0.113.7"

    def test_header_shorter_than_hop_count_falls_back_to_peer(self, hops):
        """Misconfiguration collapses everyone into one bucket, never trusts input."""
        hops(2)
        assert client_ip(make_request("1.2.3.4")) == PEER

    def test_missing_header_falls_back_to_peer(self, hops):
        hops(2)
        assert client_ip(make_request()) == PEER

    def test_whitespace_and_empty_entries_tolerated(self, hops):
        hops(2)
        assert client_ip(make_request("  203.0.113.7 ,  , 172.18.0.2  ")) == "203.0.113.7"


@pytest.fixture
def limited_app() -> FastAPI:
    """A throwaway app exercising `client_ip` through real limiter machinery.

    A fresh `Limiter` per test rather than the shared module-level one: slowapi
    keys registered limits by endpoint name, so decorating a same-named route
    again would stack another limit onto the shared instance and every request
    would consume one unit per accumulated registration.
    """
    probe_limiter = Limiter(key_func=client_ip)
    app = FastAPI()
    app.state.limiter = probe_limiter

    @app.get("/probe")
    @probe_limiter.limit("3/minute")
    async def probe(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    return app


async def _get(app: FastAPI, xff: str) -> list[int]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return [
            (await c.get("/probe", headers={"x-forwarded-for": xff})).status_code
            for _ in range(5)
        ]


class TestLimiterKeying:
    async def test_limit_trips_after_allowance(self, limited_app, hops):
        hops(1)
        codes = await _get(limited_app, "198.51.100.1")
        assert codes == [200, 200, 200, 429, 429]

    async def test_separate_clients_get_separate_buckets(self, limited_app, hops):
        hops(1)
        assert await _get(limited_app, "198.51.100.2") == [200, 200, 200, 429, 429]
        # A different client is unaffected by the first one's exhausted bucket.
        assert await _get(limited_app, "198.51.100.3") == [200, 200, 200, 429, 429]

    async def test_forged_prefix_cannot_escape_the_bucket(self, limited_app, hops):
        """The whole point: rotating the forgeable part must not reset the count."""
        hops(2)
        transport = httpx.ASGITransport(app=limited_app)
        codes = []
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            for i in range(5):
                # Caller varies the leftmost entry every request; the trusted
                # tail (real client, then proxy) is what actually keys.
                xff = f"10.{i}.{i}.{i}, 198.51.100.4, 172.18.0.2"
                r = await c.get("/probe", headers={"x-forwarded-for": xff})
                codes.append(r.status_code)
        assert codes == [200, 200, 200, 429, 429]
