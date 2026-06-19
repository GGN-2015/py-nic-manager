from __future__ import annotations

from dataclasses import dataclass

from .models import RouteInfo


WINDOWS_ONLY_ROUTE_COLUMNS = {"interface_metric", "effective_metric"}


@dataclass(frozen=True, slots=True)
class RouteColumn:
    key: str
    label: str
    width: int
    centered: bool = False


def route_table_columns(backend_name: str) -> list[RouteColumn]:
    columns = [
        RouteColumn("destination", "Destination", 190),
        RouteColumn("gateway", "Gateway", 145),
        RouteColumn("interface", "Interface", 155),
        RouteColumn("route_metric", "Route Metric", 105, True),
        RouteColumn("interface_metric", "Interface Metric", 125, True),
        RouteColumn("effective_metric", "Effective Metric", 125, True),
        RouteColumn("protocol", "Protocol", 95),
        RouteColumn("table", "Table", 80),
    ]
    if backend_name == "Windows":
        return columns
    return [column for column in columns if column.key not in WINDOWS_ONLY_ROUTE_COLUMNS]


def route_cell_text(route: RouteInfo, column: str) -> str:
    values = {
        "destination": route.destination,
        "gateway": route.gateway,
        "interface": route.interface,
        "route_metric": "" if route.metric is None else str(route.metric),
        "interface_metric": "" if route.interface_metric is None else str(route.interface_metric),
        "effective_metric": "" if route.effective_metric is None else str(route.effective_metric),
        "protocol": route.protocol,
        "table": route.table,
    }
    return values.get(column, "")
