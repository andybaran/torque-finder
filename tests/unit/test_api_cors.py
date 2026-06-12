"""CORS middleware wiring + env-driven allowlist parsing.

These tests are dependency-free: ``stub_external_apis=True`` skips the Voyage /
Claude clients, a dummy ``database_url`` is never dialed (``/healthz`` opens no
session), and ``_env_file=None`` keeps the developer's real ``.env`` out of the
loop. They drive the *real* ``CORS_ALLOW_ORIGINS`` env var through
``EnvSettingsSource`` so the comma-separated parse path is actually exercised.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from parts_lookup.api.main import create_app
from parts_lookup.config import Settings

ALLOWED = "https://app.example.com"
DISALLOWED = "https://evil.example.com"


def _settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Drive the real env-var path so EnvSettingsSource (the layer that JSON-decodes
    # list fields) actually runs — this is what catches the NoDecode bug.
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", f"{ALLOWED},http://localhost:5173")
    return Settings(
        database_url="postgresql+asyncpg://test",
        stub_external_apis=True,
        _env_file=None,
    )


def test_env_var_parses_to_list(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_env(monkeypatch)
    # Proves the comma-separated CORS_ALLOW_ORIGINS env var parsed without SettingsError.
    assert settings.cors_allow_origins == [ALLOWED, "http://localhost:5173"]


def test_allowed_origin_gets_cors_header(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(settings=_settings_from_env(monkeypatch))
    with TestClient(app) as client:
        resp = client.get("/healthz", headers={"Origin": ALLOWED})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED


def test_disallowed_origin_has_no_cors_header(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(settings=_settings_from_env(monkeypatch))
    with TestClient(app) as client:
        resp = client.get("/healthz", headers={"Origin": DISALLOWED})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
