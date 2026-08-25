from __future__ import annotations

import json

import httpx
import pytest
import respx

from digi_mouse_search.providers.mouser import parse_availability, parse_price

KEYWORD_URL = "https://api.mouser.com/api/v2/search/keyword"
PARTNUMBER_URL = "https://api.mouser.com/api/v2/search/partnumber"

SAMPLE_PART = {
    "ManufacturerPartNumber": "LM317T/NOPB",
    "Manufacturer": "Texas Instruments",
    "Description": "Linear Voltage Regulators  1.5A Adj Pos Volt Reg",
    "MouserPartNumber": "595-LM317T/NOPB",
    "ProductDetailUrl": "https://www.mouser.com/ProductDetail/595-LM317TNOPB",
    "DataSheetUrl": "https://www.ti.com/lit/ds/symlink/lm317.pdf",
    "Availability": "3,412 In Stock",
    "Min": "1",
    "LifecycleStatus": "Active",
    "Category": "Linear Voltage Regulators",
    "PriceBreaks": [
        {"Quantity": 1, "Price": "$0.94", "Currency": "USD"},
        {"Quantity": 100, "Price": "$0.61", "Currency": "USD"},
    ],
    "ProductAttributes": [
        {"AttributeName": "Output Voltage", "AttributeValue": "1.25 V to 37 V"},
    ],
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$0.94", 0.94),
        ("0.94", 0.94),
        (1.5, 1.5),
        ("1,23 EUR", 1.23),
        ("$1,234.50", 1234.50),
        ("1.234,50", 1234.50),
        ("", None),
        (None, None),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3,412 In Stock", 3412),
        ("0 In Stock", 0),
        ("None", None),
        (None, None),
        (17, 17),
    ],
)
def test_parse_availability(raw, expected):
    assert parse_availability(raw) == expected


@respx.mock
async def test_search_normalizes_a_part(service):
    respx.post(KEYWORD_URL).mock(
        return_value=httpx.Response(
            200, json={"Errors": [], "SearchResults": {"Parts": [SAMPLE_PART]}}
        )
    )
    parts = await service.providers["mouser"].search("LM317", limit=5)

    assert len(parts) == 1
    part = parts[0]
    assert part.source == "mouser"
    assert part.mpn == "LM317T/NOPB"
    assert part.distributor_pn == "595-LM317T/NOPB"
    assert part.stock == 3412
    assert part.min_order_qty == 1
    assert part.lifecycle == "Active"
    assert [b.quantity for b in part.price_breaks] == [1, 100]
    assert part.unit_price_at(150) == 0.61
    assert part.unit_price_at(1) == 0.94
    assert part.specs["Output Voltage"] == "1.25 V to 37 V"


@respx.mock
async def test_errors_in_a_200_body_are_raised(service):
    respx.post(KEYWORD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "Errors": [
                    {
                        "Code": "Invalid",
                        "Message": "Invalid unique identifier.",
                        "PropertyName": "API Key",
                    }
                ],
                "SearchResults": None,
            },
        )
    )
    from digi_mouse_search.providers.base import ProviderError

    with pytest.raises(ProviderError, match="API Key"):
        await service.providers["mouser"].search("LM317")


@respx.mock
async def test_in_stock_flag_sets_search_options(service):
    route = respx.post(KEYWORD_URL).mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": []}})
    )
    await service.providers["mouser"].search("LM317", in_stock_only=True)

    body = json.loads(route.calls.last.request.content)
    assert body["SearchByKeywordRequest"]["searchOptions"] == "InStock"


@respx.mock
async def test_manufacturer_uses_the_dedicated_endpoint(service):
    route = respx.post("https://api.mouser.com/api/v2/search/keywordandmanufacturer").mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": []}})
    )
    await service.providers["mouser"].search("LM317", manufacturer="Texas Instruments")
    assert route.called


@respx.mock
async def test_details_returns_first_match(service):
    respx.post(PARTNUMBER_URL).mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": [SAMPLE_PART]}})
    )
    part = await service.providers["mouser"].details("595-LM317T/NOPB")
    assert part is not None
    assert part.mpn == "LM317T/NOPB"


@respx.mock
async def test_details_returns_none_when_empty(service):
    respx.post(PARTNUMBER_URL).mock(
        return_value=httpx.Response(200, json={"SearchResults": {"Parts": []}})
    )
    assert await service.providers["mouser"].details("nope") is None
