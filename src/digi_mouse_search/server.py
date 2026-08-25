"""MCP tool surface."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import __version__
from .models import MergedPart, Part
from .service import ALL_SOURCES, SearchService, best_offer

# Enough breaks to see the shape of the price ladder without spending the
# caller's context on a twelve tier table per part.
MAX_PRICE_BREAKS = 6
MAX_SPECS = 12

mcp = MCPServer(
    "digi-mouse-search",
    version=__version__,
    instructions=(
        "Search DigiKey and Mouser for electronic components. Both distributors "
        "are queried together by default so that prices can be compared; pass "
        "sources to query one on its own. Request budget is limited, so prefer "
        "one broad search over many narrow ones."
    ),
)

_service: SearchService | None = None


def get_service() -> SearchService:
    """Build the service on first use so import never touches the network."""
    global _service
    if _service is None:
        _service = SearchService()
    return _service


def set_service(service: SearchService | None) -> None:
    """Replace the process-wide service, used by tests."""
    global _service
    _service = service


def _render_part(part: Part, include_specs: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "source": part.source,
        "mpn": part.mpn,
        "manufacturer": part.manufacturer,
        "description": part.description,
        "distributor_pn": part.distributor_pn,
        "stock": part.stock,
        "min_order_qty": part.min_order_qty,
        "lifecycle": part.lifecycle,
        "packaging": part.packaging,
        "product_url": part.product_url,
        "datasheet_url": part.datasheet_url,
        "price_breaks": [
            {"quantity": b.quantity, "unit_price": b.unit_price, "currency": b.currency}
            for b in part.price_breaks[:MAX_PRICE_BREAKS]
        ],
    }
    if include_specs and part.specs:
        data["specs"] = dict(list(part.specs.items())[:MAX_SPECS])
    return {key: value for key, value in data.items() if value not in (None, [], {})}


def _render_merged(part: MergedPart, quantity: int, include_specs: bool) -> dict[str, Any]:
    cheapest = best_offer(part, quantity)
    entry: dict[str, Any] = {
        "mpn": part.mpn,
        "manufacturer": part.manufacturer,
        "description": part.description,
        "available_from": part.sources,
        "offers": [_render_part(offer, include_specs) for offer in part.offers],
    }
    if cheapest is not None:
        entry["cheapest_at_quantity"] = {
            "quantity": quantity,
            "source": cheapest.source,
            "unit_price": cheapest.unit_price_at(quantity),
        }
    return entry


@mcp.tool()
async def search_parts(
    keyword: Annotated[str, Field(description="Part number, description or search phrase")],
    sources: Annotated[
        list[str] | None,
        Field(
            description=(
                "Which distributors to query: ['digikey'], ['mouser'], or both. "
                "Omit to search every configured source."
            )
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50, description="Maximum parts per source")] = 10,
    manufacturer: Annotated[
        str | None, Field(description="Restrict results to one manufacturer, by name")
    ] = None,
    in_stock_only: Annotated[bool, Field(description="Exclude parts with no stock")] = False,
    merge: Annotated[
        bool,
        Field(
            description=(
                "Group offers for the same part number into one entry with a price "
                "comparison. Set false to keep each distributor's results separate."
            )
        ),
    ] = True,
    quantity: Annotated[int, Field(ge=1, description="Quantity used for the price comparison")] = 1,
    include_specs: Annotated[bool, Field(description="Include parametric specifications")] = True,
) -> dict[str, Any]:
    """Search DigiKey and Mouser for electronic components.

    Searches every configured distributor by default and merges offers for the
    same part number so prices can be compared. Pass `sources` to query one
    distributor on its own. If a distributor fails or is out of quota, results
    from the others are still returned and the failure is listed under errors.
    """
    service = get_service()
    result = await service.search(
        keyword,
        sources=sources,
        limit=limit,
        manufacturer=manufacturer,
        in_stock_only=in_stock_only,
        merge=merge,
    )

    payload: dict[str, Any] = {
        "query": result.query,
        "sources_searched": result.sources_searched,
    }
    if merge:
        payload["parts"] = [_render_merged(part, quantity, include_specs) for part in result.parts]
        payload["count"] = len(result.parts)
    else:
        payload["by_source"] = {
            name: [_render_part(part, include_specs) for part in parts]
            for name, parts in result.by_source.items()
        }
        payload["count"] = sum(len(parts) for parts in result.by_source.values())
    if result.errors:
        payload["errors"] = [{"source": err.source, "error": err.error} for err in result.errors]
    return payload


@mcp.tool()
async def part_details(
    part_number: Annotated[
        str,
        Field(
            description=(
                "Manufacturer part number, or a distributor part number such as "
                "296-1234-ND for DigiKey or 511-LM317T for Mouser"
            )
        ),
    ],
    sources: Annotated[
        list[str] | None,
        Field(description="Which distributors to query. Omit to query all configured sources."),
    ] = None,
) -> dict[str, Any]:
    """Look up one part by part number, with full specifications and pricing.

    Distributor part numbers only resolve at the distributor that issued them,
    so a lookup by one distributor's number will normally return a result from
    that source alone.
    """
    service = get_service()
    parts, errors = await service.details(part_number, sources=sources)
    payload: dict[str, Any] = {
        "part_number": part_number,
        "results": [_render_part(part) for part in parts],
        "count": len(parts),
    }
    if errors:
        payload["errors"] = [{"source": err.source, "error": err.error} for err in errors]
    return payload


@mcp.tool()
async def source_status() -> dict[str, Any]:
    """Report which distributors are configured and how much request budget is left.

    Use this when a search reports a rate limit error, to see the configured
    limits and how many requests remain for the day.
    """
    service = get_service()
    status = service.status()
    status["known_sources"] = list(ALL_SOURCES)
    return status


@mcp.tool()
async def clear_cache(
    source: Annotated[
        str | None, Field(description="Clear one source only; omit to clear everything")
    ] = None,
) -> dict[str, Any]:
    """Drop cached distributor responses to force fresh stock and pricing.

    Cached responses do not count against the daily quota, so clearing the
    cache makes subsequent searches spend real request budget.
    """
    service = get_service()
    removed = service.cache.clear(source)
    return {"cleared": removed, "source": source or "all"}


def run() -> None:
    """Serve over stdio."""
    mcp.run()
