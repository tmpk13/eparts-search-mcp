"""Normalized data shapes shared by every provider."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SourceName = Literal["digikey", "lcsc", "mouser"]


class PriceBreak(BaseModel):
    """A single quantity tier of a distributor's price ladder."""

    quantity: int
    unit_price: float
    currency: str = "USD"


class Part(BaseModel):
    """One distributor's offer for one manufacturer part number.

    Fields that a provider does not supply stay None rather than being
    guessed at, so an empty value always means "not reported" and never
    "reported as zero".
    """

    source: SourceName
    mpn: str
    manufacturer: str = ""
    description: str = ""
    distributor_pn: str = ""
    product_url: str | None = None
    datasheet_url: str | None = None
    stock: int | None = None
    min_order_qty: int | None = None
    lifecycle: str | None = None
    packaging: str | None = None
    price_breaks: list[PriceBreak] = Field(default_factory=list)
    specs: dict[str, str] = Field(default_factory=dict)

    def unit_price_at(self, quantity: int) -> float | None:
        """Unit price for the highest break whose quantity is still <= quantity."""
        applicable = [b for b in self.price_breaks if b.quantity <= quantity]
        if not applicable:
            return None
        return max(applicable, key=lambda b: b.quantity).unit_price


class MergedPart(BaseModel):
    """One manufacturer part number with every distributor offer found for it."""

    mpn: str
    manufacturer: str = ""
    description: str = ""
    offers: list[Part] = Field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({o.source for o in self.offers})


class SourceError(BaseModel):
    """A provider that failed or was skipped, reported alongside partial results."""

    source: str
    error: str


class SearchResult(BaseModel):
    """Envelope returned by a search, carrying partial results plus failures.

    A failure in one provider never suppresses results from another; callers
    get whatever succeeded plus an explicit note of what did not.
    """

    query: str
    sources_searched: list[str] = Field(default_factory=list)
    merged: bool = True
    parts: list[MergedPart] = Field(default_factory=list)
    by_source: dict[str, list[Part]] = Field(default_factory=dict)
    errors: list[SourceError] = Field(default_factory=list)
