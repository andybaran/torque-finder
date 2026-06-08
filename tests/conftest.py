"""Shared pytest fixtures.

Tests must collect cleanly even with no env vars set: anything that needs
real credentials should call :func:`require_env` (or use the `settings`
fixture) and skip if a required variable is missing.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

import pytest

# Env vars required for any "live" test (integration + eval).
LIVE_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "DATABASE_URL",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def missing_env(names: Iterable[str]) -> list[str]:
    """Return the names that are unset or empty."""
    return [n for n in names if not os.environ.get(n)]


def require_env(names: Iterable[str]) -> None:
    """Skip the current test if any named env var is missing."""
    missing = missing_env(names)
    if missing:
        pytest.skip(f"missing required env vars: {', '.join(missing)}")


@pytest.fixture
def anyio_backend() -> str:
    """anyio uses asyncio under the hood for our tests."""
    return "asyncio"


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    """Build a Settings object from the environment, skipping if vars are missing.

    Returned untyped to avoid forcing a Settings import at collection time —
    pydantic-settings would raise if env vars are absent.
    """
    require_env(LIVE_ENV_VARS)
    from parts_lookup.config import Settings

    return Settings()  # type: ignore[call-arg]
