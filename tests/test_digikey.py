from __future__ import annotations

import json

import httpx
import pytest
import respx

from digi_mouse_search.providers.base import ProviderError, absolute_url

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
MANUFACTURERS_URL = "https://api.digikey.com/products/v4/search/manufacturers"

SAMPLE_PRODUCT = {
    "ManufacturerProductNumber": "LM317T/NOPB",
    "Manufacturer": {"Id": 296, "Name": "Texas Instruments"},
    "Description": {
        "ProductDescription": "IC REG LINEAR POS ADJ 1.5A TO220",
        "DetailedDescription": "Linear Voltage Regulator IC Positive Adjustable",
    },
    "ProductUrl": "https://www.digikey.com/short/abc",
    "DatasheetUrl": "https://www.ti.com/lit/ds/symlink/lm317.pdf",
    "QuantityAvailable": 8123,
    "ProductStatus": {"Id": 0, "Status": "Active"},
    "Parameters": [
        {"ParameterText": "Voltage - Output", "ValueText": "1.25 V ~ 37 V"},
        {"ParameterText": "Package / Case", "ValueText": "TO-220-3"},
    ],
    "ProductVariations": [
        {
            "DigiKeyProductNumber": "296-LM317T-ND",
            "PackageType": {"Id": 1, "Name": "Tube"},
            "MinimumOrderQuantity": 1,
            "QuantityAvailableforPackageType": 8123,
            "MarketPlace": False,
            "StandardPricing": [
                {"BreakQuantity": 1, "UnitPrice": 1.02, "TotalPrice": 1.02},
                {"BreakQuantity": 100, "UnitPrice": 0.68, "TotalPrice": 68.0},
            ],
        }
    ],
}


def mock_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-123", "expires_in": 600})
    )


@respx.mock
async def test_search_normalizes_a_product(service):
    mock_token()
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"Products": [SAMPLE_PRODUCT], "ProductsCount": 1})
    )

    parts = await service.providers["digikey"].search("LM317", limit=5)

    assert len(parts) == 1
    part = parts[0]
    assert part.source == "digikey"
    assert part.mpn == "LM317T/NOPB"
    assert part.distributor_pn == "296-LM317T-ND"
    assert part.manufacturer == "Texas Instruments"
    assert part.stock == 8123
    assert part.min_order_qty == 1
    assert part.packaging == "Tube"
    assert part.lifecycle == "Active"
    assert part.unit_price_at(100) == 0.68
    assert part.specs["Package / Case"] == "TO-220-3"


@respx.mock
async def test_client_id_header_accompanies_the_bearer_token(service):
    mock_token()
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": []}))
    await service.providers["digikey"].search("LM317")

    headers = route.calls.last.request.headers
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["X-DIGIKEY-Client-Id"] == "test-id"


@respx.mock
async def test_token_is_reused_across_requests(service):
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-123", "expires_in": 600})
    )
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": []}))

    await service.providers["digikey"].search("LM317")
    await service.providers["digikey"].search("LM7805")

    assert token_route.call_count == 1


@respx.mock
async def test_exact_matches_are_returned_before_other_products(service):
    mock_token()
    other: dict[str, object] = dict(SAMPLE_PRODUCT, ManufacturerProductNumber="LM317LZ")
    other["ProductVariations"] = [{"DigiKeyProductNumber": "296-LM317LZ-ND"}]
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"ExactMatches": [SAMPLE_PRODUCT], "Products": [other]}
        )
    )

    parts = await service.providers["digikey"].search("LM317", limit=5)
    assert [p.mpn for p in parts] == ["LM317T/NOPB", "LM317LZ"]


@respx.mock
async def test_limit_is_capped_at_the_api_maximum(service):
    mock_token()
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": []}))
    await service.providers["digikey"].search("resistor", limit=500)

    body = json.loads(route.calls.last.request.content)
    assert body["Limit"] == 50


@respx.mock
async def test_in_stock_flag_becomes_a_quantity_filter(service):
    mock_token()
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": []}))
    await service.providers["digikey"].search("LM317", in_stock_only=True)

    body = json.loads(route.calls.last.request.content)
    assert body["FilterOptionsRequest"]["MinimumQuantityAvailable"] == 1


@respx.mock
async def test_manufacturer_name_is_resolved_to_a_filter_id(service):
    mock_token()
    respx.get(MANUFACTURERS_URL).mock(
        return_value=httpx.Response(
            200, json={"Manufacturers": [{"Id": 296, "Name": "Texas Instruments"}]}
        )
    )
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": []}))
    await service.providers["digikey"].search("LM317", manufacturer="texas instruments")

    body = json.loads(route.calls.last.request.content)
    # The API models a filter id as a string, not an integer.
    assert body["FilterOptionsRequest"]["ManufacturerFilter"] == [{"Id": "296"}]


@respx.mock
async def test_unknown_manufacturer_returns_no_results_without_searching(service):
    mock_token()
    respx.get(MANUFACTURERS_URL).mock(return_value=httpx.Response(200, json={"Manufacturers": []}))
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": []}))
    parts = await service.providers["digikey"].search("LM317", manufacturer="Nonexistent Corp")

    assert parts == []
    assert not route.called


@respx.mock
async def test_variation_with_price_and_stock_is_preferred(service):
    mock_token()
    product: dict[str, object] = dict(SAMPLE_PRODUCT)
    product["ProductVariations"] = [
        {
            "DigiKeyProductNumber": "296-REEL-ND",
            "PackageType": {"Name": "Tape & Reel (TR)"},
            "QuantityAvailableforPackageType": 0,
            "StandardPricing": [],
        },
        {
            "DigiKeyProductNumber": "296-CUT-ND",
            "PackageType": {"Name": "Cut Tape (CT)"},
            "QuantityAvailableforPackageType": 500,
            "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 1.02}],
        },
    ]
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": [product]}))

    parts = await service.providers["digikey"].search("LM317")
    assert parts[0].distributor_pn == "296-CUT-ND"
    assert parts[0].packaging == "Cut Tape (CT)"


@respx.mock
async def test_token_failure_is_reported_as_a_provider_error(service):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(ProviderError, match="token request"):
        await service.providers["digikey"].search("LM317")


@respx.mock
async def test_http_error_is_reported_as_a_provider_error(service):
    mock_token()
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ProviderError, match="HTTP 500"):
        await service.providers["digikey"].search("LM317")


@respx.mock
async def test_details_escapes_the_part_number(service):
    mock_token()
    route = respx.get(
        "https://api.digikey.com/products/v4/search/LM317T%2FNOPB/productdetails"
    ).mock(return_value=httpx.Response(200, json={"Product": SAMPLE_PRODUCT}))

    part = await service.providers["digikey"].details("LM317T/NOPB")
    assert route.called
    assert part is not None
    assert part.mpn == "LM317T/NOPB"


@respx.mock
async def test_details_reports_an_unknown_part_as_no_result(service):
    mock_token()
    respx.get("https://api.digikey.com/products/v4/search/NOT-A-REAL-PART/productdetails").mock(
        return_value=httpx.Response(404, json={"detail": "Requested Product Not Found"})
    )

    # A part the distributor does not carry is an answer, not a broken source.
    assert await service.providers["digikey"].details("NOT-A-REAL-PART") is None


@respx.mock
async def test_search_still_reports_a_404_as_an_error(service):
    mock_token()
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(404, text="gone"))
    with pytest.raises(ProviderError):
        await service.providers["digikey"].search("LM317")


@respx.mock
async def test_protocol_relative_datasheet_gets_a_scheme(service):
    mock_token()
    product: dict[str, object] = dict(SAMPLE_PRODUCT)
    product["DatasheetUrl"] = "//mm.digikey.com/volume0/docs/lm317.pdf"
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"Products": [product]}))

    parts = await service.providers["digikey"].search("LM317")
    assert parts[0].datasheet_url == "https://mm.digikey.com/volume0/docs/lm317.pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//host/a.pdf", "https://host/a.pdf"),
        ("https://host/a.pdf", "https://host/a.pdf"),
        ("http://host/a.pdf", "http://host/a.pdf"),
        ("", None),
        (None, None),
    ],
)
def test_absolute_url(raw, expected):
    assert absolute_url(raw) == expected
