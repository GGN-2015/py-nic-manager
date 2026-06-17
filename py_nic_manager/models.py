from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


CONFIG_SCHEMA_VERSION = 1


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
    is_loopback: bool = False
    forwarding_enabled: bool | None = None

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
            is_loopback=bool(data.get("is_loopback", False)),
            forwarding_enabled=_optional_bool(data.get("forwarding_enabled")),
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
            "is_loopback": self.is_loopback,
            "forwarding_enabled": self.forwarding_enabled,
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
class NetworkSnapshot:
    platform: str
    adapters: list[AdapterInfo]
    routes: list[RouteInfo]
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
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "captured_at": self.captured_at,
            "adapters": [item.to_dict() for item in self.adapters],
            "routes": [item.to_dict() for item in self.routes],
        }


@dataclass(slots=True)
class OperationPlan:
    title: str
    commands: list[list[str]]
    notes: list[str] = field(default_factory=list)

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
