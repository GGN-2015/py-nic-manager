from __future__ import annotations

import json
import sys

from py_nic_manager.backends import LinuxBackend, MacOSBackend, WindowsBackend, decode_command_output
from py_nic_manager.io import import_snapshot
from py_nic_manager.models import AdapterInfo, AddressInfo, NetworkSnapshot, RouteInfo
from py_nic_manager.validation import normalize_mac, prefix_to_netmask, validate_network


def test_validation_helpers() -> None:
    assert normalize_mac("00:11:22:aa:bb:cc") == "00-11-22-AA-BB-CC"
    assert normalize_mac("001122aabbcc", ":") == "00:11:22:AA:BB:CC"
    assert prefix_to_netmask(24) == "255.255.255.0"
    assert validate_network("default") == "default"
    assert validate_network("192.0.2.10/24") == "192.0.2.10/24"


def test_command_output_decodes_utf8_and_gbk() -> None:
    text = "\u672c\u5730\u8fde\u63a5 \u5df2\u542f\u7528"

    assert decode_command_output(text.encode("utf-8")) == text
    assert decode_command_output(text.encode("gbk")) == text
    assert decode_command_output(("\ufeff" + text).encode("utf-8")) == text


def test_snapshot_round_trip(tmp_path) -> None:
    path = tmp_path / "snapshot.json"
    snapshot = NetworkSnapshot(
        platform="TestOS",
        adapters=[
            AdapterInfo(
                id="eth0",
                name="eth0",
                mac="00:11:22:33:44:55",
                addresses=[AddressInfo("192.0.2.10", 24)],
                gateways=["192.0.2.1"],
                dns_servers=["1.1.1.1"],
            )
        ],
        routes=[RouteInfo("0.0.0.0/0", "192.0.2.1", "eth0", 10)],
    )
    path.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")

    loaded = import_snapshot(path)

    assert loaded.platform == "TestOS"
    assert loaded.adapters[0].name == "eth0"
    assert loaded.routes[0].gateway == "192.0.2.1"
    assert loaded.routes[0].interface_metric is None
    assert loaded.routes[0].effective_metric is None


def test_route_metrics_round_trip() -> None:
    route = RouteInfo(
        "0.0.0.0/0",
        "192.0.2.1",
        "Ethernet",
        metric=10,
        interface_metric=25,
        effective_metric=35,
    )

    loaded = RouteInfo.from_dict(route.to_dict())

    assert loaded.metric == 10
    assert loaded.interface_metric == 25
    assert loaded.effective_metric == 35


def test_windows_adapter_plan_contains_netsh_and_mac_property() -> None:
    backend = WindowsBackend(dry_run=True)
    adapter = AdapterInfo(id="id", name="Ethernet", mac="00-11-22-33-44-55")

    plan = backend.plan_adapter_update(
        adapter,
        AddressInfo("192.0.2.10", 24),
        "192.0.2.1",
        ["1.1.1.1", "8.8.8.8"],
        "00:11:22:33:44:66",
        False,
    )

    assert [
        "netsh",
        "interface",
        "ip",
        "set",
        "address",
        "name=Ethernet",
        "static",
        "192.0.2.10",
        "255.255.255.0",
        "192.0.2.1",
        "1",
    ] in plan.commands
    assert any("Set-NetAdapterAdvancedProperty" in " ".join(command) for command in plan.commands)


def test_windows_loopback_plan_uses_packaged_setupapi_helper() -> None:
    backend = WindowsBackend(dry_run=True)

    plan = backend.plan_loopback_create("py-loopback0")

    rendered = " ".join(plan.commands[0])
    assert plan.commands[0][:4] == [sys.executable, "-m", "py_nic_manager.windows_loopback", "create"]
    assert "--name" in plan.commands[0]
    assert "py-loopback0" in plan.commands[0]
    assert "devcon" not in rendered.lower()


def test_linux_route_plan_uses_ipv4_ip_route() -> None:
    backend = LinuxBackend(dry_run=True)
    route = RouteInfo("198.51.100.0/24", "192.0.2.1", "eth0", 5)

    plan = backend.plan_route_add(route)

    assert plan.commands == [
        ["ip", "-4", "route", "replace", "198.51.100.0/24", "via", "192.0.2.1", "dev", "eth0", "metric", "5"]
    ]


def test_macos_loopback_create_uses_alias_address() -> None:
    backend = MacOSBackend(dry_run=True)

    plan = backend.plan_loopback_create("127.0.0.2")

    assert plan.commands == [["ifconfig", "lo0", "alias", "127.0.0.2/32"]]


def test_snapshot_apply_deletes_missing_route_and_adds_new_route() -> None:
    backend = _FakeWindowsBackend(dry_run=True)
    snapshot = NetworkSnapshot(
        platform="Windows",
        adapters=[],
        routes=[RouteInfo("203.0.113.0/24", "192.0.2.1", "Ethernet", 20)],
    )

    plan = backend.plan_snapshot_apply(snapshot)
    rendered = "\n".join(" ".join(command) for command in plan.commands)

    assert 'Remove-NetRoute' in rendered
    assert 'DestinationPrefix "198.51.100.0/24"' in rendered
    assert 'New-NetRoute' in rendered
    assert 'DestinationPrefix "203.0.113.0/24"' in rendered


class _FakeWindowsBackend(WindowsBackend):
    def list_adapters(self):
        return []

    def list_routes(self):
        return [
            RouteInfo("198.51.100.0/24", "192.0.2.1", "Ethernet", 10),
            RouteInfo("127.0.0.0/8", "", "Loopback Pseudo-Interface 1", None, protocol="Local"),
        ]
