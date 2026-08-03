"""Shared rate limiter.

Lives in its own module so routers and the app factory can both reach it
without importing each other.

Keyed on client IP, which is the right unit for a public ticket-submission
endpoint. Behind a proxy the limiter needs the forwarded address, so a real
deployment adds `ProxyHeadersMiddleware` — otherwise every request appears to
come from the load balancer and shares one bucket.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

limiter = Limiter(key_func=get_remote_address)


def ticket_rate_limit() -> str:
    return get_settings().rate_limit_tickets
