"""The MCP tool surface: shapes returned to a client."""

from __future__ import annotations

import httpx
import pytest
import respx

from digi_mouse_search import server

from .test_digikey import SAMPLE_PRODUCT, SEARCH_URL, TOKEN_URL
from .test_mouser import KEYWORD_URL, SAMPLE_PART


@pytest.fixture(autouse=True)
def use_test_service(service):
    server.set_service(service)
    yield
    server.set_service(None)


def mock_both_sources() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
    )
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"Products": [SAMPLE_PRODUCT]})
    )
    respx.post(KEYWORD_URL).mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
    )


async def test_tools_are_registered():
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert names == {"search_parts", "part_details", "source_status", "clear_cache"}


@respx.mock
async def test_search_parts_merges_and_compares_prices():
    mock_both_sources()
    payload = await server.search_parts("LM317", quantity=100)

    assert payload["count"] == 1
    part = payload["parts"][0]
    assert part["available_from"] == ["digikey", "mouser"]
    assert len(part["offers"]) == 2
    # At 100 pieces Mouser quotes 0.61 against DigiKey's 0.68, so the comparison
    # must name the cheaper source rather than the first one searched.
    assert part["cheapest_at_quantity"]["source"] == "mouser"
    assert part["cheapest_at_quantity"]["unit_price"] == 0.61


@respx.mock
async def test_search_parts_can_target_one_source():
    mock_both_sources()
    payload = await server.search_parts("LM317", sources=["mouser"])

    assert payload["sources_searched"] == ["mouser"]
    assert payload["parts"][0]["available_from"] == ["mouser"]


@respx.mock
async def test_search_parts_unmerged_groups_by_source():
    mock_both_sources()
    payload = await server.search_parts("LM317", merge=False)

    assert set(payload["by_source"]) == {"digikey", "mouser"}
    assert "parts" not in payload


@respx.mock
async def test_search_parts_reports_a_failed_source():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
    )
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500, text="boom"))
    respx.post(KEYWORD_URL).mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
    )

    payload = await server.search_parts("LM317")
    assert payload["count"] == 1
    assert payload["errors"][0]["source"] == "digikey"


@respx.mock
async def test_specs_can_be_omitted():
    mock_both_sources()
    payload = await server.search_parts("LM317", include_specs=False, sources=["digikey"])
    assert "specs" not in payload["parts"][0]["offers"][0]


@respx.mock
async def test_part_details_returns_each_source_that_knows_the_part():
    mock_both_sources()
    respx.get("https://api.digikey.com/products/v4/search/LM317T%2FNOPB/productdetails").mock(
        return_value=httpx.Response(200, json={"Product": SAMPLE_PRODUCT})
    )
    respx.post("https://api.mouser.com/api/v2/search/partnumber").mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
    )

    payload = await server.part_details("LM317T/NOPB")
    assert payload["count"] == 2
    assert {entry["source"] for entry in payload["results"]} == {"digikey", "mouser"}


async def test_source_status_reports_limits_and_budget():
    payload = await server.source_status()
    assert payload["known_sources"] == ["digikey", "mouser"]
    assert payload["sources"]["digikey"]["configured"] is True
    assert "remaining_today" in payload["sources"]["digikey"]["rate_limit"]


@respx.mock
async def test_clear_cache_reports_how_much_was_dropped(service):
    service.cache.set("k1", "mouser", {"a": 1}, ttl=600)
    service.cache.set("k2", "digikey", {"b": 2}, ttl=600)

    assert (await server.clear_cache("mouser"))["cleared"] == 1
    assert (await server.clear_cache())["cleared"] == 1
