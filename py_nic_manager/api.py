"""Headless Python API for Py NIC Manager."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .admin import is_admin as default_admin_checker
from .backends import BaseBackend, get_backend
from .io import export_snapshot as write_snapshot
from .io import import_snapshot as read_snapshot
from .models import AdapterInfo, AddressInfo, CommandResult, NetworkSnapshot, OperationPlan, RouteInfo
from .validation import parse_csv, validate_ip, validate_network, validate_prefix


AdapterRef = AdapterInfo | str | int
RouteRef = RouteInfo | str
SnapshotRef = NetworkSnapshot | str | Path

ADAPTER_SORT_COLUMNS = {"index", "name", "status", "forwarding", "ipv4", "mac", "gateway", "dns", "kind"}
ROUTE_SORT_COLUMNS = {
    "destination",
    "gateway",
    "interface",
    "route_metric",
    "interface_metric",
    "effective_metric",
    "protocol",
    "table",
}


class PrivilegeError(PermissionError):
    """Raised when a mutating API call needs elevated privileges."""


class NetworkManager:
    """Programmatic interface for the same network operations exposed by the GUI."""

    def __init__(
        self,
        backend: BaseBackend | None = None,
        *,
        dry_run: bool = False,
        admin_checker: Callable[[], bool] = default_admin_checker,
    ) -> None:
        self.backend = backend or get_backend()
        if dry_run:
            self.backend.dry_run = True
        self._admin_checker = admin_checker

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def dry_run(self) -> bool:
        return self.backend.dry_run

    @property
    def is_admin(self) -> bool:
        return bool(self._admin_checker())

    def list_adapters(self, *, sort_by: str | None = None, descending: bool = False) -> list[AdapterInfo]:
        adapters = self.backend.list_adapters()
        if sort_by is None:
            return adapters
        return sort_adapters(adapters, sort_by=sort_by, descending=descending)

    def list_routes(self, *, sort_by: str | None = None, descending: bool = False) -> list[RouteInfo]:
        routes = self.backend.list_routes()
        if sort_by is None:
            return routes
        return sort_routes(routes, sort_by=sort_by, descending=descending)

    def get_snapshot(self, *, concurrent: bool = True) -> NetworkSnapshot:
        if not concurrent:
            return self.backend.get_snapshot()
        with ThreadPoolExecutor(max_workers=2) as executor:
            adapters_future = executor.submit(self.backend.list_adapters)
            routes_future = executor.submit(self.backend.list_routes)
            return NetworkSnapshot(
                platform=self.backend.name,
                adapters=adapters_future.result(),
                routes=routes_future.result(),
            )

    def export_snapshot(self, path: str | Path, snapshot: NetworkSnapshot | None = None) -> Path:
        target = Path(path)
        write_snapshot(snapshot or self.get_snapshot(), target)
        return target

    def import_snapshot(self, path: str | Path) -> NetworkSnapshot:
        return read_snapshot(path)

    def plan_apply_snapshot(
        self,
        snapshot: SnapshotRef,
        *,
        allow_platform_mismatch: bool = False,
    ) -> OperationPlan:
        loaded = self._coerce_snapshot(snapshot)
        if loaded.platform and loaded.platform != self.backend.name and not allow_platform_mismatch:
            raise ValueError(
                f"Snapshot platform is {loaded.platform}, but this system uses "
                f"the {self.backend.name} backend. Pass allow_platform_mismatch=True "
                "to apply it best-effort."
            )
        return self.backend.plan_snapshot_apply(loaded)

    def apply_snapshot(
        self,
        snapshot: SnapshotRef,
        *,
        allow_platform_mismatch: bool = False,
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_apply_snapshot(snapshot, allow_platform_mismatch=allow_platform_mismatch),
            require_admin=require_admin,
        )

    def find_adapter(self, adapter: AdapterRef) -> AdapterInfo:
        if isinstance(adapter, AdapterInfo):
            return adapter
        adapters = self.backend.list_adapters()
        if isinstance(adapter, int):
            try:
                return adapters[adapter]
            except IndexError as exc:
                raise LookupError(f"No adapter exists at index {adapter}.") from exc

        text = str(adapter).strip()
        for current in adapters:
            if current.id == text or current.name == text:
                return current
        lowered = text.lower()
        for current in adapters:
            if current.id.lower() == lowered or current.name.lower() == lowered:
                return current
        raise LookupError(f"Adapter not found: {text}")

    def find_route(self, route: RouteRef, *, gateway: str = "", interface: str = "") -> RouteInfo:
        if isinstance(route, RouteInfo):
            return route
        destination = validate_network(str(route))
        gateway_filter = validate_ip(gateway, allow_empty=True)
        interface_filter = interface.strip()
        matches = [
            current
            for current in self.backend.list_routes()
            if _route_destination_matches(current.destination, destination)
            and (not gateway_filter or current.gateway.lower() == gateway_filter.lower())
            and (not interface_filter or current.interface.lower() == interface_filter.lower())
        ]
        if not matches:
            raise LookupError(f"Route not found: {destination}")
        if len(matches) > 1:
            raise LookupError("Route selector is ambiguous; specify gateway and interface.")
        return matches[0]

    def suggest_loopback_value(self, adapters: list[AdapterInfo] | None = None) -> str:
        return suggest_loopback_value(self.backend.name, adapters or self.backend.list_adapters())

    def plan_update_adapter(
        self,
        adapter: AdapterRef,
        *,
        address: AddressInfo | str | None = None,
        prefix_length: int | str | None = None,
        gateway: str = "",
        dns_servers: Iterable[str] | str | None = None,
        mac: str = "",
        dhcp_enabled: bool = False,
    ) -> OperationPlan:
        current = self.find_adapter(adapter)
        return self.backend.plan_adapter_update(
            current,
            _coerce_address(address, prefix_length),
            validate_ip(gateway, allow_empty=True),
            _coerce_dns_servers(dns_servers),
            mac.strip(),
            bool(dhcp_enabled),
        )

    def update_adapter(
        self,
        adapter: AdapterRef,
        *,
        address: AddressInfo | str | None = None,
        prefix_length: int | str | None = None,
        gateway: str = "",
        dns_servers: Iterable[str] | str | None = None,
        mac: str = "",
        dhcp_enabled: bool = False,
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_update_adapter(
                adapter,
                address=address,
                prefix_length=prefix_length,
                gateway=gateway,
                dns_servers=dns_servers,
                mac=mac,
                dhcp_enabled=dhcp_enabled,
            ),
            require_admin=require_admin,
        )

    def plan_set_adapter_forwarding(self, adapter: AdapterRef, enabled: bool) -> OperationPlan:
        return self.backend.plan_adapter_forwarding_update(self.find_adapter(adapter), bool(enabled))

    def set_adapter_forwarding(
        self,
        adapter: AdapterRef,
        enabled: bool,
        *,
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_set_adapter_forwarding(adapter, enabled),
            require_admin=require_admin,
        )

    def plan_create_loopback(self, name: str | None = None) -> OperationPlan:
        value = (name or self.suggest_loopback_value()).strip()
        if not value:
            raise ValueError("A loopback adapter name or alias is required.")
        return self.backend.plan_loopback_create(value)

    def create_loopback(self, name: str | None = None, *, require_admin: bool = True) -> list[CommandResult]:
        return self.run_plan(self.plan_create_loopback(name), require_admin=require_admin)

    def plan_delete_loopback(self, adapter: AdapterRef) -> OperationPlan:
        current = self.find_adapter(adapter)
        if not current.is_loopback:
            raise ValueError("The selected adapter is not marked as loopback.")
        return self.backend.plan_loopback_delete(current)

    def delete_loopback(self, adapter: AdapterRef, *, require_admin: bool = True) -> list[CommandResult]:
        return self.run_plan(self.plan_delete_loopback(adapter), require_admin=require_admin)

    def plan_update_loopback(
        self,
        adapter: AdapterRef,
        *,
        address: AddressInfo | str | None = None,
        prefix_length: int | str | None = None,
        gateway: str = "",
        dns_servers: Iterable[str] | str | None = None,
        mac: str = "",
        dhcp_enabled: bool = False,
    ) -> OperationPlan:
        current = self.find_adapter(adapter)
        if not current.is_loopback:
            raise ValueError("The selected adapter is not marked as loopback.")
        return self.plan_update_adapter(
            current,
            address=address,
            prefix_length=prefix_length,
            gateway=gateway,
            dns_servers=dns_servers,
            mac=mac,
            dhcp_enabled=dhcp_enabled,
        )

    def update_loopback(
        self,
        adapter: AdapterRef,
        *,
        address: AddressInfo | str | None = None,
        prefix_length: int | str | None = None,
        gateway: str = "",
        dns_servers: Iterable[str] | str | None = None,
        mac: str = "",
        dhcp_enabled: bool = False,
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_update_loopback(
                adapter,
                address=address,
                prefix_length=prefix_length,
                gateway=gateway,
                dns_servers=dns_servers,
                mac=mac,
                dhcp_enabled=dhcp_enabled,
            ),
            require_admin=require_admin,
        )

    def plan_add_route(
        self,
        route: RouteInfo | str,
        *,
        gateway: str = "",
        interface: str = "",
        metric: int | str | None = None,
    ) -> OperationPlan:
        return self.backend.plan_route_add(_coerce_route(route, gateway=gateway, interface=interface, metric=metric))

    def add_route(
        self,
        route: RouteInfo | str,
        *,
        gateway: str = "",
        interface: str = "",
        metric: int | str | None = None,
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_add_route(route, gateway=gateway, interface=interface, metric=metric),
            require_admin=require_admin,
        )

    def plan_update_route(
        self,
        old_route: RouteRef,
        new_route: RouteInfo | str,
        *,
        old_gateway: str = "",
        old_interface: str = "",
        gateway: str = "",
        interface: str = "",
        metric: int | str | None = None,
    ) -> OperationPlan:
        current = self.find_route(old_route, gateway=old_gateway, interface=old_interface)
        replacement = _coerce_route(new_route, gateway=gateway, interface=interface, metric=metric)
        return self.backend.plan_route_update(current, replacement)

    def update_route(
        self,
        old_route: RouteRef,
        new_route: RouteInfo | str,
        *,
        old_gateway: str = "",
        old_interface: str = "",
        gateway: str = "",
        interface: str = "",
        metric: int | str | None = None,
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_update_route(
                old_route,
                new_route,
                old_gateway=old_gateway,
                old_interface=old_interface,
                gateway=gateway,
                interface=interface,
                metric=metric,
            ),
            require_admin=require_admin,
        )

    def plan_delete_route(self, route: RouteRef, *, gateway: str = "", interface: str = "") -> OperationPlan:
        return self.backend.plan_route_delete(self.find_route(route, gateway=gateway, interface=interface))

    def delete_route(
        self,
        route: RouteRef,
        *,
        gateway: str = "",
        interface: str = "",
        require_admin: bool = True,
    ) -> list[CommandResult]:
        return self.run_plan(
            self.plan_delete_route(route, gateway=gateway, interface=interface),
            require_admin=require_admin,
        )

    def run_plan(self, plan: OperationPlan, *, require_admin: bool = True) -> list[CommandResult]:
        if require_admin and not self.backend.dry_run and not self.is_admin:
            raise PrivilegeError(
                "This action changes system network settings. Run as Administrator/root "
                "or create the manager with dry_run=True to preview commands safely."
            )
        return self.backend.run_plan(plan)

    def _coerce_snapshot(self, snapshot: SnapshotRef) -> NetworkSnapshot:
        if isinstance(snapshot, NetworkSnapshot):
            return snapshot
        return self.import_snapshot(snapshot)


def sort_adapters(
    adapters: list[AdapterInfo],
    *,
    sort_by: str = "index",
    descending: bool = False,
) -> list[AdapterInfo]:
    if sort_by not in ADAPTER_SORT_COLUMNS:
        raise ValueError(f"Unsupported adapter sort column: {sort_by}")
    items = list(enumerate(adapters))
    ordered = sorted(
        items,
        key=lambda item: adapter_sort_key(item[1], sort_by=sort_by, index=item[0]),
        reverse=descending,
    )
    return [adapter for _index, adapter in ordered]


def sort_routes(
    routes: list[RouteInfo],
    *,
    sort_by: str = "destination",
    descending: bool = False,
) -> list[RouteInfo]:
    if sort_by not in ROUTE_SORT_COLUMNS:
        raise ValueError(f"Unsupported route sort column: {sort_by}")
    return sorted(routes, key=lambda route: route_sort_key(route, sort_by=sort_by), reverse=descending)


def adapter_sort_key(adapter: AdapterInfo, *, sort_by: str = "index", index: int = 0) -> tuple:
    if sort_by == "index":
        return (0, int(index))
    ipv4 = _first_ipv4(adapter)
    values = {
        "name": adapter.name,
        "status": adapter.status,
        "forwarding": _format_forwarding(adapter.forwarding_enabled),
        "ipv4": "" if ipv4 is None else _format_address(ipv4),
        "mac": adapter.mac,
        "gateway": ", ".join(adapter.gateways),
        "dns": ", ".join(adapter.dns_servers),
        "kind": "Loopback" if adapter.is_loopback else "Physical",
    }
    if sort_by == "ipv4" and ipv4 is not None:
        return _ip_or_text_sort_key(ipv4.address)
    return _text_sort_key(values.get(sort_by, ""))


def route_sort_key(route: RouteInfo, *, sort_by: str = "destination") -> tuple:
    if sort_by == "destination":
        return _network_sort_key(route.destination)
    if sort_by == "gateway":
        return _ip_or_text_sort_key(route.gateway)
    if sort_by == "route_metric":
        return _optional_int_sort_key(route.metric)
    if sort_by == "interface_metric":
        return _optional_int_sort_key(route.interface_metric)
    if sort_by == "effective_metric":
        return _optional_int_sort_key(route.effective_metric)
    values = {
        "interface": route.interface,
        "protocol": route.protocol,
        "table": route.table,
    }
    return _text_sort_key(values.get(sort_by, ""))


def suggest_loopback_value(backend_name: str, adapters: list[AdapterInfo]) -> str:
    if backend_name in {"macOS", "POSIX"}:
        used_addresses = {
            address.address
            for adapter in adapters
            for address in adapter.addresses
            if address.family.lower() == "ipv4"
        }
        for host in range(2, 255):
            candidate = f"127.0.0.{host}"
            if candidate not in used_addresses:
                return f"{candidate}/32"
        return "127.0.1.1/32"

    used_names = {adapter.name.strip().lower() for adapter in adapters}
    index = 0
    while True:
        candidate = f"py-loopback{index}"
        if candidate.lower() not in used_names:
            return candidate
        index += 1


def _coerce_address(address: AddressInfo | str | None, prefix_length: int | str | None) -> AddressInfo | None:
    if address is None:
        return None
    if isinstance(address, AddressInfo):
        return address
    text = str(address).strip()
    if not text:
        return None
    if "/" in text:
        interface = ipaddress.ip_interface(text)
        return AddressInfo(
            address=str(interface.ip),
            prefix_length=int(interface.network.prefixlen),
            family=f"ipv{interface.ip.version}",
        )
    return AddressInfo(
        address=validate_ip(text),
        prefix_length=validate_prefix(str(prefix_length or "24")),
        family="ipv4",
    )


def _coerce_dns_servers(dns_servers: Iterable[str] | str | None) -> list[str]:
    if dns_servers is None:
        return []
    if isinstance(dns_servers, str):
        values = parse_csv(dns_servers)
    else:
        values = [str(item).strip() for item in dns_servers if str(item).strip()]
    return [validate_ip(item) for item in values]


def _coerce_route(
    route: RouteInfo | str,
    *,
    gateway: str = "",
    interface: str = "",
    metric: int | str | None = None,
) -> RouteInfo:
    if isinstance(route, RouteInfo):
        return route
    metric_value = None if metric in (None, "") else int(metric)
    return RouteInfo(
        destination=validate_network(str(route)),
        gateway=validate_ip(gateway, allow_empty=True),
        interface=interface.strip(),
        metric=metric_value,
        family="ipv4",
    )


def _first_ipv4(adapter: AdapterInfo) -> AddressInfo | None:
    return next((item for item in adapter.addresses if item.family.lower() == "ipv4"), None)


def _format_address(address: AddressInfo) -> str:
    if address.prefix_length is None:
        return address.address
    return f"{address.address}/{address.prefix_length}"


def _format_forwarding(value: bool | None) -> str:
    if value is None:
        return "Unknown"
    return "Enabled" if value else "Disabled"


def _network_sort_key(value: str) -> tuple:
    text = value.strip()
    if not text:
        return (1, 0, 0, "")
    if text.lower() == "default":
        return (0, 0, 0, "default")
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return _text_sort_key(text)
    if network.version == 4:
        return (0, int(network.network_address), int(network.prefixlen), "")
    return (0, int(network.network_address), int(network.prefixlen), f"ipv{network.version}")


def _ip_or_text_sort_key(value: str) -> tuple:
    text = value.strip()
    if not text:
        return (1, 0, "")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return _text_sort_key(text)
    if ip.version == 4:
        return (0, int(ip), "")
    return (0, int(ip), str(ip.version))


def _route_destination_matches(left: str, right: str) -> bool:
    left_text = left.strip().lower()
    right_text = right.strip().lower()
    if left_text == right_text:
        return True
    default_values = {"default", "0.0.0.0/0"}
    return left_text in default_values and right_text in default_values


def _optional_int_sort_key(value: int | None) -> tuple[int, int]:
    if value is None:
        return (1, 0)
    return (0, int(value))


def _text_sort_key(value: str) -> tuple[int, str]:
    text = value.strip().lower()
    return (0 if text else 1, text)


__all__ = [
    "ADAPTER_SORT_COLUMNS",
    "ROUTE_SORT_COLUMNS",
    "AdapterRef",
    "NetworkManager",
    "PrivilegeError",
    "RouteRef",
    "SnapshotRef",
    "adapter_sort_key",
    "route_sort_key",
    "sort_adapters",
    "sort_routes",
    "suggest_loopback_value",
]
