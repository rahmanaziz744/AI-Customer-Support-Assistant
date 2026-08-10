"""Shared-secret gate for the destructive routes on a public demo.

The demo is deliberately open: submitting a ticket and approving the drafted
reply are the whole point, and putting a password in front of them would hide
the human-in-the-loop flow this exists to show. What it should not offer a
passer-by is a way to delete the seeded data everyone else is looking at.

So this guards deletion only. When `DEMO_ADMIN_TOKEN` is unset the dependency
is a no-op, which keeps local development and the test suite unchanged.
"""

import secrets
from typing import Annotated

from fastapi import Header

from app.core.config import get_settings
from app.core.errors import UnauthorizedError


def require_demo_token(
    x_demo_token: Annotated[str | None, Header()] = None,
) -> None:
    """Require `X-Demo-Token` to match the configured secret, if one is set."""
    expected = get_settings().demo_admin_token.get_secret_value()
    if not expected:
        return

    # compare_digest over `==` so a wrong token cannot be narrowed down by
    # timing the response.
    if not x_demo_token or not secrets.compare_digest(x_demo_token, expected):
        raise UnauthorizedError("This action requires a valid X-Demo-Token header.")
