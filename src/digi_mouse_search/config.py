"""Configuration loading.

Settings come from three layers, later layers winning:

1. Built-in defaults (conservative, sized to the distributors' free tiers).
2. A TOML file: the path in DMS_CONFIG, or, if that is unset, a default of
   $XDG_CONFIG_HOME/digikey-search-mcp/config.toml (i.e. usually
   ~/.config/digikey-search-mcp/config.toml). This is the intended home for
   credentials, keeping them out of the environment.
3. Environment variables.

Environment wins last so that an MCP client launching the server can override
a checked-in file without editing it.
"""

from __future__ import annotations

import os
import stat
import tomllib
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CACHE_TTL = 3600
DEFAULT_MAX_WAIT = 10.0


@dataclass(frozen=True)
class RateLimitConfig:
    """Request budget for one provider.

    Any window set to None is unlimited. per_second and per_minute are
    enforced by waiting; per_day is a hard stop, because waiting out a daily
    quota would outlive any caller that is willing to block.
    """

    per_second: float | None = None
    per_minute: float | None = None
    per_day: int | None = None
    burst: int = 1
    max_wait_seconds: float = DEFAULT_MAX_WAIT

    def describe(self) -> dict[str, Any]:
        return {
            "per_second": self.per_second,
            "per_minute": self.per_minute,
            "per_day": self.per_day,
            "burst": self.burst,
            "max_wait_seconds": self.max_wait_seconds,
        }


# Both distributors grant roughly 1000 calls/day on their standard free tier.
# The per-minute values are deliberately below what either documents, since
# tripping a distributor's own limiter is far more costly than waiting here.
DIGIKEY_DEFAULT_LIMITS = RateLimitConfig(per_second=2.0, per_minute=60.0, per_day=1000, burst=5)
MOUSER_DEFAULT_LIMITS = RateLimitConfig(per_second=1.0, per_minute=25.0, per_day=1000, burst=3)


@dataclass(frozen=True)
class DigiKeyConfig:
    client_id: str | None = None
    client_secret: str | None = None
    sandbox: bool = False
    site: str = "US"
    currency: str = "USD"
    language: str = "en"
    rate_limit: RateLimitConfig = field(default_factory=lambda: DIGIKEY_DEFAULT_LIMITS)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def base_url(self) -> str:
        host = "sandbox-api.digikey.com" if self.sandbox else "api.digikey.com"
        return f"https://{host}"


@dataclass(frozen=True)
class MouserConfig:
    api_key: str | None = None
    rate_limit: RateLimitConfig = field(default_factory=lambda: MOUSER_DEFAULT_LIMITS)
    base_url: str = "https://api.mouser.com"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Config:
    digikey: DigiKeyConfig = field(default_factory=DigiKeyConfig)
    mouser: MouserConfig = field(default_factory=MouserConfig)
    cache_path: Path = field(default_factory=lambda: _default_state_dir() / "cache.sqlite3")
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL
    request_timeout_seconds: float = 30.0

    def configured_sources(self) -> list[str]:
        names = []
        if self.digikey.configured:
            names.append("digikey")
        if self.mouser.configured:
            names.append("mouser")
        return names


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "digi-mouse-search"


def _default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "digikey-search-mcp" / "config.toml"


class _Unset:
    """Marker for "this layer said nothing", distinct from an explicit None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


def _env_str(name: str) -> str | _Unset:
    """Read a variable, treating absent and blank alike as "said nothing".

    Returning _UNSET rather than None matters: None is a value a lower layer
    may legitimately hold, and an unset variable must not overwrite a value
    the config file supplied.
    """
    value = os.environ.get(name)
    if value is None:
        return _UNSET
    value = value.strip()
    return value or _UNSET


def _optional(value: Any) -> Any:
    """Collapse _UNSET to None, for callers that want a plain optional."""
    return None if isinstance(value, _Unset) else value


def _env_float(name: str) -> float | None | _Unset:
    """Return a float, None for an explicit "none"/"unlimited", or _UNSET."""
    raw = _env_str(name)
    if isinstance(raw, _Unset):
        return _UNSET
    if raw.lower() in {"none", "off", "unlimited", "0"}:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number or 'none', got {raw!r}") from exc


def _env_int(name: str) -> int | None | _Unset:
    value = _env_float(name)
    if isinstance(value, float):
        return int(value)
    return value


def _env_bool(name: str) -> bool | _Unset:
    raw = _env_str(name)
    if isinstance(raw, _Unset):
        return _UNSET
    return raw.lower() in {"1", "true", "yes", "on"}


def _apply(current: Any, *candidates: Any) -> Any:
    """Return the last candidate that is neither _UNSET nor absent."""
    value = current
    for candidate in candidates:
        if candidate is not _UNSET:
            value = candidate
    return value


def _rate_limit_from(
    defaults: RateLimitConfig, table: dict[str, Any], prefix: str
) -> RateLimitConfig:
    """Merge a TOML rate_limit table then the matching env vars over defaults."""

    def from_table(key: str) -> Any:
        return table[key] if key in table else _UNSET

    per_second = _apply(
        defaults.per_second, from_table("per_second"), _env_float(f"{prefix}_RATE_PER_SECOND")
    )
    per_minute = _apply(
        defaults.per_minute, from_table("per_minute"), _env_float(f"{prefix}_RATE_PER_MINUTE")
    )
    per_day = _apply(defaults.per_day, from_table("per_day"), _env_int(f"{prefix}_RATE_PER_DAY"))
    burst = _apply(defaults.burst, from_table("burst"), _env_int(f"{prefix}_RATE_BURST"))
    max_wait = _apply(
        defaults.max_wait_seconds,
        from_table("max_wait_seconds"),
        _env_float(f"{prefix}_RATE_MAX_WAIT"),
    )
    return RateLimitConfig(
        per_second=per_second,
        per_minute=per_minute,
        per_day=per_day,
        burst=max(1, int(burst or 1)),
        max_wait_seconds=float(max_wait if max_wait is not None else 0.0),
    )


def _warn_if_world_readable(path: Path) -> None:
    """Warn (never fail) if a credential file is group- or world-accessible.

    The config file is the intended home for secrets, so lax permissions are
    worth flagging. POSIX only: the mode bits are meaningless on Windows.
    Warnings go to stderr, which keeps them clear of the stdio MCP channel.
    """
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.warn(
            f"config file {path} is accessible to group or others; "
            f"it may contain credentials. Restrict it with: chmod 600 {path}",
            stacklevel=2,
        )


def _load_toml(path_hint: str | None) -> dict[str, Any]:
    raw_path = path_hint or _optional(_env_str("DMS_CONFIG"))
    if raw_path:
        # An explicitly named file must exist: a typo should fail loudly rather
        # than silently fall back to defaults.
        path = Path(str(raw_path)).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
    else:
        # The default location is optional; its absence just means no file layer.
        path = _default_config_path()
        if not path.is_file():
            return {}
    _warn_if_world_readable(path)
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(config_path: str | None = None) -> Config:
    """Build the effective configuration from file plus environment."""
    doc = _load_toml(config_path)
    providers = doc.get("providers", {})
    cache_table = doc.get("cache", {})

    dk_table = providers.get("digikey", {})
    digikey = DigiKeyConfig(
        client_id=_apply(None, dk_table.get("client_id", _UNSET), _env_str("DIGIKEY_CLIENT_ID")),
        client_secret=_apply(
            None, dk_table.get("client_secret", _UNSET), _env_str("DIGIKEY_CLIENT_SECRET")
        ),
        sandbox=bool(
            _apply(False, dk_table.get("sandbox", _UNSET), _env_bool("DIGIKEY_SANDBOX")) or False
        ),
        site=_apply("US", dk_table.get("site", _UNSET), _env_str("DIGIKEY_LOCALE_SITE")) or "US",
        currency=_apply(
            "USD", dk_table.get("currency", _UNSET), _env_str("DIGIKEY_LOCALE_CURRENCY")
        )
        or "USD",
        language=_apply("en", dk_table.get("language", _UNSET), _env_str("DIGIKEY_LOCALE_LANGUAGE"))
        or "en",
        rate_limit=_rate_limit_from(
            DIGIKEY_DEFAULT_LIMITS, dk_table.get("rate_limit", {}), "DIGIKEY"
        ),
    )

    mo_table = providers.get("mouser", {})
    mouser = MouserConfig(
        api_key=_apply(None, mo_table.get("api_key", _UNSET), _env_str("MOUSER_API_KEY")),
        rate_limit=_rate_limit_from(
            MOUSER_DEFAULT_LIMITS, mo_table.get("rate_limit", {}), "MOUSER"
        ),
    )

    cache_path = _apply(None, cache_table.get("path", _UNSET), _env_str("DMS_CACHE_PATH"))
    cache_ttl = _apply(
        DEFAULT_CACHE_TTL, cache_table.get("ttl_seconds", _UNSET), _env_int("DMS_CACHE_TTL")
    )
    timeout = _apply(
        30.0, doc.get("request_timeout_seconds", _UNSET), _env_float("DMS_REQUEST_TIMEOUT")
    )

    base = Config(digikey=digikey, mouser=mouser)
    return replace(
        base,
        cache_path=Path(cache_path).expanduser() if cache_path else base.cache_path,
        cache_ttl_seconds=int(cache_ttl or 0),
        request_timeout_seconds=float(timeout or 30.0),
    )
