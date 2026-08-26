"""LCSC Open API adapter, Product module."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import urllib.parse
from typing import Any

import httpx

from ..cache import Cache
from ..config import LCSCConfig
from ..models import Part, PriceBreak
from ..normalize import mpn_key
from ..ratelimit import RateLimiter
from .base import Provider, ProviderError, absolute_url, clean_text

KEYWORD_SEARCH_PATH = "/rest/api/agent/product/v1/keywordsearch"

# Business status codes, carried in the body rather than the HTTP status.
CODE_OK = 200
CODE_RATE_LIMITED = 429
CODE_QUOTA_EXCEEDED = 430

# The keyword field is rejected above this length.
MAX_KEYWORD = 500

# A page may hold up to 500 products; a details lookup only needs enough
# results to find the exact match among fuzzier ones.
MAX_PAGE = 500
DETAILS_CANDIDATES = 10


def canonical_value(value: Any) -> str:
    """Render one parameter the way the signature expects.

    Nested values become compact JSON with object keys sorted, and a missing
    value becomes an empty string, so that both ends build the same string
    for the same request.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        ordered = {key: canonical_value(value[key]) for key in sorted(value)}
        return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(
            [canonical_value(item) for item in value], separators=(",", ":"), ensure_ascii=False
        )
    return str(value)


def sign(payload: dict[str, Any], key: str, secret: str, nonce: str, timestamp: str) -> str:
    """Digest the credentials together with the sorted, encoded query.

    The secret is an input to the hash but is never transmitted, so the
    request proves possession of it without carrying it. Parameter order is
    fixed by sorting, since a query's order is not preserved end to end.
    """
    material = f"key={key}&nonce={nonce}&secret={secret}&timestamp={timestamp}"
    query = "&".join(
        f"{urllib.parse.quote_plus(name)}={urllib.parse.quote_plus(canonical_value(value))}"
        for name, value in sorted(payload.items())
        if value is not None
    )
    if query:
        material = f"{material}&{query}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LCSCProvider(Provider):
    name = "lcsc"

    def __init__(
        self,
        config: LCSCConfig,
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

    def _headers(self, payload: dict[str, Any]) -> dict[str, str]:
        """Sign one request.

        The nonce and timestamp are per request, which is why they are built
        here rather than being part of the cached query: two identical
        searches carry different signatures but yield the same result.
        """
        nonce = secrets.token_hex(8)
        timestamp = str(int(time.time()))
        signature = sign(
            payload,
            key=self.config.key or "",
            secret=self.config.secret or "",
            nonce=nonce,
            timestamp=timestamp,
        )
        return {
            "key": self.config.key or "",
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": signature,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _parse_response(self, response: httpx.Response) -> Any:
        """Decode a response, treating the body's status code as authoritative.

        Transport failures arrive as an HTTP status, but a rejected signature
        or a spent quota comes back as HTTP 200 with the real outcome in the
        body, so the status code alone would let failures through as empty
        results.
        """
        if response.status_code >= 400:
            raise ProviderError(f"lcsc: HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("lcsc: response was not JSON") from exc
        if not isinstance(data, dict) or "code" not in data:
            raise ProviderError("lcsc: response did not carry a status code")

        code = _as_int(data.get("code"))
        if code != CODE_OK:
            message = clean_text(data.get("message"), 200) or "request rejected"
            if code in (CODE_RATE_LIMITED, CODE_QUOTA_EXCEEDED):
                # LCSC counts calls on its own side, where the local budget
                # cannot see them; saying so avoids a hunt through settings
                # that are not the cause.
                raise ProviderError(
                    f"lcsc: {message} (code {code}); this is LCSC's own limit, "
                    f"not the local request budget"
                )
            raise ProviderError(f"lcsc: {message} (code {code})")

        result = data.get("result")
        return {} if result is None else result

    async def search(
        self,
        keyword: str,
        limit: int = 10,
        manufacturer: str | None = None,
        in_stock_only: bool = False,
    ) -> list[Part]:
        if not self.configured:
            raise ProviderError("lcsc: LCSC_KEY and LCSC_SECRET are not set")

        # There is no manufacturer parameter: the keyword field itself accepts
        # a brand name combined with the rest of the query.
        query = f"{keyword} {manufacturer}" if manufacturer else keyword
        payload = {
            "keyword": query.strip()[:MAX_KEYWORD],
            "limit": str(max(1, min(int(limit), MAX_PAGE))),
            # Offsets are one based: the first page starts at one, not zero.
            "offset": "1",
            "returnInformation": "All",
            "currency": self.config.currency,
            "language": self.config.language,
            "inStockOnly": "true" if in_stock_only else "false",
        }

        result = await self._cached_request(
            "search",
            {"payload": payload, "base_url": self.config.base_url},
            {
                "method": "GET",
                "url": f"{self.config.base_url}{KEYWORD_SEARCH_PATH}",
                "params": payload,
                "headers": self._headers(payload),
            },
        )

        products = result.get("products") if isinstance(result, dict) else None
        parts = [self._to_part(entry) for entry in products or []]
        found = [part for part in parts if part is not None]

        if in_stock_only:
            # inStockOnly is documented as taking effect only when pricing
            # alone is requested, and both basic data and pricing are needed
            # here, so the filter is applied again on the results.
            found = [part for part in found if part.stock is None or part.stock > 0]
        if manufacturer:
            wanted = manufacturer.strip().lower()
            found = [part for part in found if wanted in part.manufacturer.lower()]
        return found[:limit]

    async def details(self, part_number: str) -> Part | None:
        if not self.configured:
            raise ProviderError("lcsc: LCSC_KEY and LCSC_SECRET are not set")

        # There is no per part endpoint. Keyword search resolves both an LCSC C
        # number and a manufacturer part number, but it also returns fuzzy
        # matches, so the exact one is picked out rather than trusting the
        # first hit.
        candidates = await self.search(part_number, limit=DETAILS_CANDIDATES)
        wanted = mpn_key(part_number)
        for part in candidates:
            if mpn_key(part.distributor_pn) == wanted or mpn_key(part.mpn) == wanted:
                return part
        return None

    def _to_part(self, entry: Any) -> Part | None:
        # Entries come straight from the response body, so the shape is
        # checked rather than assumed.
        if not isinstance(entry, dict):
            return None
        # The field table and the worked example disagree on the name of the
        # part number field, so both are accepted.
        mpn = clean_text(
            entry.get("manufacturerProductNumber") or entry.get("manufacturerProductCode"), 120
        )
        if not mpn:
            return None

        pricing = entry.get("productPrice")
        if isinstance(pricing, list):
            pricing = pricing[0] if pricing else {}
        if not isinstance(pricing, dict):
            pricing = {}

        currency = clean_text(pricing.get("currency"), 8) or self.config.currency
        price_breaks = []
        for tier in pricing.get("standardPricing") or []:
            quantity = _as_int(tier.get("breakQuantity"))
            unit_price = _as_float(tier.get("unitPrice"))
            if quantity is None or unit_price is None:
                continue
            price_breaks.append(
                PriceBreak(quantity=quantity, unit_price=unit_price, currency=currency)
            )
        price_breaks.sort(key=lambda b: b.quantity)

        stock = _as_int(entry.get("quantityAvailable"))
        if stock is None:
            stock = _as_int(pricing.get("quantityAvailable"))
        if stock is None and entry.get("isInStock") is False:
            stock = 0

        specs = _parameters(entry.get("productParameters"))
        category = _category_name(entry.get("category"))
        if category:
            specs.setdefault("Category", category)
        package = clean_text(entry.get("package"), 60)
        if package:
            specs.setdefault("Package", package)
        increment = _as_int(entry.get("incrementQuantity"))
        if increment and increment > 1:
            specs.setdefault("Order increment", str(increment))
        reel_fee = _as_float(entry.get("lcscReelFee") or pricing.get("lcscReelFee"))
        if reel_fee:
            # A per order surcharge rather than a per piece one, so it cannot
            # be folded into the price ladder that drives the comparison.
            specs.setdefault("Reel packaging fee", f"{reel_fee} {currency}")

        return Part(
            source="lcsc",
            mpn=mpn,
            manufacturer=_manufacturer_name(entry.get("manufacturer")),
            description=clean_text(entry.get("description"), 200),
            distributor_pn=clean_text(entry.get("lcscProductNumber"), 60) or mpn,
            product_url=absolute_url(entry.get("productDetailURL")),
            datasheet_url=absolute_url(entry.get("datasheetURL")),
            stock=stock,
            min_order_qty=_as_int(entry.get("minimumOrderQuantity")),
            # The catalog carries no lifecycle status.
            lifecycle=None,
            packaging=clean_text(entry.get("packageType"), 40) or None,
            price_breaks=price_breaks,
            specs=specs,
        )


def _manufacturer_name(raw: Any) -> str:
    """Pull a brand name out of the manufacturer field.

    It is documented as a list of objects keyed manufacturerName but arrives
    as a single object keyed name, so both shapes are accepted.
    """
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        return clean_text(raw.get("name") or raw.get("manufacturerName"), 80)
    return clean_text(raw, 80)


def _category_name(raw: Any) -> str:
    """Name the deepest catalog level present, which is the most specific one."""
    if isinstance(raw, str):
        return clean_text(raw, 60)
    if not isinstance(raw, dict):
        return ""
    for key in (
        "forthCatalogName",
        "fourthCatalogName",
        "thirdCatalogName",
        "secondCatalogName",
        "firstCatalogName",
    ):
        name = clean_text(raw.get(key), 60)
        if name:
            return name
    return ""


_LABEL_KEYS = ("paramName", "parameterName", "attributeName", "name", "key", "label")
_VALUE_KEYS = ("paramValue", "parameterValue", "attributeValue", "value")


def _parameters(raw: Any) -> dict[str, str]:
    """Normalize the parametric list into label/value pairs.

    The specification describes the field only as key value pairs and gives no
    example, so a mapping, a list of single pair mappings and a list of named
    entries are all handled.
    """
    specs: dict[str, str] = {}

    def record(label: Any, value: Any) -> None:
        name = clean_text(label, 60)
        text = clean_text(value, 80)
        if name and text:
            specs.setdefault(name, text)

    if isinstance(raw, dict):
        for label, value in raw.items():
            record(label, value)
        return specs
    if not isinstance(raw, list):
        return specs

    for item in raw:
        if isinstance(item, dict):
            label = next((item[key] for key in _LABEL_KEYS if item.get(key)), None)
            value = next((item[key] for key in _VALUE_KEYS if item.get(key)), None)
            if label is None and len(item) == 1:
                label, value = next(iter(item.items()))
            record(label, value)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            record(item[0], item[1])
    return specs


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
