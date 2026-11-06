from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class OrderPricingError(ValueError):
    pass


@dataclass(frozen=True)
class OrderLineRequest:
    sku: str
    quantity: int


@dataclass(frozen=True)
class PricedOrderLine:
    sku: str
    quantity: int
    unit_amount_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class PricedOrder:
    service_id: str
    currency: str
    lines: tuple[PricedOrderLine, ...]
    total_minor: int


def price_order(
    *,
    service_id: str,
    requests: list[OrderLineRequest],
    catalog: Mapping,
) -> PricedOrder:
    if not requests:
        raise OrderPricingError("at least one order item is required")
    service = catalog.get("order_services", {}).get(service_id)
    if not service:
        raise OrderPricingError("unknown or non-tradable service")
    item_catalog = service.get("items", {})
    lines: list[PricedOrderLine] = []
    for request in requests:
        if request.quantity < 1 or request.quantity > 99:
            raise OrderPricingError("quantity must be 1..99")
        item = item_catalog.get(request.sku)
        if not item:
            raise OrderPricingError(f"unknown sku: {request.sku}")
        unit = int(item["unit_amount_minor"])
        line_total = unit * request.quantity
        lines.append(PricedOrderLine(request.sku, request.quantity, unit, line_total))
    total = sum(line.line_total_minor for line in lines)
    return PricedOrder(service_id, str(service["currency"]).upper(), tuple(lines), total)
