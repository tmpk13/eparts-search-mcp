"""Mouser Search API V2 adapter."""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..cache import Cache
from ..config import MouserConfig
from ..models import Part, PriceBreak
from ..ratelimit import RateLimiter
from .base import Provider, ProviderError, absolute_url, clean_text

_LEADING_NUMBER = re.compile(r"[\d,]+")
_PRICE_CHARS = re.compile(r"[^\d.,-]")


def parse_price(raw: Any) -> float | None:
    """Parse a localized price string such as "$1.23", "1,23 EUR" or "1.234,50".

    Prices arrive as display strings rather than numbers, and the separator
    convention follows the account's locale, so the last separator present is
    treated as the decimal point.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = _PRICE_CHARS.sub("", str(raw)).strip()
    if not text:
        return None

    last_dot = text.rfind(".")
    last_comma = text.rfind(",")
    if last_dot == -1 and last_comma == -1:
        decimal_pos = -1
    else:
        decimal_pos = max(last_dot, last_comma)

    if decimal_pos == -1:
        cleaned = text
    else:
        # Digits after the final separator decide whether it is a decimal
        # point or a thousands grouping mark.
        fraction = text[decimal_pos + 1 :]
        if len(fraction) in (1, 2):
            cleaned = text[:decimal_pos].replace(",", "").replace(".", "") + "." + fraction
        else:
            cleaned = text.replace(",", "").replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_availability(raw: Any) -> int | None:
    """Pull the stock count out of strings like "1,234 In Stock"."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    match = _LEADING_NUMBER.search(str(raw))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


class MouserProvider(Provider):
    name = "mouser"

    def __init__(
        self,
        config: MouserConfig,
        client: httpx.AsyncClient,
        cache: Cache,
        limiter: RateLimiter,
        cache_ttl: int,
    ) -> None:
        super().__init__(client, cache, limiter, cache_ttl)
        self.config = config

    @property
    def configured(self) -> bool:
        return self.config.configured

    def _parse_response(self, response: httpx.Response) -> Any:
        """Decode a response, treating the body's error list as authoritative.

        The API answers with HTTP 200 even for a rejected key or a malformed
        request, so status code alone would let failures through as empty
        results.
        """
        if response.status_code >= 400:
            raise ProviderError(f"mouser: HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("mouser: response was not JSON") from exc

        errors = data.get("Errors") or []
        if errors:
            messages = "; ".join(
                f"{item.get('PropertyName') or item.get('Code') or 'error'}: "
                f"{item.get('Message') or 'unknown'}"
                for item in errors
            )
            raise ProviderError(f"mouser: {messages}")
        return data

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/api/v2/search/{path}"

    def _params(self) -> dict[str, str]:
        return {"apiKey": self.config.api_key or ""}

    async def search(
        self,
        keyword: str,
        limit: int = 10,
        manufacturer: str | None = None,
        in_stock_only: bool = False,
    ) -> list[Part]:
        if not self.configured:
            raise ProviderError("mouser: MOUSER_API_KEY is not set")

        records = max(1, min(int(limit), 50))
        search_options = "InStock" if in_stock_only else "None"

        if manufacturer:
            path = "keywordandmanufacturer"
            body: dict[str, Any] = {
                "SearchByKeywordMfrNameRequest": {
                    "keyword": keyword,
                    "manufacturerName": manufacturer,
                    "pageNumber": 0,
                    "pageSize": records,
                    "searchOptions": search_options,
                }
            }
        else:
            path = "keyword"
            body = {
                "SearchByKeywordRequest": {
                    "keyword": keyword,
                    "records": records,
                    "startingRecord": 0,
                    "searchOptions": search_options,
                }
            }

        data = await self._cached_request(
            f"search:{path}",
            {"body": body},
            {
                "method": "POST",
                "url": self._url(path),
                "params": self._params(),
                "json": body,
                "headers": {"Content-Type": "application/json", "Accept": "application/json"},
            },
        )

        results = (data.get("SearchResults") or {}).get("Parts") or []
        parts = [self._to_part(entry) for entry in results]
        return [part for part in parts if part is not None][:limit]

    async def details(self, part_number: str) -> Part | None:
        if not self.configured:
            raise ProviderError("mouser: MOUSER_API_KEY is not set")

        body = {"SearchByPartRequest": {"mouserPartNumber": part_number}}
        data = await self._cached_request(
            "details",
            {"body": body},
            {
                "method": "POST",
                "url": self._url("partnumber"),
                "params": self._params(),
                "json": body,
                "headers": {"Content-Type": "application/json", "Accept": "application/json"},
            },
        )
        results = (data.get("SearchResults") or {}).get("Parts") or []
        if not results:
            return None
        return self._to_part(results[0])

    def _to_part(self, entry: dict[str, Any]) -> Part | None:
        mpn = clean_text(entry.get("ManufacturerPartNumber"), 120)
        if not mpn:
            return None

        price_breaks = []
        for item in entry.get("PriceBreaks") or []:
            price = parse_price(item.get("Price"))
            quantity = item.get("Quantity")
            if price is None or quantity is None:
                continue
            price_breaks.append(
                PriceBreak(
                    quantity=int(quantity),
                    unit_price=price,
                    currency=clean_text(item.get("Currency"), 8) or "USD",
                )
            )
        price_breaks.sort(key=lambda b: b.quantity)

        specs = {}
        category = clean_text(entry.get("Category"), 60)
        if category:
            specs["Category"] = category
        for attribute in entry.get("ProductAttributes") or []:
            label = clean_text(attribute.get("AttributeName"), 60)
            value = clean_text(attribute.get("AttributeValue"), 80)
            if label and value:
                specs[label] = value

        return Part(
            source="mouser",
            mpn=mpn,
            manufacturer=clean_text(entry.get("Manufacturer"), 80),
            description=clean_text(entry.get("Description"), 200),
            distributor_pn=clean_text(entry.get("MouserPartNumber"), 60) or mpn,
            product_url=absolute_url(entry.get("ProductDetailUrl")),
            datasheet_url=absolute_url(entry.get("DataSheetUrl")),
            stock=parse_availability(entry.get("Availability")),
            min_order_qty=_as_int(entry.get("Min")),
            lifecycle=clean_text(entry.get("LifecycleStatus"), 40) or None,
            # Search results describe a catalog entry, not a packaging option,
            # so there is no per-packaging field to report here.
            packaging=None,
            price_breaks=price_breaks,
            specs=specs,
        )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
