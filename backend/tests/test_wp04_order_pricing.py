from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.commerce.order_pricing import OrderLineRequest, OrderPricingError, price_order

CATALOG = json.loads(
    (Path(__file__).resolve().parents[2] / "data/mock/wp04_commerce_catalog.json").read_text(
        encoding="utf-8"
    )
)


def test_server_catalog_computes_order_total():
    priced = price_order(
        service_id="service-food-preorder",
        requests=[OrderLineRequest("local-lunch-box", 1), OrderLineRequest("jasmine-tea", 2)],
        catalog=CATALOG,
    )
    assert priced.currency == "CNY"
    assert priced.total_minor == 8200
    assert priced.lines[0].unit_amount_minor == 5800


def test_unknown_sku_and_empty_order_are_rejected():
    with pytest.raises(OrderPricingError):
        price_order(service_id="service-food-preorder", requests=[], catalog=CATALOG)
    with pytest.raises(OrderPricingError):
        price_order(
            service_id="service-food-preorder",
            requests=[OrderLineRequest("forged-sku", 1)],
            catalog=CATALOG,
        )
