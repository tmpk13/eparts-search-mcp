from __future__ import annotations

import hashlib
from urllib.parse import quote_plus

import httpx
import pytest
import respx

from eparts_search_mcp.providers.base import ProviderError
from eparts_search_mcp.providers.lcsc import canonical_value, sign

SEARCH_URL = "https://api.lcsc.com/rest/api/agent/product/v1/keywordsearch"

SAMPLE_PRODUCT = {
    "lcscProductNumber": "C22452",
    "manufacturer": {"id": 296, "name": "Texas Instruments"},
    "manufacturerProductNumber": "LM317T/NOPB",
    "package": "TO-220-3",
    "packageType": "Tube",
    "description": "1.5A Adjustable Positive Linear Regulator TO-220-3",
    "productDetailURL": "https://www.lcsc.com/product-detail/C22452.html",
    "datasheetURL": "https://datasheet.lcsc.com/lcsc/LM317T.pdf",
    "productPhotoURL": ["https://assets.lcsc.com/images/lcsc/900x900/LM317T_front.jpg"],
    "category": {
        "firstCatalogId": 24,
        "firstCatalogName": "Power Management",
        "secondCatalogId": 154,
        "secondCatalogName": "Linear Regulators",
        "thirdCatalogId": 802,
        "thirdCatalogName": "Adjustable LDO",
    },
    "isInStock": True,
    "minimumOrderQuantity": 10,
    "incrementQuantity": 10,
    "manufacturerStandardPackageQuantity": 50,
    "isOnSale": True,
    "quantityAvailable": "4210",
    "productParameters": [
        {"paramName": "Output Voltage", "paramValue": "1.25V~37V"},
        {"paramName": "Output Current", "paramValue": "1.5A"},
    ],
    "productPrice": {
        "lcscProductNumber": "C22452",
        "manufacturer": {"id": 296, "name": "Texas Instruments"},
        "manufacturerProductNumber": "LM317T/NOPB",
        "currency": "USD",
        "standardPricing": [
            {"breakQuantity": 100, "unitPrice": 0.55, "discountRate": 1.0},
            {"breakQuantity": 10, "unitPrice": 0.72, "discountRate": 1.0},
        ],
        "quantityAvailable": "4210",
        "lcscReelAvailable": True,
        "lcscReelFee": 3.0,
    },
}


def envelope(result, code: int = 200, message: str = "ok") -> dict:
    """Wrap a payload the way every LCSC interface answers."""
    return {"code": code, "message": message, "success": code == 200, "result": result}


def mock_search(*products) -> respx.Route:
    entries = list(products) or [SAMPLE_PRODUCT]
    return respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json=envelope({"productCount": len(entries), "products": entries})
        )
    )


def reference_signature(
    query: dict[str, str], key: str, secret: str, nonce: str, timestamp: str
) -> str:
    """The algorithm as published, rebuilt here rather than reused from the adapter."""
    material = f"key={key}&nonce={nonce}&secret={secret}&timestamp={timestamp}"
    encoded = "&".join(
        f"{quote_plus(name)}={quote_plus(value)}" for name, value in sorted(query.items())
    )
    return hashlib.sha256(f"{material}&{encoded}".encode()).hexdigest()


def test_signature_matches_the_published_algorithm():
    query = {"keyword": "LM317 TI", "limit": "10", "offset": "1"}
    expected = reference_signature(
        query, key="the-key", secret="the-secret", nonce="abc123", timestamp="1750000000"
    )
    assert (
        sign(query, key="the-key", secret="the-secret", nonce="abc123", timestamp="1750000000")
        == expected
    )


def test_the_secret_changes_the_signature_without_being_sent():
    query = {"keyword": "LM317"}
    first = sign(query, key="k", secret="one", nonce="n", timestamp="1")
    second = sign(query, key="k", secret="two", nonce="n", timestamp="1")
    assert first != second


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("plain", "plain"),
        (10, "10"),
        ({"b": 2, "a": 1}, '{"a":"1","b":"2"}'),
        ([1, 2], '["1","2"]'),
    ],
)
def test_canonical_value_serializes_predictably(value, expected):
    assert canonical_value(value) == expected


@respx.mock
async def test_search_normalizes_a_product(service):
    mock_search()
    parts = await service.providers["lcsc"].search("LM317", limit=5)

    assert len(parts) == 1
    part = parts[0]
    assert part.source == "lcsc"
    assert part.mpn == "LM317T/NOPB"
    assert part.manufacturer == "Texas Instruments"
    assert part.distributor_pn == "C22452"
    assert part.stock == 4210
    assert part.min_order_qty == 10
    assert part.packaging == "Tube"
    assert part.datasheet_url == "https://datasheet.lcsc.com/lcsc/LM317T.pdf"
    # Breaks arrive unordered, and the price ladder is only meaningful sorted.
    assert [b.quantity for b in part.price_breaks] == [10, 100]
    assert part.unit_price_at(100) == 0.55
    # Below the smallest break there is no price to report.
    assert part.unit_price_at(1) is None
    assert part.specs["Output Voltage"] == "1.25V~37V"
    assert part.specs["Category"] == "Adjustable LDO"
    assert part.specs["Package"] == "TO-220-3"
    assert part.specs["Order increment"] == "10"
    # A per order surcharge that the unit price comparison cannot express.
    assert part.specs["Reel packaging fee"] == "3.0 USD"


@respx.mock
async def test_the_request_is_signed_over_the_parameters_actually_sent(service):
    route = mock_search()
    # A keyword with spaces and reserved characters, since the signature is
    # taken over the encoded query and must survive the round trip.
    await service.providers["lcsc"].search("10k ohm 1% & 0402", limit=5)

    request = route.calls.last.request
    headers = request.headers
    assert headers["key"] == "test-key-id"
    assert len(headers["nonce"]) == 16

    expected = reference_signature(
        dict(request.url.params),
        key="test-key-id",
        secret="test-secret",
        nonce=headers["nonce"],
        timestamp=headers["timestamp"],
    )
    assert headers["signature"] == expected
    # The secret authenticates the call but must never leave the process.
    assert "test-secret" not in str(request.url)


@respx.mock
async def test_search_asks_for_pricing_alongside_basic_data(service):
    route = mock_search()
    await service.providers["lcsc"].search("LM317", limit=5)

    params = dict(route.calls.last.request.url.params)
    assert params["returnInformation"] == "All"
    assert params["currency"] == "USD"
    assert params["offset"] == "1"


@respx.mock
async def test_a_business_error_in_a_200_body_is_raised(service):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json=envelope(None, code=4005, message="Signature is invalid.")
        )
    )
    with pytest.raises(ProviderError, match="Signature is invalid"):
        await service.providers["lcsc"].search("LM317")


@respx.mock
async def test_a_spent_quota_names_whose_limit_was_hit(service):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope(None, code=430, message="The client exceeded the daily request quota."),
        )
    )
    with pytest.raises(ProviderError, match="LCSC's own limit"):
        await service.providers["lcsc"].search("LM317")


@respx.mock
async def test_manufacturer_is_folded_into_the_keyword(service):
    other_brand = {**SAMPLE_PRODUCT, "manufacturer": {"id": 9, "name": "UMW"}}
    route = mock_search(SAMPLE_PRODUCT, other_brand)

    parts = await service.providers["lcsc"].search("LM317", manufacturer="Texas Instruments")

    # There is no manufacturer parameter, so the brand rides in the keyword and
    # anything the search still returns from another brand is dropped.
    assert route.calls.last.request.url.params["keyword"] == "LM317 Texas Instruments"
    assert [part.manufacturer for part in parts] == ["Texas Instruments"]


@respx.mock
async def test_in_stock_only_drops_products_the_api_still_returns(service):
    out_of_stock = {
        **SAMPLE_PRODUCT,
        "lcscProductNumber": "C99999",
        "isInStock": False,
        "quantityAvailable": "0",
    }
    route = mock_search(SAMPLE_PRODUCT, out_of_stock)

    parts = await service.providers["lcsc"].search("LM317", in_stock_only=True)

    assert route.calls.last.request.url.params["inStockOnly"] == "true"
    assert [part.distributor_pn for part in parts] == ["C22452"]


@respx.mock
async def test_details_accepts_an_lcsc_product_number(service):
    mock_search()
    part = await service.providers["lcsc"].details("C22452")
    assert part is not None
    assert part.mpn == "LM317T/NOPB"


@respx.mock
async def test_details_ignores_a_fuzzy_match(service):
    # Keyword search is the only lookup available, and it answers with near
    # matches, which must not be passed off as the part that was asked for.
    mock_search()
    assert await service.providers["lcsc"].details("LM337T") is None


@respx.mock
async def test_a_missing_status_code_is_treated_as_a_failure(service):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"products": []}))
    with pytest.raises(ProviderError, match="status code"):
        await service.providers["lcsc"].search("LM317")


@respx.mock
async def test_the_test_environment_is_a_separate_host(config):
    from dataclasses import replace

    from eparts_search_mcp.service import SearchService

    sandboxed = replace(config, lcsc=replace(config.lcsc, sandbox=True))
    service = SearchService(sandboxed)
    try:
        route = respx.get("https://fatapi.lcsc.com/rest/api/agent/product/v1/keywordsearch").mock(
            return_value=httpx.Response(200, json=envelope({"productCount": 0, "products": []}))
        )
        await service.providers["lcsc"].search("LM317")
        assert route.called
    finally:
        await service.aclose()
