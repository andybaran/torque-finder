"""Liveness vs deep-readiness probes (#33 sub-piece b3).

``/healthz`` is static; ``/readyz`` reflects recent extraction-upstream health
from a cheap cached signal (no live API call). DB-free via dependency override.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from parts_lookup.api.dependencies import get_claude_extractor
from parts_lookup.api.main import create_app
from parts_lookup.config import Settings


class _StubExtractor:
    def __init__(self, healthy: bool) -> None:
        self._healthy = healthy

    @property
    def upstream_healthy(self) -> bool:
        return self._healthy


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://stub/stub",
        stub_external_apis=True,
        _env_file=None,
    )


def _client(healthy: bool) -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[get_claude_extractor] = lambda: _StubExtractor(healthy)
    return TestClient(app, raise_server_exceptions=False)


def test_healthz_static_200_regardless_of_upstream() -> None:
    # Even when the upstream is down, liveness stays 200 (don't get restarted).
    resp = _client(healthy=False).get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_200_when_upstream_healthy() -> None:
    resp = _client(healthy=True).get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["extraction_upstream_healthy"] is True


def test_readyz_503_when_upstream_unhealthy() -> None:
    resp = _client(healthy=False).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["extraction_upstream_healthy"] is False


def test_readyz_flips_unhealthy_to_healthy_while_healthz_stays_200() -> None:
    down = _client(healthy=False)
    assert down.get("/readyz").status_code == 503
    assert down.get("/healthz").status_code == 200

    up = _client(healthy=True)
    assert up.get("/readyz").status_code == 200
    assert up.get("/healthz").status_code == 200
