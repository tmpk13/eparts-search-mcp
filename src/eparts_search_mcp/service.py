"""Orchestration across providers.

Every fan-out is partial-failure tolerant: one distributor being down, out of
quota or unconfigured must never hide results from the other.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .cache import Cache
from .config import Config, load_config
from .models import MergedPart, Part, SearchResult, SourceError
from .normalize import merge_parts
from .providers import DigiKeyProvider, MouserProvider, Provider, ProviderError
from .ratelimit import RateLimiter, RateLimitExceeded

ALL_SOURCES = ("digikey", "mouser")


class SearchService:
    """Owns the HTTP client, cache, limiters and provider adapters."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        self.cache = Cache(self.config.cache_path, self.config.cache_ttl_seconds)
        self._client = httpx.AsyncClient(
            timeout=self.config.request_timeout_seconds,
            headers={"User-Agent": "eparts-search-mcp/0.1"},
            follow_redirects=True,
        )
        self.limiters = {
            "digikey": RateLimiter("digikey", self.config.digikey.rate_limit, self.cache),
            "mouser": RateLimiter("mouser", self.config.mouser.rate_limit, self.cache),
        }
        self.providers: dict[str, Provider] = {
            "digikey": DigiKeyProvider(
                self.config.digikey,
                self._client,
                self.cache,
                self.limiters["digikey"],
                self.config.cache_ttl_seconds,
            ),
            "mouser": MouserProvider(
                self.config.mouser,
                self._client,
                self.cache,
                self.limiters["mouser"],
                self.config.cache_ttl_seconds,
            ),
        }

    def resolve_sources(self, sources: list[str] | None) -> tuple[list[str], list[SourceError]]:
        """Turn a requested source list into usable names plus explained rejects.

        Omitting the list means "every configured source", which is what makes
        a combined search the default while still allowing a single-source
        query.
        """
        errors: list[SourceError] = []
        if not sources:
            usable = [name for name in ALL_SOURCES if self.providers[name].configured]
            if not usable:
                errors.append(
                    SourceError(
                        source="*",
                        error="no source is configured; set DIGIKEY_CLIENT_ID with "
                        "DIGIKEY_CLIENT_SECRET, or MOUSER_API_KEY",
                    )
                )
            return usable, errors

        usable = []
        for raw in sources:
            name = raw.strip().lower()
            provider = self.providers.get(name)
            if provider is None:
                errors.append(
                    SourceError(source=name, error=f"unknown source, expected one of {ALL_SOURCES}")
                )
            elif not provider.configured:
                errors.append(SourceError(source=name, error="credentials are not configured"))
            else:
                usable.append(name)
        return usable, errors

    async def search(
        self,
        keyword: str,
        sources: list[str] | None = None,
        limit: int = 10,
        manufacturer: str | None = None,
        in_stock_only: bool = False,
        merge: bool = True,
    ) -> SearchResult:
        names, errors = self.resolve_sources(sources)
        result = SearchResult(
            query=keyword, sources_searched=names, merged=merge, errors=list(errors)
        )
        if not names:
            return result

        outcomes = await asyncio.gather(
            *(
                self.providers[name].search(
                    keyword, limit=limit, manufacturer=manufacturer, in_stock_only=in_stock_only
                )
                for name in names
            ),
            return_exceptions=True,
        )

        collected: list[Part] = []
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                result.errors.append(SourceError(source=name, error=_describe(outcome)))
                result.by_source[name] = []
                continue
            result.by_source[name] = outcome
            collected.extend(outcome)

        if merge:
            result.parts = merge_parts(collected)
        return result

    async def details(
        self, part_number: str, sources: list[str] | None = None
    ) -> tuple[list[Part], list[SourceError]]:
        names, errors = self.resolve_sources(sources)
        if not names:
            return [], errors

        outcomes = await asyncio.gather(
            *(self.providers[name].details(part_number) for name in names),
            return_exceptions=True,
        )

        parts: list[Part] = []
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors.append(SourceError(source=name, error=_describe(outcome)))
            elif outcome is not None:
                parts.append(outcome)
        return parts, errors

    def status(self) -> dict[str, Any]:
        return {
            "sources": {name: provider.describe() for name, provider in self.providers.items()},
            "cache": {
                "path": str(self.config.cache_path),
                "ttl_seconds": self.config.cache_ttl_seconds,
            },
        }

    async def aclose(self) -> None:
        await self._client.aclose()
        self.cache.close()


def _describe(error: BaseException) -> str:
    if isinstance(error, (ProviderError, RateLimitExceeded)):
        return str(error)
    return f"{type(error).__name__}: {error}"


def best_offer(part: MergedPart, quantity: int = 1) -> Part | None:
    """Cheapest offer at a given quantity, ignoring offers without a price."""
    priced: list[tuple[float, Part]] = []
    for offer in part.offers:
        price = offer.unit_price_at(quantity)
        if price is not None:
            priced.append((price, offer))
    if not priced:
        return None
    return min(priced, key=lambda pair: pair[0])[1]
