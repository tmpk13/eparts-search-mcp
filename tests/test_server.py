"""The MCP tool surface: shapes returned to a client."""

from __future__ import annotations

import httpx
import pytest
import respx

from eparts_search_mcp import server

from .test_digikey import SAMPLE_PRODUCT, SEARCH_URL, TOKEN_URL
from .test_lcsc import mock_search as mock_lcsc_search
from .test_mouser import KEYWORD_URL, SAMPLE_PART


@pytest.fixture(autouse=True)
def use_test_service(service):
    server.set_service(service)
    yield
    server.set_service(None)


def mock_every_source() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
    )
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"Products": [SAMPLE_PRODUCT]})
    )
    mock_lcsc_search()
    respx.post(KEYWORD_URL).mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
    )


async def test_tools_are_registered():
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert names == {"search_parts", "part_details", "source_status", "clear_cache"}


@respx.mock
async def test_search_parts_merges_and_compares_prices():
    mock_every_source()
    payload = await server.search_parts("LM317", quantity=100)

    assert payload["count"] == 1
    part = payload["parts"][0]
    assert part["available_from"] == ["digikey", "lcsc", "mouser"]
    assert len(part["offers"]) == 3
    # At 100 pieces LCSC quotes 0.55 against Mouser's 0.61 and DigiKey's 0.68,
    # so the comparison must name the cheapest source rather than the first
    # one searched.
    assert part["cheapest_at_quantity"]["source"] == "lcsc"
    assert part["cheapest_at_quantity"]["unit_price"] == 0.55


@respx.mock
async def test_price_comparison_skips_offers_without_a_price_at_that_quantity():
    mock_every_source()
    # LCSC sells this part in tens, so at a quantity of one it has no
    # applicable break and must not win the comparison by default.
    payload = await server.search_parts("LM317", quantity=1)

    assert payload["parts"][0]["cheapest_at_quantity"]["source"] == "mouser"


@respx.mock
async def test_search_parts_can_target_one_source():
    mock_every_source()
    payload = await server.search_parts("LM317", sources=["mouser"])

    assert payload["sources_searched"] == ["mouser"]
    assert payload["parts"][0]["available_from"] == ["mouser"]


@respx.mock
async def test_search_parts_unmerged_groups_by_source():
    mock_every_source()
    payload = await server.search_parts("LM317", merge=False)

    assert set(payload["by_source"]) == {"digikey", "lcsc", "mouser"}
    assert "parts" not in payload


@respx.mock
async def test_search_parts_reports_a_failed_source():
    mock_every_source()
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500, text="boom"))

    payload = await server.search_parts("LM317")
    assert payload["count"] == 1
    assert payload["errors"][0]["source"] == "digikey"


@respx.mock
async def test_specs_can_be_omitted():
    mock_every_source()
    payload = await server.search_parts("LM317", include_specs=False, sources=["digikey"])
    assert "specs" not in payload["parts"][0]["offers"][0]


@respx.mock
async def test_part_details_returns_each_source_that_knows_the_part():
    mock_every_source()
    respx.get("https://api.digikey.com/products/v4/search/LM317T%2FNOPB/productdetails").mock(
        return_value=httpx.Response(200, json={"Product": SAMPLE_PRODUCT})
    )
    respx.post("https://api.mouser.com/api/v2/search/partnumber").mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
    )

    payload = await server.part_details("LM317T/NOPB")
    assert payload["count"] == 3
    assert {entry["source"] for entry in payload["results"]} == {"digikey", "lcsc", "mouser"}


async def test_source_status_reports_limits_and_budget():
    payload = await server.source_status()
    assert payload["known_sources"] == ["digikey", "lcsc", "mouser"]
    assert payload["sources"]["digikey"]["configured"] is True
    assert "remaining_today" in payload["sources"]["digikey"]["rate_limit"]


@respx.mock
async def test_clear_cache_reports_how_much_was_dropped(service):
    service.cache.set("k1", "mouser", {"a": 1}, ttl=600)
    service.cache.set("k2", "digikey", {"b": 2}, ttl=600)

    assert (await server.clear_cache("mouser"))["cleared"] == 1
    assert (await server.clear_cache())["cleared"] == 1
