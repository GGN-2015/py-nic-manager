from __future__ import annotations

import locale
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable

from .models import AdapterInfo, AddressInfo, CommandResult, NetworkSnapshot, OperationPlan, RouteInfo
from .validation import netmask_to_prefix, normalize_mac, prefix_to_netmask


class BackendError(RuntimeError):
    pass


class BaseBackend(ABC):
    name = "Unknown"

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    @abstractmethod
    def list_adapters(self) -> list[AdapterInfo]:
        raise NotImplementedError

    @abstractmethod
    def list_routes(self) -> list[RouteInfo]:
        raise NotImplementedError

    @abstractmethod
    def plan_adapter_update(
        self,
        adapter: AdapterInfo,
        address: AddressInfo | None,
        gateway: str,
        dns_servers: list[str],
        mac: str,
        dhcp_enabled: bool,
    ) -> OperationPlan:
        raise NotImplementedError

    @abstractmethod
    def plan_route_add(self, route: RouteInfo) -> OperationPlan:
        raise NotImplementedError

    @abstractmethod
    def plan_route_delete(self, route: RouteInfo) -> OperationPlan:
        raise NotImplementedError

    @abstractmethod
    def plan_route_update(self, old_route: RouteInfo, new_route: RouteInfo) -> OperationPlan:
        raise NotImplementedError

    @abstractmethod
    def plan_loopback_create(self, name: str) -> OperationPlan:
        raise NotImplementedError

    @abstractmethod
    def plan_loopback_delete(self, adapter: AdapterInfo) -> OperationPlan:
        raise NotImplementedError

    def get_snapshot(self) -> NetworkSnapshot:
        return NetworkSnapshot(
            platform=self.name,
            adapters=self.list_adapters(),
            routes=self.list_routes(),
        )

    def plan_snapshot_apply(self, snapshot: NetworkSnapshot) -> OperationPlan:
        current_adapters = self.list_adapters()
        current_routes = [route for route in self.list_routes() if route.family.lower() == "ipv4"]
        adapters_by_id = {adapter.id: adapter for adapter in current_adapters}
        adapters_by_name = {adapter.name: adapter for adapter in current_adapters}
        commands: list[list[str]] = []
        notes: list[str] = []

        for saved in snapshot.adapters:
            current = adapters_by_id.get(saved.id) or adapters_by_name.get(saved.name)
            if current is None:
                notes.append(f"Skipped missing adapter: {saved.name or saved.id}")
                continue
            address = next(
                (item for item in saved.addresses if item.family.lower() == "ipv4"),
                None,
            )
            plan = self.plan_adapter_update(
                current,
                address,
                saved.gateways[0] if saved.gateways else "",
                saved.dns_servers,
                saved.mac,
                bool(saved.dhcp_enabled),
            )
            commands.extend(plan.commands)
            notes.extend(plan.notes)

        target_routes = [route for route in snapshot.routes if route.family.lower() == "ipv4"]
        target_route_keys = {_route_key(route) for route in target_routes}
        current_route_keys = {_route_key(route) for route in current_routes}

        preserved_count = 0
        for route in current_routes:
            if self.should_preserve_route_on_snapshot_apply(route):
                preserved_count += 1
                continue
            if _route_key(route) not in target_route_keys:
                commands.extend(self.plan_route_delete(route).commands)

        for route in target_routes:
            if _route_key(route) not in current_route_keys:
                commands.extend(self.plan_route_add(route).commands)

        if preserved_count:
            notes.append(
                f"Preserved {preserved_count} system-generated/local route(s) "
                "that should be managed by the operating system."
            )

        return OperationPlan(
            title="Apply imported network configuration",
            commands=_dedupe_commands(commands),
            notes=notes,
        )

    def should_preserve_route_on_snapshot_apply(self, route: RouteInfo) -> bool:
        destination = route.destination.lower()
        protocol = route.protocol.lower()
        if protocol in {"kernel", "local"}:
            return True
        if destination.startswith(("127.", "127.0.0.0/", "224.", "255.")):
            return True
        if route.interface.lower() in {"lo", "lo0", "loopback pseudo-interface 1"}:
            return True
        return False

    def run_plan(self, plan: OperationPlan) -> list[CommandResult]:
        results: list[CommandResult] = []
        for command in plan.commands:
            results.append(self.run(command))
        return results

    def run(self, command: list[str]) -> CommandResult:
        if self.dry_run:
            return CommandResult(command=command, returncode=0)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=90,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                returncode=124,
                stdout=decode_command_output(exc.stdout or b""),
                stderr=decode_command_output(exc.stderr or b"") or "Command timed out after 90 seconds.",
            )
        except FileNotFoundError as exc:
            raise BackendError(f"Command not found: {command[0]}") from exc
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=decode_command_output(completed.stdout),
            stderr=decode_command_output(completed.stderr),
        )

    def run_json(self, command: list[str]) -> object:
        result = self.run(command)
        if not result.ok:
            raise BackendError(result.summary())
        text = result.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BackendError(f"Failed to parse JSON from {' '.join(command)}") from exc


class WindowsBackend(BaseBackend):
    name = "Windows"

    def list_adapters(self) -> list[AdapterInfo]:
        script = r"""
$adapters = Get-NetAdapter -IncludeHidden | Sort-Object -Property InterfaceIndex | ForEach-Object {
  $adapter = $_
  $config = Get-NetIPConfiguration -InterfaceIndex $adapter.InterfaceIndex -ErrorAction SilentlyContinue
  $dns = Get-DnsClientServerAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
  [pscustomobject]@{
    id = [string]$adapter.PnPDeviceID
    name = [string]$adapter.Name
    description = [string]$adapter.InterfaceDescription
    mac = [string]$adapter.MacAddress
    status = [string]$adapter.Status
    dhcp_enabled = [bool]($config.NetIPv4Interface.Dhcp -eq "Enabled")
    is_loopback = [bool]($adapter.InterfaceDescription -match "Loopback|KM-TEST|Npcap Loopback")
    addresses = @($config.IPv4Address | ForEach-Object {
      [pscustomobject]@{
        address = [string]$_.IPAddress
        prefix_length = [int]$_.PrefixLength
        family = "ipv4"
      }
    })
    gateways = @($config.IPv4DefaultGateway | ForEach-Object { [string]$_.NextHop })
    dns_servers = @($dns.ServerAddresses | ForEach-Object { [string]$_ })
  }
}
$adapters | ConvertTo-Json -Depth 6
"""
        data = self.run_json(_powershell(script))
        return [AdapterInfo.from_dict(item) for item in _as_list(data)]

    def list_routes(self) -> list[RouteInfo]:
        script = r"""
Get-NetRoute -AddressFamily IPv4 |
  Sort-Object -Property DestinationPrefix, InterfaceAlias, NextHop |
  ForEach-Object {
    [pscustomobject]@{
      destination = [string]$_.DestinationPrefix
      gateway = [string]$_.NextHop
      interface = [string]$_.InterfaceAlias
      metric = [int]$_.RouteMetric
      family = "ipv4"
      protocol = [string]$_.Protocol
      table = ""
    }
  } | ConvertTo-Json -Depth 4
"""
        data = self.run_json(_powershell(script))
        return [RouteInfo.from_dict(item) for item in _as_list(data)]

    def plan_adapter_update(
        self,
        adapter: AdapterInfo,
        address: AddressInfo | None,
        gateway: str,
        dns_servers: list[str],
        mac: str,
        dhcp_enabled: bool,
    ) -> OperationPlan:
        commands: list[list[str]] = []
        notes: list[str] = []
        name = adapter.name
        if dhcp_enabled:
            commands.append(["netsh", "interface", "ip", "set", "address", f"name={name}", "source=dhcp"])
            commands.append(["netsh", "interface", "ip", "set", "dns", f"name={name}", "source=dhcp"])
        elif address:
            netmask = prefix_to_netmask(address.prefix_length or 24)
            cmd = [
                "netsh",
                "interface",
                "ip",
                "set",
                "address",
                f"name={name}",
                "static",
                address.address,
                netmask,
            ]
            if gateway:
                cmd.extend([gateway, "1"])
            commands.append(cmd)
            commands.extend(_windows_dns_commands(name, dns_servers))
        else:
            notes.append("Skipped IP update because no IPv4 address was provided.")

        clean_mac = mac.strip()
        if clean_mac and clean_mac != adapter.mac:
            normalized = normalize_mac(clean_mac, separator="")
            ps = (
                f'Set-NetAdapterAdvancedProperty -Name "{_ps_escape(name)}" '
                f'-RegistryKeyword "NetworkAddress" -RegistryValue "{normalized}"'
            )
            commands.append(_powershell(ps))
            commands.append(["netsh", "interface", "set", "interface", f"name={name}", "admin=disabled"])
            commands.append(["netsh", "interface", "set", "interface", f"name={name}", "admin=enabled"])
            notes.append("Changing a MAC address briefly disables and re-enables the adapter.")

        return OperationPlan("Update adapter", commands, notes)

    def plan_route_add(self, route: RouteInfo) -> OperationPlan:
        gateway = route.gateway if route.gateway else "0.0.0.0"
        metric = "" if route.metric is None else f" -RouteMetric {int(route.metric)}"
        interface = f' -InterfaceAlias "{_ps_escape(route.interface)}"' if route.interface else ""
        script = (
            f'New-NetRoute -DestinationPrefix "{_ps_escape(route.destination)}"'
            f"{interface} -NextHop \"{_ps_escape(gateway)}\"{metric} -PolicyStore ActiveStore"
        )
        return OperationPlan("Add route", [_powershell(script)])

    def plan_route_delete(self, route: RouteInfo) -> OperationPlan:
        gateway_filter = (
            f' | Where-Object {{ $_.NextHop -eq "{_ps_escape(route.gateway)}" }}'
            if route.gateway
            else ""
        )
        interface_filter = (
            f' | Where-Object {{ $_.InterfaceAlias -eq "{_ps_escape(route.interface)}" }}'
            if route.interface
            else ""
        )
        script = (
            f'Get-NetRoute -DestinationPrefix "{_ps_escape(route.destination)}"'
            f"{gateway_filter}{interface_filter} | Remove-NetRoute -Confirm:$false"
        )
        return OperationPlan("Delete route", [_powershell(script)])

    def plan_route_update(self, old_route: RouteInfo, new_route: RouteInfo) -> OperationPlan:
        return OperationPlan(
            "Update route",
            self.plan_route_delete(old_route).commands + self.plan_route_add(new_route).commands,
        )

    def plan_route_add_legacy(self, route: RouteInfo) -> OperationPlan:
        destination, mask = _windows_route_destination(route.destination)
        command = ["route", "-p", "add", destination, "mask", mask]
        command.append(route.gateway if route.gateway and route.gateway != "0.0.0.0" else "0.0.0.0")
        if route.interface:
            command.extend(["if", route.interface])
        if route.metric is not None:
            command.extend(["metric", str(route.metric)])
        return OperationPlan("Add route", [command])

    def plan_loopback_create(self, name: str) -> OperationPlan:
        command = [sys.executable, "-m", "py_nic_manager.windows_loopback", "create"]
        if name.strip():
            command.extend(["--name", name.strip()])
        return OperationPlan(
            "Create loopback adapter",
            [command],
            [
                "Windows creates a Microsoft KM-TEST Loopback Adapter with the built-in netloop driver.",
                "This uses Windows SetupAPI directly and does not require devcon.exe or the Windows Driver Kit.",
            ],
        )

    def plan_loopback_delete(self, adapter: AdapterInfo) -> OperationPlan:
        script = rf"""
$device = Get-PnpDevice |
  Where-Object {{ $_.InstanceId -eq "{_ps_escape(adapter.id)}" -or $_.FriendlyName -like "*{_ps_escape(adapter.name)}*" }} |
  Select-Object -First 1
if ($device) {{
  pnputil /remove-device $device.InstanceId
}} else {{
  throw "Loopback device not found."
}}
"""
        return OperationPlan("Delete loopback adapter", [_powershell(script)])


class LinuxBackend(BaseBackend):
    name = "Linux"

    def list_adapters(self) -> list[AdapterInfo]:
        data = self.run_json(["ip", "-j", "addr", "show"])
        routes = self.list_routes()
        dns_by_iface = self._dns_servers_by_iface()
        adapters: list[AdapterInfo] = []
        for item in _as_list(data):
            name = str(item.get("ifname", ""))
            addresses = [
                AddressInfo(
                    address=str(info.get("local", "")),
                    prefix_length=int(info.get("prefixlen", 0)),
                    family="ipv4" if info.get("family") == "inet" else "ipv6",
                )
                for info in item.get("addr_info", [])
                if info.get("family") in {"inet", "inet6"}
            ]
            adapters.append(
                AdapterInfo(
                    id=name,
                    name=name,
                    description=str(item.get("link_type", "")),
                    mac=str(item.get("address", "")),
                    status=str(item.get("operstate", "")),
                    addresses=addresses,
                    gateways=[
                        route.gateway
                        for route in routes
                        if route.interface == name and route.destination in {"default", "0.0.0.0/0"}
                    ],
                    dns_servers=dns_by_iface.get(name, []),
                    dhcp_enabled=None,
                    is_loopback=bool(item.get("link_type") in {"loopback", "dummy"} or name == "lo"),
                )
            )
        return adapters

    def list_routes(self) -> list[RouteInfo]:
        data = self.run_json(["ip", "-j", "route", "show", "table", "all"])
        routes: list[RouteInfo] = []
        for item in _as_list(data):
            destination = str(item.get("dst", "default"))
            routes.append(
                RouteInfo(
                    destination=destination,
                    gateway=str(item.get("gateway", "")),
                    interface=str(item.get("dev", "")),
                    metric=_optional_int(item.get("metric")),
                    family="ipv4",
                    protocol=str(item.get("protocol", "")),
                    table=str(item.get("table", "")),
                )
            )
        return routes

    def plan_adapter_update(
        self,
        adapter: AdapterInfo,
        address: AddressInfo | None,
        gateway: str,
        dns_servers: list[str],
        mac: str,
        dhcp_enabled: bool,
    ) -> OperationPlan:
        commands: list[list[str]] = []
        notes: list[str] = []
        iface = adapter.name

        if mac.strip() and mac.strip() != adapter.mac:
            commands.append(["ip", "link", "set", "dev", iface, "down"])
            commands.append(["ip", "link", "set", "dev", iface, "address", normalize_mac(mac, ":")])
            commands.append(["ip", "link", "set", "dev", iface, "up"])
            notes.append("Changing a MAC address briefly brings the interface down.")

        if dhcp_enabled:
            if shutil.which("nmcli"):
                connection = self._nmcli_connection_for_iface(iface)
                if connection:
                    commands.extend(
                        [
                            ["nmcli", "connection", "modify", connection, "ipv4.method", "auto"],
                            ["nmcli", "connection", "up", connection],
                        ]
                    )
                else:
                    notes.append("No NetworkManager connection was found for DHCP update.")
            else:
                notes.append("DHCP updates on Linux require NetworkManager (nmcli).")
        elif address:
            commands.append(["ip", "addr", "flush", "dev", iface, "scope", "global"])
            commands.append(["ip", "addr", "add", f"{address.address}/{address.prefix_length or 24}", "dev", iface])
            commands.append(["ip", "link", "set", "dev", iface, "up"])
            if gateway:
                commands.append(["ip", "-4", "route", "replace", "default", "via", gateway, "dev", iface])
        else:
            notes.append("Skipped IP update because no IPv4 address was provided.")

        if dns_servers:
            if shutil.which("nmcli"):
                connection = self._nmcli_connection_for_iface(iface)
                if connection:
                    commands.append(
                        [
                            "nmcli",
                            "connection",
                            "modify",
                            connection,
                            "ipv4.ignore-auto-dns",
                            "yes",
                            "ipv4.dns",
                            ",".join(dns_servers),
                        ]
                    )
                    commands.append(["nmcli", "connection", "up", connection])
                else:
                    notes.append("No NetworkManager connection was found for DNS update.")
            elif shutil.which("resolvectl"):
                commands.append(["resolvectl", "dns", iface, *dns_servers])
            else:
                notes.append("DNS updates require nmcli or resolvectl on Linux.")

        return OperationPlan("Update adapter", commands, notes)

    def plan_route_add(self, route: RouteInfo) -> OperationPlan:
        command = ["ip", "-4", "route", "replace", route.destination]
        if route.gateway:
            command.extend(["via", route.gateway])
        if route.interface:
            command.extend(["dev", route.interface])
        if route.metric is not None:
            command.extend(["metric", str(route.metric)])
        return OperationPlan("Add route", [command])

    def plan_route_delete(self, route: RouteInfo) -> OperationPlan:
        command = ["ip", "-4", "route", "del", route.destination]
        if route.gateway:
            command.extend(["via", route.gateway])
        if route.interface:
            command.extend(["dev", route.interface])
        return OperationPlan("Delete route", [command])

    def plan_route_update(self, old_route: RouteInfo, new_route: RouteInfo) -> OperationPlan:
        return OperationPlan(
            "Update route",
            self.plan_route_delete(old_route).commands + self.plan_route_add(new_route).commands,
        )

    def plan_loopback_create(self, name: str) -> OperationPlan:
        return OperationPlan(
            "Create loopback adapter",
            [
                ["ip", "link", "add", name, "type", "dummy"],
                ["ip", "link", "set", "dev", name, "up"],
            ],
            ["Linux uses a dummy interface as a configurable loopback-style virtual adapter."],
        )

    def plan_loopback_delete(self, adapter: AdapterInfo) -> OperationPlan:
        return OperationPlan("Delete loopback adapter", [["ip", "link", "delete", adapter.name]])

    def _dns_servers_by_iface(self) -> dict[str, list[str]]:
        if shutil.which("resolvectl"):
            result = self.run(["resolvectl", "dns"])
            if result.ok:
                return _parse_resolvectl_dns(result.stdout)
        return {}

    def _nmcli_connection_for_iface(self, iface: str) -> str:
        if not shutil.which("nmcli"):
            return ""
        result = self.run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])
        if not result.ok:
            return ""
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[-1] == iface:
                return ":".join(parts[:-1]).replace(r"\:", ":")
        return ""


class MacOSBackend(BaseBackend):
    name = "macOS"

    def list_adapters(self) -> list[AdapterInfo]:
        services = self._services()
        adapters: list[AdapterInfo] = []
        for service, device in services.items():
            info = self._networksetup_getinfo(service)
            mac = ""
            if device:
                mac_result = self.run(["ifconfig", device])
                mac = _parse_ifconfig_mac(mac_result.stdout) if mac_result.ok else ""
            adapters.append(
                AdapterInfo(
                    id=device or service,
                    name=service,
                    description=device,
                    mac=mac,
                    status="",
                    addresses=[
                        AddressInfo(
                            address=info["ip"],
                            prefix_length=netmask_to_prefix(info["subnet"]) if info["subnet"] else None,
                            family="ipv4",
                        )
                    ]
                    if info["ip"]
                    else [],
                    gateways=[info["router"]] if info["router"] else [],
                    dns_servers=self._dns_for_service(service),
                    dhcp_enabled=info["method"].lower() == "dhcp",
                    is_loopback=service.lower().startswith("loopback") or device.startswith("lo"),
                )
            )
        adapters.extend(self._loopback_aliases())
        return adapters

    def list_routes(self) -> list[RouteInfo]:
        result = self.run(["netstat", "-rn", "-f", "inet"])
        if not result.ok:
            raise BackendError(result.summary())
        routes: list[RouteInfo] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0] in {"Routing", "Internet:", "Destination"}:
                continue
            routes.append(
                RouteInfo(
                    destination="default" if parts[0] == "default" else parts[0],
                    gateway=parts[1],
                    interface=parts[3] if len(parts) > 3 else "",
                    metric=None,
                    family="ipv4",
                )
            )
        return routes

    def plan_adapter_update(
        self,
        adapter: AdapterInfo,
        address: AddressInfo | None,
        gateway: str,
        dns_servers: list[str],
        mac: str,
        dhcp_enabled: bool,
    ) -> OperationPlan:
        commands: list[list[str]] = []
        notes: list[str] = []
        if adapter.id.startswith("lo0:"):
            old_address = adapter.addresses[0].address if adapter.addresses else ""
            if address and address.address != old_address:
                commands.append(["ifconfig", "lo0", "-alias", old_address])
                commands.append(["ifconfig", "lo0", "alias", f"{address.address}/{address.prefix_length or 32}"])
            else:
                notes.append("No loopback alias address change was requested.")
            if gateway:
                notes.append("Gateways are not used for macOS loopback aliases.")
            if dns_servers:
                notes.append("DNS servers are not configured on macOS loopback aliases.")
            if mac.strip():
                notes.append("MAC addresses are not configured on macOS loopback aliases.")
            return OperationPlan("Update loopback alias", commands, notes)

        service = adapter.name
        device = adapter.description or adapter.id

        if dhcp_enabled:
            commands.append(["networksetup", "-setdhcp", service])
        elif address:
            netmask = prefix_to_netmask(address.prefix_length or 24)
            router = gateway or "none"
            commands.append(["networksetup", "-setmanual", service, address.address, netmask, router])
        else:
            notes.append("Skipped IP update because no IPv4 address was provided.")

        if dns_servers:
            commands.append(["networksetup", "-setdnsservers", service, *dns_servers])
        else:
            commands.append(["networksetup", "-setdnsservers", service, "Empty"])

        if mac.strip() and mac.strip() != adapter.mac:
            commands.append(["ifconfig", device, "ether", normalize_mac(mac, ":")])

        return OperationPlan("Update adapter", commands, notes)

    def plan_route_add(self, route: RouteInfo) -> OperationPlan:
        command = ["route", "-n", "add", route.destination]
        if route.gateway:
            command.append(route.gateway)
        if route.interface:
            command.extend(["-interface", route.interface])
        return OperationPlan("Add route", [command])

    def plan_route_delete(self, route: RouteInfo) -> OperationPlan:
        command = ["route", "-n", "delete", route.destination]
        if route.gateway:
            command.append(route.gateway)
        return OperationPlan("Delete route", [command])

    def plan_route_update(self, old_route: RouteInfo, new_route: RouteInfo) -> OperationPlan:
        return OperationPlan(
            "Update route",
            self.plan_route_delete(old_route).commands + self.plan_route_add(new_route).commands,
        )

    def plan_loopback_create(self, name: str) -> OperationPlan:
        alias = _loopback_alias_from_user_value(name)
        return OperationPlan(
            "Create loopback alias",
            [["ifconfig", "lo0", "alias", alias]],
            ["macOS adds loopback addresses to lo0 instead of creating separate loopback devices."],
        )

    def plan_loopback_delete(self, adapter: AdapterInfo) -> OperationPlan:
        address = adapter.addresses[0].address if adapter.addresses else adapter.name
        if address in {"127.0.0.1", "::1", "lo0"}:
            raise BackendError("The primary loopback address cannot be deleted.")
        return OperationPlan("Delete loopback alias", [["ifconfig", "lo0", "-alias", address]])

    def _services(self) -> dict[str, str]:
        result = self.run(["networksetup", "-listallhardwareports"])
        if not result.ok:
            raise BackendError(result.summary())
        services: dict[str, str] = {}
        current = ""
        for line in result.stdout.splitlines():
            if line.startswith("Hardware Port:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("Device:") and current:
                services[current] = line.split(":", 1)[1].strip()
                current = ""
        return services

    def _networksetup_getinfo(self, service: str) -> dict[str, str]:
        result = self.run(["networksetup", "-getinfo", service])
        if not result.ok:
            return {"method": "", "ip": "", "subnet": "", "router": ""}
        values = {"method": "", "ip": "", "subnet": "", "router": ""}
        for line in result.stdout.splitlines():
            key, _, value = line.partition(":")
            clean = value.strip()
            if key == "Manual Configuration":
                values["method"] = "manual"
            elif key == "DHCP Configuration":
                values["method"] = "dhcp"
            elif key == "IP address":
                values["ip"] = clean
            elif key == "Subnet mask":
                values["subnet"] = clean
            elif key == "Router":
                values["router"] = clean
        return values

    def _dns_for_service(self, service: str) -> list[str]:
        result = self.run(["networksetup", "-getdnsservers", service])
        if not result.ok or "not any DNS" in result.stdout:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _loopback_aliases(self) -> list[AdapterInfo]:
        result = self.run(["ifconfig", "lo0"])
        if not result.ok:
            return []
        return _loopback_alias_adapters(result.stdout)


class GenericPosixBackend(BaseBackend):
    name = "POSIX"

    def list_adapters(self) -> list[AdapterInfo]:
        if not shutil.which("ifconfig"):
            raise BackendError("ifconfig was not found on this POSIX system.")
        result = self.run(["ifconfig", "-a"])
        if not result.ok:
            raise BackendError(result.summary())
        adapters = _parse_ifconfig_adapters(result.stdout)
        adapters.extend(_loopback_alias_adapters(result.stdout))
        return adapters

    def list_routes(self) -> list[RouteInfo]:
        if not shutil.which("netstat"):
            return []
        result = self.run(["netstat", "-rn"])
        if not result.ok:
            raise BackendError(result.summary())
        return _parse_netstat_routes(result.stdout)

    def plan_adapter_update(
        self,
        adapter: AdapterInfo,
        address: AddressInfo | None,
        gateway: str,
        dns_servers: list[str],
        mac: str,
        dhcp_enabled: bool,
    ) -> OperationPlan:
        commands: list[list[str]] = []
        notes: list[str] = []
        iface = adapter.name
        if dhcp_enabled:
            notes.append("DHCP configuration is not portable across generic POSIX systems.")
        elif address:
            netmask = prefix_to_netmask(address.prefix_length or 24)
            if adapter.id.startswith("lo0:"):
                old_address = adapter.addresses[0].address if adapter.addresses else ""
                if old_address:
                    commands.append(["ifconfig", "lo0", "-alias", old_address])
                commands.append(["ifconfig", "lo0", "alias", f"{address.address}/{address.prefix_length or 32}"])
            else:
                commands.append(["ifconfig", iface, "inet", address.address, "netmask", netmask, "up"])
            if gateway:
                commands.append(["route", "add", "default", gateway])
        else:
            notes.append("Skipped IP update because no IPv4 address was provided.")

        if mac.strip() and mac.strip() != adapter.mac:
            notes.append("MAC address changes are not portable across generic POSIX systems.")
        if dns_servers:
            notes.append("DNS server updates are not portable across generic POSIX systems.")
        return OperationPlan("Update adapter", commands, notes)

    def plan_route_add(self, route: RouteInfo) -> OperationPlan:
        command = ["route", "add", route.destination]
        if route.gateway:
            command.append(route.gateway)
        return OperationPlan("Add route", [command])

    def plan_route_delete(self, route: RouteInfo) -> OperationPlan:
        command = ["route", "delete", route.destination]
        if route.gateway:
            command.append(route.gateway)
        return OperationPlan("Delete route", [command])

    def plan_route_update(self, old_route: RouteInfo, new_route: RouteInfo) -> OperationPlan:
        return OperationPlan(
            "Update route",
            self.plan_route_delete(old_route).commands + self.plan_route_add(new_route).commands,
        )

    def plan_loopback_create(self, name: str) -> OperationPlan:
        alias = _loopback_alias_from_user_value(name)
        return OperationPlan(
            "Create loopback alias",
            [["ifconfig", "lo0", "alias", alias]],
            ["Generic POSIX systems usually add loopback aliases rather than creating devices."],
        )

    def plan_loopback_delete(self, adapter: AdapterInfo) -> OperationPlan:
        address = adapter.addresses[0].address if adapter.addresses else adapter.name
        if address in {"127.0.0.1", "::1", "lo0"}:
            raise BackendError("The primary loopback address cannot be deleted.")
        return OperationPlan("Delete loopback alias", [["ifconfig", "lo0", "-alias", address]])


class UnsupportedBackend(BaseBackend):
    name = platform.system() or "Unsupported"

    def list_adapters(self) -> list[AdapterInfo]:
        return []

    def list_routes(self) -> list[RouteInfo]:
        return []

    def plan_adapter_update(
        self,
        adapter: AdapterInfo,
        address: AddressInfo | None,
        gateway: str,
        dns_servers: list[str],
        mac: str,
        dhcp_enabled: bool,
    ) -> OperationPlan:
        raise BackendError(f"{self.name} is not supported yet.")

    def plan_route_add(self, route: RouteInfo) -> OperationPlan:
        raise BackendError(f"{self.name} is not supported yet.")

    def plan_route_delete(self, route: RouteInfo) -> OperationPlan:
        raise BackendError(f"{self.name} is not supported yet.")

    def plan_route_update(self, old_route: RouteInfo, new_route: RouteInfo) -> OperationPlan:
        raise BackendError(f"{self.name} is not supported yet.")

    def plan_loopback_create(self, name: str) -> OperationPlan:
        raise BackendError(f"{self.name} is not supported yet.")

    def plan_loopback_delete(self, adapter: AdapterInfo) -> OperationPlan:
        raise BackendError(f"{self.name} is not supported yet.")


def get_backend() -> BaseBackend:
    system = platform.system().lower()
    if system == "windows":
        return WindowsBackend()
    if system == "linux":
        return LinuxBackend()
    if system == "darwin":
        return MacOSBackend()
    if os.name == "posix":
        return GenericPosixBackend()
    return UnsupportedBackend()


def _powershell(script: str) -> list[str]:
    executable = "powershell"
    if shutil.which("pwsh"):
        executable = "pwsh"
    return [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


def decode_command_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")

    for encoding in _candidate_output_encodings():
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _candidate_output_encodings() -> list[str]:
    candidates = [
        "utf-8",
        locale.getpreferredencoding(False),
        getattr(locale, "getencoding", lambda: "")(),
    ]
    candidates.extend(_windows_code_page_encodings())
    candidates.extend(["gbk", "cp936", "mbcs", "latin-1"])

    seen: set[str] = set()
    unique: list[str] = []
    for encoding in candidates:
        normalized = (encoding or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(encoding)
    return unique


def _windows_code_page_encodings() -> list[str]:
    if platform.system().lower() != "windows":
        return []
    encodings: list[str] = []
    for args in (["chcp"], ["cmd", "/c", "chcp"]):
        try:
            completed = subprocess.run(args, capture_output=True, check=False, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        raw = completed.stdout + completed.stderr
        text = raw.decode("ascii", errors="ignore")
        match = re.search(r"(\d+)", text)
        if match:
            encodings.append(f"cp{match.group(1)}")
            break
    return encodings


def _windows_dns_commands(name: str, dns_servers: list[str]) -> list[list[str]]:
    if not dns_servers:
        return [["netsh", "interface", "ip", "set", "dns", f"name={name}", "source=dhcp"]]
    commands = [
        [
            "netsh",
            "interface",
            "ip",
            "set",
            "dns",
            f"name={name}",
            "static",
            dns_servers[0],
            "primary",
        ]
    ]
    for index, server in enumerate(dns_servers[1:], start=2):
        commands.append(
            [
                "netsh",
                "interface",
                "ip",
                "add",
                "dns",
                f"name={name}",
                server,
                f"index={index}",
            ]
        )
    return commands


def _windows_route_destination(destination: str) -> tuple[str, str]:
    if destination in {"default", "0.0.0.0/0"}:
        return "0.0.0.0", "0.0.0.0"
    if "/" not in destination:
        return destination, "255.255.255.255"
    address, prefix = destination.split("/", 1)
    return address, prefix_to_netmask(int(prefix))


def _as_list(data: object) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _optional_int(value: object) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_commands(commands: Iterable[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for command in commands:
        key = tuple(command)
        if key not in seen:
            seen.add(key)
            unique.append(command)
    return unique


def _route_key(route: RouteInfo) -> tuple[str, str, str, int | None]:
    return (
        route.destination.strip().lower(),
        route.gateway.strip().lower(),
        route.interface.strip().lower(),
        route.metric,
    )


def _parse_resolvectl_dns(output: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    pattern = re.compile(r"^Link\s+\d+\s+\(([^)]+)\):\s*(.*)$")
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).split()
    return values


def _parse_ifconfig_mac(output: str) -> str:
    match = re.search(r"\bether\s+([0-9a-fA-F:]{17})", output)
    return match.group(1) if match else ""


def _loopback_alias_from_user_value(value: str) -> str:
    text = value.strip()
    if not text:
        raise BackendError("A loopback alias address is required.")
    if text.startswith("lo0:"):
        _, _, text = text.partition(":")
    if "/" not in text:
        text = f"{text}/32"
    return text


def _loopback_alias_adapters(output: str) -> list[AdapterInfo]:
    adapters: list[AdapterInfo] = []
    for line in output.splitlines():
        match = re.search(r"\binet\s+([0-9.]+)(?:\s+netmask\s+([0-9a-fx.]+))?", line)
        if not match:
            continue
        address = match.group(1)
        if address == "127.0.0.1":
            continue
        prefix = _ifconfig_netmask_to_prefix(match.group(2) or "") or 32
        adapters.append(
            AdapterInfo(
                id=f"lo0:{address}",
                name=f"lo0:{address}",
                description="lo0 alias",
                status="up",
                addresses=[AddressInfo(address, prefix, "ipv4")],
                is_loopback=True,
            )
        )
    return adapters


def _parse_ifconfig_adapters(output: str) -> list[AdapterInfo]:
    adapters: list[AdapterInfo] = []
    current: AdapterInfo | None = None
    for line in output.splitlines():
        header = re.match(r"^([^\s:]+):\s", line)
        if header:
            if current:
                adapters.append(current)
            name = header.group(1)
            current = AdapterInfo(
                id=name,
                name=name,
                description="",
                status="up" if "UP" in line else "down",
                is_loopback=name.startswith("lo"),
            )
            continue
        if current is None:
            continue
        inet = re.search(r"\binet\s+([0-9.]+)(?:\s+netmask\s+([0-9a-fx.]+))?", line)
        if inet:
            prefix = _ifconfig_netmask_to_prefix(inet.group(2) or "")
            current.addresses.append(AddressInfo(inet.group(1), prefix, "ipv4"))
        mac = re.search(r"\b(?:ether|lladdr)\s+([0-9a-fA-F:]{17})", line)
        if mac:
            current.mac = mac.group(1)
    if current:
        adapters.append(current)
    return adapters


def _parse_netstat_routes(output: str) -> list[RouteInfo]:
    routes: list[RouteInfo] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in {"Routing", "Internet:", "Internet6:", "Destination"}:
            continue
        destination = "default" if parts[0] in {"default", "0.0.0.0"} else parts[0]
        interface = parts[-1] if len(parts) >= 4 else ""
        routes.append(
            RouteInfo(
                destination=destination,
                gateway=parts[1],
                interface=interface,
                family="ipv4",
            )
        )
    return routes


def _ifconfig_netmask_to_prefix(value: str) -> int | None:
    if not value:
        return None
    if value.startswith("0x"):
        try:
            number = int(value, 16)
            return bin(number).count("1")
        except ValueError:
            return None
    try:
        return netmask_to_prefix(value)
    except ValueError:
        return None


def _ps_escape(value: str) -> str:
    return value.replace("`", "``").replace('"', '`"')
