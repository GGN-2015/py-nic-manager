from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


CONFIG_SCHEMA_VERSION = 1
NIC_NATURE_PHYSICAL = "Physical NIC"
NIC_NATURE_LOOPBACK = "Loopback"
NIC_NATURE_VIRTUAL = "Non-loopback Virtual NIC"


def classify_nic_nature(*, is_loopback: bool, is_virtual: bool) -> str:
    if is_loopback:
        return NIC_NATURE_LOOPBACK
    if is_virtual:
        return NIC_NATURE_VIRTUAL
    return NIC_NATURE_PHYSICAL


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def summary(self) -> str:
        command_text = " ".join(self.command)
        output = (self.stderr or self.stdout).strip()
        if output:
            return f"{command_text}\n{output}"
        return command_text

    def error_message(self) -> str:
        output = (self.stderr or self.stdout).strip()
        if output:
            return _clean_command_error(output)
        return "Command failed without stderr/stdout. See the log for the full command."


def _clean_command_error(output: str) -> str:
    lines = output.strip().splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("At line:", "+ ", "CategoryInfo", "FullyQualifiedErrorId")):
            break
        if set(stripped) <= {"~"}:
            break
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return cleaned or output.strip()


@dataclass(slots=True)
class AddressInfo:
    address: str
    prefix_length: int | None = None
    family: str = "ipv4"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AddressInfo":
        return cls(
            address=str(data.get("address", "")),
            prefix_length=_optional_int(data.get("prefix_length")),
            family=str(data.get("family", "ipv4")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "prefix_length": self.prefix_length,
            "family": self.family,
        }


@dataclass(slots=True)
class AdapterInfo:
    id: str
    name: str
    description: str = ""
    mac: str = ""
    status: str = ""
    addresses: list[AddressInfo] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    dns_servers: list[str] = field(default_factory=list)
    dhcp_enabled: bool | None = None
    admin_enabled: bool | None = None
    is_loopback: bool = False
    is_virtual: bool = False
    virtual_kind: str = ""
    nic_nature: str = ""
    forwarding_enabled: bool | None = None
    ttl_exceeded_icmp_enabled: bool | None = None
    ics_compatible: bool | None = None
    ics_note: str = ""

    @property
    def nature(self) -> str:
        return self.nic_nature or classify_nic_nature(
            is_loopback=self.is_loopback,
            is_virtual=self.is_virtual,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdapterInfo":
        return cls(
            id=str(data.get("id", data.get("name", ""))),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            mac=str(data.get("mac", "")),
            status=str(data.get("status", "")),
            addresses=[
                AddressInfo.from_dict(item)
                for item in data.get("addresses", [])
                if isinstance(item, dict)
            ],
            gateways=[str(item) for item in data.get("gateways", [])],
            dns_servers=[str(item) for item in data.get("dns_servers", [])],
            dhcp_enabled=_optional_bool(data.get("dhcp_enabled")),
            admin_enabled=_optional_bool(data.get("admin_enabled")),
            is_loopback=bool(data.get("is_loopback", False)),
            is_virtual=bool(data.get("is_virtual", False)),
            virtual_kind=str(data.get("virtual_kind", "")),
            nic_nature=str(data.get("nic_nature", "")),
            forwarding_enabled=_optional_bool(data.get("forwarding_enabled")),
            ttl_exceeded_icmp_enabled=_optional_bool(data.get("ttl_exceeded_icmp_enabled")),
            ics_compatible=_optional_bool(data.get("ics_compatible")),
            ics_note=str(data.get("ics_note", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "mac": self.mac,
            "status": self.status,
            "addresses": [item.to_dict() for item in self.addresses],
            "gateways": self.gateways,
            "dns_servers": self.dns_servers,
            "dhcp_enabled": self.dhcp_enabled,
            "admin_enabled": self.admin_enabled,
            "is_loopback": self.is_loopback,
            "is_virtual": self.is_virtual,
            "virtual_kind": self.virtual_kind,
            "nic_nature": self.nature,
            "forwarding_enabled": self.forwarding_enabled,
            "ttl_exceeded_icmp_enabled": self.ttl_exceeded_icmp_enabled,
            "ics_compatible": self.ics_compatible,
            "ics_note": self.ics_note,
        }


@dataclass(slots=True)
class VirtualAdapterInfo:
    name: str
    kind: str
    status: str = ""
    address: str = ""
    source_cidr: str = ""
    nat_capable: bool = True
    persistent: bool = True
    managed: bool = True
    backend_id: str = ""
    admin_enabled: bool | None = None
    ics_compatible: bool | None = None
    ics_note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VirtualAdapterInfo":
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            status=str(data.get("status", "")),
            address=str(data.get("address", "")),
            source_cidr=str(data.get("source_cidr", "")),
            nat_capable=bool(_optional_bool(data.get("nat_capable")) if data.get("nat_capable") is not None else True),
            persistent=bool(_optional_bool(data.get("persistent")) if data.get("persistent") is not None else True),
            managed=bool(_optional_bool(data.get("managed")) if data.get("managed") is not None else True),
            backend_id=str(data.get("backend_id", "")),
            admin_enabled=_optional_bool(data.get("admin_enabled")),
            ics_compatible=_optional_bool(data.get("ics_compatible")),
            ics_note=str(data.get("ics_note", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "address": self.address,
            "source_cidr": self.source_cidr,
            "nat_capable": self.nat_capable,
            "persistent": self.persistent,
            "managed": self.managed,
            "backend_id": self.backend_id,
            "admin_enabled": self.admin_enabled,
            "ics_compatible": self.ics_compatible,
            "ics_note": self.ics_note,
        }


@dataclass(slots=True)
class RouteInfo:
    destination: str
    gateway: str = ""
    interface: str = ""
    metric: int | None = None
    interface_metric: int | None = None
    effective_metric: int | None = None
    family: str = "ipv4"
    protocol: str = ""
    table: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouteInfo":
        return cls(
            destination=str(data.get("destination", "")),
            gateway=str(data.get("gateway", "")),
            interface=str(data.get("interface", "")),
            metric=_optional_int(data.get("metric")),
            interface_metric=_optional_int(data.get("interface_metric")),
            effective_metric=_optional_int(data.get("effective_metric")),
            family=str(data.get("family", "ipv4")),
            protocol=str(data.get("protocol", "")),
            table=str(data.get("table", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "gateway": self.gateway,
            "interface": self.interface,
            "metric": self.metric,
            "interface_metric": self.interface_metric,
            "effective_metric": self.effective_metric,
            "family": self.family,
            "protocol": self.protocol,
            "table": self.table,
        }


@dataclass(slots=True)
class NatRule:
    name: str
    source_cidr: str
    outbound_interface: str = ""
    enabled: bool = True
    persistent: bool = True
    managed: bool = True
    family: str = "ipv4"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NatRule":
        return cls(
            name=str(data.get("name", "")),
            source_cidr=str(data.get("source_cidr", "")),
            outbound_interface=str(data.get("outbound_interface", "")),
            enabled=bool(_optional_bool(data.get("enabled")) if data.get("enabled") is not None else True),
            persistent=bool(_optional_bool(data.get("persistent")) if data.get("persistent") is not None else True),
            managed=bool(_optional_bool(data.get("managed")) if data.get("managed") is not None else True),
            family=str(data.get("family", "ipv4")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_cidr": self.source_cidr,
            "outbound_interface": self.outbound_interface,
            "enabled": self.enabled,
            "persistent": self.persistent,
            "managed": self.managed,
            "family": self.family,
        }


@dataclass(slots=True)
class NetworkSnapshot:
    platform: str
    adapters: list[AdapterInfo]
    routes: list[RouteInfo]
    nat_rules: list[NatRule] = field(default_factory=list)
    global_forwarding_enabled: bool | None = None
    virtual_adapters: list[VirtualAdapterInfo] = field(default_factory=list)
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
    schema_version: int = CONFIG_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NetworkSnapshot":
        return cls(
            schema_version=int(data.get("schema_version", CONFIG_SCHEMA_VERSION)),
            platform=str(data.get("platform", "")),
            captured_at=str(data.get("captured_at", "")),
            adapters=[
                AdapterInfo.from_dict(item)
                for item in data.get("adapters", [])
                if isinstance(item, dict)
            ],
            routes=[
                RouteInfo.from_dict(item)
                for item in data.get("routes", [])
                if isinstance(item, dict)
            ],
            nat_rules=[
                NatRule.from_dict(item)
                for item in data.get("nat_rules", [])
                if isinstance(item, dict)
            ],
            virtual_adapters=[
                VirtualAdapterInfo.from_dict(item)
                for item in data.get("virtual_adapters", [])
                if isinstance(item, dict)
            ],
            global_forwarding_enabled=_optional_bool(data.get("global_forwarding_enabled")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "captured_at": self.captured_at,
            "global_forwarding_enabled": self.global_forwarding_enabled,
            "adapters": [item.to_dict() for item in self.adapters],
            "virtual_adapters": [item.to_dict() for item in self.virtual_adapters],
            "routes": [item.to_dict() for item in self.routes],
            "nat_rules": [item.to_dict() for item in self.nat_rules],
        }


@dataclass(slots=True)
class OperationPlan:
    title: str
    commands: list[list[str]]
    notes: list[str] = field(default_factory=list)
    restart_required: bool = False

    def as_text(self) -> str:
        parts: list[str] = [self.title, ""]
        if self.notes:
            parts.extend(self.notes)
            parts.append("")
        parts.extend(" ".join(command) for command in self.commands)
        return "\n".join(parts).strip()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "enabled", "on"}:
        return True
    if text in {"0", "false", "no", "disabled", "off"}:
        return False
    return None
