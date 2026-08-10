"""Shared rate limiter.

Lives in its own module so routers and the app factory can both reach it
without importing each other.

Keyed on client IP, which is the right unit for a public ticket-submission
endpoint. Behind a proxy that address has to come from `X-Forwarded-For`, and
reading that header naively is worse than not rate limiting at all — see
`client_ip` for why the entry is picked from the right.

Counters are held in memory, so the limits are per-process. That is accurate
only while exactly one API process is running, which is the deployed topology
(a single container on a single instance). Running more than one replica
multiplies every limit by the replica count; that is the point at which this
needs a shared backend (`Limiter(storage_uri=...)`).
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings


def client_ip(request: Request) -> str:
    """The caller's address, trusting exactly as many proxy hops as configured.

    `X-Forwarded-For` is append-only and the client writes first: a caller can
    open with any value it likes, so the *leftmost* entries are attacker
    controlled and picking `parts[0]` hands every visitor a limit bucket of
    their own choosing. The only trustworthy entries are the ones our own
    proxies appended, and those are at the right-hand end.

    `TRUSTED_PROXY_HOPS` says how many proxies append to the header before the
    request arrives here, and we count in from the right by exactly that many.
    For the deployed Caddy -> nginx chain that is 2: Caddy appends the real
    client address, nginx then appends Caddy's. A request forged as
    `X-Forwarded-For: 1.2.3.4` therefore arrives as `1.2.3.4, <client>, <caddy>`
    and still keys on `<client>`.

    Falls back to the socket peer when the header is absent or shorter than the
    configured hop count — a misconfiguration should collapse everyone into the
    proxy's bucket rather than silently trust forged input.
    """
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return get_remote_address(request)


def _default_limits() -> list[str]:
    configured = get_settings().rate_limit_default.strip()
    return [configured] if configured else []


# Routes carrying their own `@limiter.limit(...)` ignore these defaults;
# slowapi's decorator overrides them rather than stacking.
limiter = Limiter(key_func=client_ip, default_limits=_default_limits())


def ticket_rate_limit() -> str:
    return get_settings().rate_limit_tickets


def expensive_rate_limit() -> str:
    """Limit for routes that trigger model work on an existing ticket."""
    return get_settings().rate_limit_expensive
