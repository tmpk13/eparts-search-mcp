from __future__ import annotations

import pytest

from digi_mouse_search.config import load_config


def test_defaults_are_conservative():
    config = load_config()
    assert config.digikey.rate_limit.per_day == 1000
    assert config.mouser.rate_limit.per_day == 1000
    assert config.configured_sources() == []


def test_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MOUSER_API_KEY", "key")

    config = load_config()
    assert config.configured_sources() == ["digikey", "mouser"]


def test_rate_limits_are_overridable_per_provider(monkeypatch):
    monkeypatch.setenv("DIGIKEY_RATE_PER_DAY", "250")
    monkeypatch.setenv("DIGIKEY_RATE_PER_MINUTE", "10")
    monkeypatch.setenv("MOUSER_RATE_BURST", "7")

    config = load_config()
    assert config.digikey.rate_limit.per_day == 250
    assert config.digikey.rate_limit.per_minute == 10.0
    assert config.mouser.rate_limit.burst == 7
    # An untouched provider keeps its default.
    assert config.mouser.rate_limit.per_day == 1000


@pytest.mark.parametrize("value", ["none", "off", "unlimited", "0"])
def test_a_limit_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("MOUSER_RATE_PER_DAY", value)
    assert load_config().mouser.rate_limit.per_day is None


def test_an_invalid_limit_is_rejected(monkeypatch):
    monkeypatch.setenv("MOUSER_RATE_PER_DAY", "lots")
    with pytest.raises(ValueError, match="must be a number"):
        load_config()


def test_toml_file_configures_everything(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [cache]
        ttl_seconds = 120

        [providers.digikey]
        client_id = "file-id"
        client_secret = "file-secret"
        currency = "GBP"

        [providers.digikey.rate_limit]
        per_day = 42
        per_minute = 6

        [providers.mouser]
        api_key = "file-key"
        """,
        encoding="ascii",
    )
    path.chmod(0o600)

    config = load_config(str(path))
    assert config.digikey.client_id == "file-id"
    assert config.digikey.currency == "GBP"
    assert config.digikey.rate_limit.per_day == 42
    assert config.mouser.api_key == "file-key"
    assert config.cache_ttl_seconds == 120


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [providers.mouser]
        api_key = "file-key"

        [providers.mouser.rate_limit]
        per_day = 42
        """,
        encoding="ascii",
    )
    path.chmod(0o600)
    monkeypatch.setenv("DMS_CONFIG", str(path))
    monkeypatch.setenv("MOUSER_API_KEY", "env-key")
    monkeypatch.setenv("MOUSER_RATE_PER_DAY", "99")

    config = load_config()
    assert config.mouser.api_key == "env-key"
    assert config.mouser.rate_limit.per_day == 99


def test_missing_config_file_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DMS_CONFIG", str(tmp_path / "absent.toml"))
    with pytest.raises(FileNotFoundError):
        load_config()


def test_default_config_path_is_read_when_dms_config_is_unset(monkeypatch, tmp_path):
    home = tmp_path / "cfg"
    path = home / "digikey-search-mcp" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
        [providers.digikey]
        client_id = "file-id"
        client_secret = "file-secret"
        """,
        encoding="ascii",
    )
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))

    config = load_config()
    assert config.digikey.client_id == "file-id"
    assert config.digikey.client_secret == "file-secret"
    assert config.configured_sources() == ["digikey"]


def test_missing_default_config_path_is_not_an_error(monkeypatch, tmp_path):
    # An absent default file is fine; only an explicit DMS_CONFIG must exist.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert load_config().configured_sources() == []


def test_world_readable_config_file_warns(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[providers.digikey]\nclient_id = \"x\"\n", encoding="ascii")
    path.chmod(0o644)
    monkeypatch.setenv("DMS_CONFIG", str(path))
    with pytest.warns(UserWarning, match="chmod 600"):
        load_config()


def test_locked_down_config_file_does_not_warn(monkeypatch, tmp_path, recwarn):
    path = tmp_path / "config.toml"
    path.write_text("[providers.digikey]\nclient_id = \"x\"\n", encoding="ascii")
    path.chmod(0o600)
    monkeypatch.setenv("DMS_CONFIG", str(path))
    load_config()
    assert not recwarn.list


def test_sandbox_switches_the_host(monkeypatch):
    monkeypatch.setenv("DIGIKEY_SANDBOX", "true")
    assert load_config().digikey.base_url == "https://sandbox-api.digikey.com"
    monkeypatch.setenv("DIGIKEY_SANDBOX", "false")
    assert load_config().digikey.base_url == "https://api.digikey.com"
