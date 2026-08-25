"""DigiKey Product Information V4 adapter."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

from ..cache import Cache
from ..config import DigiKeyConfig
from ..models import Part, PriceBreak
from ..ratelimit import RateLimiter
from .base import Provider, ProviderError, ProviderNotFound, absolute_url, clean_text

# Manufacturer ids change far more slowly than stock or pricing, so the lookup
# used to translate a manufacturer name into a filter id gets its own long TTL.
MANUFACTURER_CACHE_TTL = 7 * 24 * 3600

# Refresh slightly early; the token is short lived and a request that starts
# valid must not expire in flight.
TOKEN_EXPIRY_MARGIN = 60.0


class DigiKeyProvider(Provider):
    name = "digikey"

    def __init__(
        self,
        config: DigiKeyConfig,
        client: httpx.AsyncClient,
        cache: Cache,
        limiter: RateLimiter,
        cache_ttl: int,
    ) -> None:
        super().__init__(client, cache, limiter, cache_ttl)
        self.config = config
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return self.config.configured

    async def _access_token(self) -> str:
        """Fetch or reuse a client_credentials token.

        Tokens are held in memory only. They are credentials with a ten minute
        life, so writing them into the on-disk cache would trade real risk for
        no meaningful saving.
        """
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token

        await self._limiter.acquire()
        try:
            response = await self._client.post(
                f"{self.config.base_url}/v1/oauth2/token",
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"digikey: token request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"digikey: token request returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ProviderError("digikey: token response contained no access_token")

        self._token = token
        self._token_expires_at = now + max(
            0.0, float(payload.get("expires_in", 600)) - TOKEN_EXPIRY_MARGIN
        )
        return token

    async def _headers(self) -> dict[str, str]:
        """Build request headers.

        The client id header is required in addition to the bearer token; a
        request carrying only the token is rejected.
        """
        token = await self._access_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": self.config.client_id or "",
            "X-DIGIKEY-Locale-Site": self.config.site,
            "X-DIGIKEY-Locale-Language": self.config.language,
            "X-DIGIKEY-Locale-Currency": self.config.currency,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _locale_key(self) -> dict[str, str]:
        """Locale affects pricing and availability, so it belongs in the cache key."""
        return {
            "site": self.config.site,
            "currency": self.config.currency,
            "language": self.config.language,
            "sandbox": str(self.config.sandbox),
        }

    async def _manufacturer_filter_id(self, name: str) -> str | None:
        """Translate a manufacturer name into the filter id the API expects."""
        data = await self._cached_request(
            "manufacturers",
            {"locale": self._locale_key()},
            {
                "method": "GET",
                "url": f"{self.config.base_url}/products/v4/search/manufacturers",
                "headers": await self._headers(),
            },
            ttl=MANUFACTURER_CACHE_TTL,
        )
        wanted = name.strip().lower()
        entries = data.get("Manufacturers") or []
        for entry in entries:
            if str(entry.get("Name", "")).strip().lower() == wanted:
                return str(entry.get("Id"))
        for entry in entries:
            if wanted in str(entry.get("Name", "")).strip().lower():
                return str(entry.get("Id"))
        return None

    async def search(
        self,
        keyword: str,
        limit: int = 10,
        manufacturer: str | None = None,
        in_stock_only: bool = False,
    ) -> list[Part]:
        if not self.configured:
            raise ProviderError("digikey: DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET are not set")

        filters: dict[str, Any] = {}
        if in_stock_only:
            filters["MinimumQuantityAvailable"] = 1
        if manufacturer:
            filter_id = await self._manufacturer_filter_id(manufacturer)
            if filter_id is None:
                return []
            filters["ManufacturerFilter"] = [{"Id": filter_id}]

        body: dict[str, Any] = {
            "Keywords": keyword[:250],
            # The API caps a page at 50 regardless of what is asked for.
            "Limit": max(1, min(int(limit), 50)),
            "Offset": 0,
        }
        if filters:
            body["FilterOptionsRequest"] = filters

        data = await self._cached_request(
            "search",
            {"body": body, "locale": self._locale_key()},
            {
                "method": "POST",
                "url": f"{self.config.base_url}/products/v4/search/keyword",
                "headers": await self._headers(),
                "json": body,
            },
        )

        products = list(data.get("ExactMatches") or [])
        products.extend(data.get("Products") or [])

        parts: list[Part] = []
        seen: set[str] = set()
        for product in products:
            part = self._to_part(product)
            if part is None or part.distributor_pn in seen:
                continue
            seen.add(part.distributor_pn)
            parts.append(part)
            if len(parts) >= limit:
                break
        return parts

    async def details(self, part_number: str) -> Part | None:
        if not self.configured:
            raise ProviderError("digikey: DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET are not set")

        try:
            data = await self._cached_request(
                "details",
                {"part_number": part_number, "locale": self._locale_key()},
                {
                    "method": "GET",
                    # Part numbers legitimately contain slashes and hashes, so
                    # the segment is escaped rather than interpolated raw.
                    "url": (
                        f"{self.config.base_url}/products/v4/search/"
                        f"{quote(part_number, safe='')}/productdetails"
                    ),
                    "headers": await self._headers(),
                },
            )
        except ProviderNotFound:
            # The API answers 404 for a part number it does not carry. That is
            # an answer, not a failure, so it becomes an empty result.
            return None
        product = data.get("Product")
        if not product:
            return None
        return self._to_part(product)

    @staticmethod
    def _pick_variation(product: dict[str, Any]) -> dict[str, Any]:
        """Choose the packaging variation that best represents the offer.

        A product carries one variation per packaging option (cut tape, reel,
        tube). Preferring one that is both priced and in stock keeps the
        summary from reporting a reel-only price for a part someone wants in
        single quantities.
        """
        variations = product.get("ProductVariations") or []
        if not variations:
            return {}

        def score(variation: dict[str, Any]) -> tuple[int, int, int]:
            has_price = 1 if variation.get("StandardPricing") else 0
            in_stock = 1 if (variation.get("QuantityAvailableforPackageType") or 0) > 0 else 0
            not_marketplace = 0 if variation.get("MarketPlace") else 1
            return (has_price, in_stock, not_marketplace)

        return max(variations, key=score)

    def _to_part(self, product: dict[str, Any]) -> Part | None:
        mpn = clean_text(product.get("ManufacturerProductNumber"), 120)
        if not mpn:
            return None

        variation = self._pick_variation(product)
        price_breaks = [
            PriceBreak(
                quantity=int(entry.get("BreakQuantity") or 0),
                unit_price=float(entry.get("UnitPrice") or 0.0),
                currency=self.config.currency,
            )
            for entry in (variation.get("StandardPricing") or [])
            if entry.get("BreakQuantity") is not None
        ]
        price_breaks.sort(key=lambda b: b.quantity)

        description = product.get("Description") or {}
        status = product.get("ProductStatus") or {}
        manufacturer = product.get("Manufacturer") or {}
        packaging = (variation.get("PackageType") or {}).get("Name")

        specs = {}
        for parameter in product.get("Parameters") or []:
            label = clean_text(parameter.get("ParameterText"), 60)
            value = clean_text(parameter.get("ValueText"), 80)
            if label and value:
                specs[label] = value

        return Part(
            source="digikey",
            mpn=mpn,
            manufacturer=clean_text(manufacturer.get("Name"), 80),
            description=clean_text(description.get("ProductDescription"), 200),
            distributor_pn=clean_text(variation.get("DigiKeyProductNumber"), 60) or mpn,
            product_url=absolute_url(product.get("ProductUrl")),
            datasheet_url=absolute_url(product.get("DatasheetUrl")),
            stock=_as_int(product.get("QuantityAvailable")),
            min_order_qty=_as_int(variation.get("MinimumOrderQuantity")),
            lifecycle=clean_text(status.get("Status"), 40) or None,
            packaging=clean_text(packaging, 40) or None,
            price_breaks=price_breaks,
            specs=specs,
        )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
