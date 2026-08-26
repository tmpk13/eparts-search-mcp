"""Fan-out behavior: sources used independently, combined, and partially failing."""

from __future__ import annotations

import httpx
import respx

from eparts_search_mcp.config import Config, DigiKeyConfig, MouserConfig
from eparts_search_mcp.models import Part, PriceBreak
from eparts_search_mcp.normalize import merge_parts, mpn_key
from eparts_search_mcp.service import SearchService, best_offer

from .test_digikey import SAMPLE_PRODUCT, SEARCH_URL, TOKEN_URL
from .test_lcsc import mock_search as mock_lcsc_search
from .test_mouser import KEYWORD_URL, SAMPLE_PART


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


@respx.mock
async def test_combined_search_queries_every_source(service):
    mock_every_source()
    result = await service.search("LM317")

    assert sorted(result.sources_searched) == ["digikey", "lcsc", "mouser"]
    assert result.errors == []
    # All three carry the same part number, so it merges into one entry.
    assert len(result.parts) == 1
    assert result.parts[0].sources == ["digikey", "lcsc", "mouser"]


@respx.mock
async def test_single_source_search_leaves_the_others_untouched(service):
    mock_every_source()
    mouser_route = respx.post(KEYWORD_URL)
    lcsc_route = mock_lcsc_search()

    result = await service.search("LM317", sources=["digikey"])

    assert result.sources_searched == ["digikey"]
    assert not mouser_route.called
    assert not lcsc_route.called
    assert all(offer.source == "digikey" for part in result.parts for offer in part.offers)


@respx.mock
async def test_unmerged_search_keeps_sources_separate(service):
    mock_every_source()
    result = await service.search("LM317", merge=False)

    assert set(result.by_source) == {"digikey", "lcsc", "mouser"}
    assert len(result.by_source["digikey"]) == 1
    assert len(result.by_source["lcsc"]) == 1
    assert len(result.by_source["mouser"]) == 1
    assert result.parts == []


@respx.mock
async def test_one_source_failing_does_not_hide_the_others(service):
    mock_every_source()
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    result = await service.search("LM317")

    assert len(result.parts) == 1
    assert result.parts[0].sources == ["lcsc", "mouser"]
    assert [err.source for err in result.errors] == ["digikey"]
    assert "503" in result.errors[0].error


async def test_unconfigured_source_is_reported_not_raised(tmp_path):
    config = Config(
        digikey=DigiKeyConfig(),
        mouser=MouserConfig(api_key="key"),
        cache_path=tmp_path / "cache.sqlite3",
        cache_ttl_seconds=0,
    )
    svc = SearchService(config)
    try:
        names, errors = svc.resolve_sources(["digikey", "mouser"])
        assert names == ["mouser"]
        assert errors[0].source == "digikey"
        assert "credentials" in errors[0].error
    finally:
        await svc.aclose()


async def test_unknown_source_name_is_reported(service):
    names, errors = service.resolve_sources(["digikey", "farnell"])
    assert names == ["digikey"]
    assert errors[0].source == "farnell"
    assert "unknown source" in errors[0].error


async def test_no_configured_source_produces_a_clear_message(tmp_path):
    svc = SearchService(Config(cache_path=tmp_path / "cache.sqlite3", cache_ttl_seconds=0))
    try:
        result = await svc.search("LM317")
        assert result.parts == []
        assert "no source is configured" in result.errors[0].error
    finally:
        await svc.aclose()


@respx.mock
async def test_caching_prevents_a_second_api_call(tmp_path):
    config = Config(
        digikey=DigiKeyConfig(),
        mouser=MouserConfig(api_key="key"),
        cache_path=tmp_path / "cache.sqlite3",
        cache_ttl_seconds=600,
    )
    svc = SearchService(config)
    try:
        route = respx.post(KEYWORD_URL).mock(
            return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
        )
        first = await svc.search("LM317")
        second = await svc.search("LM317")

        assert route.call_count == 1
        assert len(first.parts) == len(second.parts) == 1
        # A cached hit must not spend request budget.
        assert svc.limiters["mouser"].describe()["used_today"] == 1
    finally:
        await svc.aclose()


def test_mpn_key_ignores_case_and_punctuation():
    assert mpn_key("LM317T") == mpn_key("lm-317_t")


def test_mpn_key_keeps_meaningful_suffixes():
    assert mpn_key("LM317T/NOPB") != mpn_key("LM317T")


def test_merge_groups_matching_part_numbers():
    parts = [
        Part(source="digikey", mpn="LM317T", manufacturer="TI"),
        Part(source="mouser", mpn="lm317t", manufacturer="Texas Instruments"),
        Part(source="mouser", mpn="LM7805", manufacturer="TI"),
    ]
    merged = merge_parts(parts)

    assert len(merged) == 2
    assert merged[0].sources == ["digikey", "mouser"]
    assert merged[1].sources == ["mouser"]


def test_best_offer_uses_the_applicable_price_break():
    cheap_in_bulk = Part(
        source="digikey",
        mpn="LM317T",
        price_breaks=[
            PriceBreak(quantity=1, unit_price=1.20),
            PriceBreak(quantity=100, unit_price=0.50),
        ],
    )
    cheap_in_ones = Part(
        source="mouser",
        mpn="LM317T",
        price_breaks=[PriceBreak(quantity=1, unit_price=0.94)],
    )
    merged = merge_parts([cheap_in_bulk, cheap_in_ones])[0]

    assert best_offer(merged, quantity=1).source == "mouser"
    assert best_offer(merged, quantity=100).source == "digikey"


def test_best_offer_is_none_without_pricing():
    merged = merge_parts([Part(source="mouser", mpn="LM317T")])[0]
    assert best_offer(merged) is None
