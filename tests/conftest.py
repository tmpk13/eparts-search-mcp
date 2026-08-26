from __future__ import annotations

import pytest

from eparts_search_mcp.config import (
    Config,
    DigiKeyConfig,
    MouserConfig,
    RateLimitConfig,
)
from eparts_search_mcp.service import SearchService

# Tests must never inherit a developer's real credentials or budget.
UNLIMITED = RateLimitConfig(per_second=None, per_minute=None, per_day=None, burst=1)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    # Point the default config lookup at an empty dir so tests never inherit a
    # developer's real ~/.config/eparts-search-mcp/config.toml.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg_config")))
    for name in (
        "DIGIKEY_CLIENT_ID",
        "DIGIKEY_CLIENT_SECRET",
        "DIGIKEY_SANDBOX",
        "DIGIKEY_LOCALE_SITE",
        "DIGIKEY_LOCALE_CURRENCY",
        "DIGIKEY_LOCALE_LANGUAGE",
        "DIGIKEY_RATE_PER_SECOND",
        "DIGIKEY_RATE_PER_MINUTE",
        "DIGIKEY_RATE_PER_DAY",
        "DIGIKEY_RATE_BURST",
        "DIGIKEY_RATE_MAX_WAIT",
        "MOUSER_API_KEY",
        "MOUSER_RATE_PER_SECOND",
        "MOUSER_RATE_PER_MINUTE",
        "MOUSER_RATE_PER_DAY",
        "MOUSER_RATE_BURST",
        "MOUSER_RATE_MAX_WAIT",
        "EPARTS_CONFIG",
        "EPARTS_CACHE_PATH",
        "EPARTS_CACHE_TTL",
        "EPARTS_REQUEST_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        digikey=DigiKeyConfig(
            client_id="test-id",
            client_secret="test-secret",
            rate_limit=UNLIMITED,
        ),
        mouser=MouserConfig(api_key="test-key", rate_limit=UNLIMITED),
        cache_path=tmp_path / "cache.sqlite3",
        # Caching off by default so each test controls its own request count.
        cache_ttl_seconds=0,
    )


@pytest.fixture
async def service(config):
    svc = SearchService(config)
    try:
        yield svc
    finally:
        await svc.aclose()
