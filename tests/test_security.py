"""The shared-secret gate on the destructive route."""

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.errors import UnauthorizedError
from app.core.security import require_demo_token


@pytest.fixture
def token(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "demo_admin_token", SecretStr(value))

    return _set


class TestRequireDemoToken:
    def test_unset_token_leaves_the_route_open(self, token):
        """The default: local dev and the test suite are unaffected."""
        token("")
        assert require_demo_token(None) is None
        assert require_demo_token("anything") is None

    def test_correct_token_passes(self, token):
        token("s3cret")
        assert require_demo_token("s3cret") is None

    def test_missing_header_rejected(self, token):
        token("s3cret")
        with pytest.raises(UnauthorizedError) as exc:
            require_demo_token(None)
        assert exc.value.status_code == 401
        assert exc.value.code == "unauthorized"

    def test_wrong_token_rejected(self, token):
        token("s3cret")
        with pytest.raises(UnauthorizedError):
            require_demo_token("guess")

    def test_empty_header_rejected(self, token):
        token("s3cret")
        with pytest.raises(UnauthorizedError):
            require_demo_token("")

    def test_prefix_of_token_rejected(self, token):
        token("s3cret")
        with pytest.raises(UnauthorizedError):
            require_demo_token("s3c")
