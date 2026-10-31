"""auth.rate_limits and auth.failed_auth_limit are validated at load time."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grimoire.config.settings import AuthConfig


class TestAuthConfigValidation:
    def test_defaults(self) -> None:
        cfg = AuthConfig()
        assert cfg.rate_limits["agt"] == "600/minute"
        assert cfg.failed_auth_limit == "10/minute"

    def test_rejects_unknown_tier(self) -> None:
        with pytest.raises(ValidationError, match="unknown tier"):
            AuthConfig(rate_limits={"admin": "10/minute"})

    def test_rejects_unparseable_limit(self) -> None:
        with pytest.raises(ValidationError, match="not a rate limit"):
            AuthConfig(rate_limits={"agt": "lots"})

    def test_rejects_unparseable_failed_auth_limit(self) -> None:
        with pytest.raises(ValidationError, match="not a rate limit"):
            AuthConfig(failed_auth_limit="ten")
