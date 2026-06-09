from __future__ import annotations


def test_discovery_error_is_parts_lookup_error():
    from parts_lookup.domain.errors import DiscoveryError, PartsLookupError

    assert issubclass(DiscoveryError, PartsLookupError)


def test_settings_have_discovery_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    from parts_lookup.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.sram_base_url == "https://www.sram.com"
    assert s.sram_docs_base_url == "https://docs.sram.com"
    assert s.discovery_max_concurrency >= 1
    assert s.discovery_request_delay_seconds >= 0
    assert "discovery" in s.discovery_cache_dir or s.discovery_cache_dir.endswith("cache")
    assert s.discovery_user_agent  # non-empty
