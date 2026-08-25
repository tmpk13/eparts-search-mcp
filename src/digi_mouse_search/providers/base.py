"""Shared plumbing for distributor adapters: caching, budgeting, transport."""

from __future__ import annotations

import abc
from typing import Any

import httpx

from ..cache import Cache, make_key
from ..models import Part
from ..ratelimit import RateLimiter


class ProviderError(RuntimeError):
    """A provider-level failure that should be reported, not raised to the client."""


class ProviderNotFound(ProviderError):
    """The distributor has no record of the requested part.

    Distinct from a failure: a lookup that correctly establishes a part does
    not exist has succeeded, and must not be reported as a broken source.
    """


class Provider(abc.ABC):
    """One distributor's API, exposed as normalized parts."""

    name: str

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: Cache,
        limiter: RateLimiter,
        cache_ttl: int,
    ) -> None:
        self._client = client
        self._cache = cache
        self._limiter = limiter
        self._cache_ttl = cache_ttl

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True when credentials are present and the provider may be called."""

    @abc.abstractmethod
    async def search(
        self,
        keyword: str,
        limit: int = 10,
        manufacturer: str | None = None,
        in_stock_only: bool = False,
    ) -> list[Part]:
        """Keyword search returning normalized parts."""

    @abc.abstractmethod
    async def details(self, part_number: str) -> Part | None:
        """Look up one part by manufacturer or distributor part number."""

    async def _cached_request(
        self,
        operation: str,
        cache_payload: Any,
        request: dict[str, Any],
        ttl: int | None = None,
    ) -> Any:
        """Serve `operation` from cache, else spend budget and call the API.

        The cache is consulted before the limiter on purpose: a repeated query
        must not consume a request from the daily quota.
        """
        key = make_key(self.name, operation, cache_payload)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        await self._limiter.acquire()
        try:
            response = await self._client.request(**request)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: request failed: {exc}") from exc

        data = self._parse_response(response)
        self._cache.set(key, self.name, data, self._cache_ttl if ttl is None else ttl)
        return data

    def _parse_response(self, response: httpx.Response) -> Any:
        """Turn a raw response into JSON, raising ProviderError on failure.

        Overridden where a distributor signals errors in the body rather than
        in the status code.
        """
        if response.status_code == 404:
            raise ProviderNotFound(f"{self.name}: no record of the requested part")
        if response.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: response was not JSON") from exc

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "rate_limit": self._limiter.describe(),
        }


def absolute_url(value: Any) -> str | None:
    """Give a protocol-relative URL a scheme.

    Some asset URLs arrive as //host/path, which is not usable by anything
    that receives the value on its own rather than resolving it against a
    page.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("//"):
        return f"https:{text}"
    return text


def clean_text(value: Any, limit: int = 300) -> str:
    """Collapse whitespace and clamp length, keeping tool output compact."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
