from __future__ import annotations

import json
import sys
import time
import subprocess
import pytest

from py_nic_manager.backends import (
    LinuxBackend,
    MacOSBackend,
    WindowsBackend,
    _macos_adapter_forwarding_state,
    decode_command_output,
)
from py_nic_manager.api import NetworkManager, PrivilegeError, sort_routes as api_sort_routes
from py_nic_manager.app import NetworkManagerApp, _suggest_loopback_value, format_elapsed_time, route_sort_key
from py_nic_manager.io import import_snapshot
from py_nic_manager.__main__ import _gui_preference, _qt_runtime_available
from py_nic_manager.models import AdapterInfo, AddressInfo, NetworkSnapshot, OperationPlan, RouteInfo
from py_nic_manager.tk_fonts import BUNDLED_FONT_FAMILY, bundled_font_paths
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


def test_adapter_forwarding_round_trip() -> None:
    adapter = AdapterInfo(id="eth0", name="eth0", forwarding_enabled=False)

    loaded = AdapterInfo.from_dict(adapter.to_dict())

    assert loaded.forwarding_enabled is False


def test_loopback_suggestion_skips_existing_adapter_names() -> None:
    adapters = [
        AdapterInfo(id="0", name="py-loopback0"),
        AdapterInfo(id="1", name="PY-LOOPBACK1"),
        AdapterInfo(id="2", name="Ethernet"),
    ]

    assert _suggest_loopback_value("Windows", adapters) == "py-loopback2"
    assert _suggest_loopback_value("Linux", adapters) == "py-loopback2"


def test_loopback_suggestion_skips_existing_macos_aliases() -> None:
    adapters = [
        AdapterInfo(id="lo0:127.0.0.2", name="lo0:127.0.0.2", addresses=[AddressInfo("127.0.0.2", 32)]),
        AdapterInfo(id="lo0:127.0.0.3", name="lo0:127.0.0.3", addresses=[AddressInfo("127.0.0.3", 32)]),
    ]

    assert _suggest_loopback_value("macOS", adapters) == "127.0.0.4/32"


def test_network_state_loader_fetches_adapters_and_routes_concurrently() -> None:
    class SlowBackend:
        def list_adapters(self):
            time.sleep(0.2)
            return ["adapter"]

        def list_routes(self):
            time.sleep(0.2)
            return ["route"]

    class Loader:
        backend = SlowBackend()
        _load_network_state = NetworkManagerApp._load_network_state

    started_at = time.perf_counter()
    adapters, routes = Loader()._load_network_state()
    elapsed = time.perf_counter() - started_at

    assert adapters == ["adapter"]
    assert routes == ["route"]
    assert elapsed < 0.35


def test_format_elapsed_time_uses_seconds_minutes_and_hours() -> None:
    assert format_elapsed_time(0) == "0s"
    assert format_elapsed_time(59.9) == "59s"
    assert format_elapsed_time(60) == "1m 00s"
    assert format_elapsed_time(65) == "1m 05s"
    assert format_elapsed_time(3661) == "1h 01m 01s"


def test_qt_format_elapsed_time_matches_tkinter_format() -> None:
    qt_app = pytest.importorskip("py_nic_manager.qt_app")

    assert qt_app.format_elapsed_time(3661) == "1h 01m 01s"


def test_qt_window_can_be_constructed_without_refresh(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qt_app = pytest.importorskip("py_nic_manager.qt_app")

    app = qt_app.QApplication.instance() or qt_app.QApplication([])
    qt_app.apply_auto_theme(app)
    window = qt_app.NetworkManagerQtWindow(auto_refresh=False)

    assert window.windowTitle() == "Py NIC Manager"
    assert window.tabs.count() == 4
    window.close()


def test_gui_preference_env_values() -> None:
    assert _gui_preference({}) == "auto"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "qt"}) == "qt"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "pyqt6"}) == "qt"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "tkinter"}) == "tk"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "surprise"}) == "auto"


def test_bundled_tk_font_assets_are_present() -> None:
    paths = bundled_font_paths()
    names = {path.name for path in paths}

    assert BUNDLED_FONT_FAMILY == "JetBrains Mono"
    assert names == {"JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"}
    assert all(path.exists() and path.stat().st_size > 100_000 for path in paths)
    assert (paths[0].parent / "JetBrainsMono-OFL.txt").exists()


def test_qt_runtime_probe_handles_crashes(monkeypatch) -> None:
    class Completed:
        returncode = 134

    def fake_run(*_args, **_kwargs):
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _qt_runtime_available() is False


def test_qt_runtime_probe_accepts_zero_returncode(monkeypatch) -> None:
    class Completed:
        returncode = 0

    def fake_run(*_args, **_kwargs):
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _qt_runtime_available() is True


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


def test_route_destination_sort_key_uses_ipv4_integer_then_prefix() -> None:
    routes = [
        RouteInfo("10.0.0.0/24"),
        RouteInfo("0.0.0.0/0"),
        RouteInfo("192.168.1.0/24"),
        RouteInfo("10.0.0.0/8"),
        RouteInfo("default"),
    ]

    ordered = sorted(routes, key=lambda route: route_sort_key(route, "destination"))

    assert [route.destination for route in ordered] == [
        "0.0.0.0/0",
        "default",
        "10.0.0.0/8",
        "10.0.0.0/24",
        "192.168.1.0/24",
    ]


def test_route_numeric_and_text_sort_keys() -> None:
    routes = [
        RouteInfo("198.51.100.0/24", "192.0.2.10", "wifi", metric=100, effective_metric=125),
        RouteInfo("198.51.100.0/24", "192.0.2.2", "ethernet", metric=5, effective_metric=30),
        RouteInfo("198.51.100.0/24", "", "loopback", metric=None, effective_metric=None),
    ]

    by_gateway = sorted(routes, key=lambda route: route_sort_key(route, "gateway"))
    by_metric = sorted(routes, key=lambda route: route_sort_key(route, "route_metric"))
    by_interface = sorted(routes, key=lambda route: route_sort_key(route, "interface"))

    assert [route.gateway for route in by_gateway] == ["192.0.2.2", "192.0.2.10", ""]
    assert [route.metric for route in by_metric] == [5, 100, None]
    assert [route.interface for route in by_interface] == ["ethernet", "loopback", "wifi"]


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


def test_windows_forwarding_plan_uses_netipinterface() -> None:
    backend = WindowsBackend(dry_run=True)
    adapter = AdapterInfo(id="id", name="Ethernet")

    plan = backend.plan_adapter_forwarding_update(adapter, False)
    rendered = " ".join(plan.commands[0])

    assert "Set-NetIPInterface" in rendered
    assert "-Forwarding Disabled" in rendered


def test_linux_route_plan_uses_ipv4_ip_route() -> None:
    backend = LinuxBackend(dry_run=True)
    route = RouteInfo("198.51.100.0/24", "192.0.2.1", "eth0", 5)

    plan = backend.plan_route_add(route)

    assert plan.commands == [
        ["ip", "-4", "route", "replace", "198.51.100.0/24", "via", "192.0.2.1", "dev", "eth0", "metric", "5"]
    ]


def test_linux_forwarding_plan_uses_sysctl() -> None:
    backend = LinuxBackend(dry_run=True)
    adapter = AdapterInfo(id="eth0", name="eth0")

    plan = backend.plan_adapter_forwarding_update(adapter, False)

    assert plan.commands == [["sysctl", "-w", "net.ipv4.conf.eth0.forwarding=0"]]


def test_macos_loopback_create_uses_alias_address() -> None:
    backend = MacOSBackend(dry_run=True)

    plan = backend.plan_loopback_create("127.0.0.2")

    assert plan.commands == [["ifconfig", "lo0", "alias", "127.0.0.2/32"]]


def test_macos_forwarding_plan_uses_packaged_pf_helper() -> None:
    backend = MacOSBackend(dry_run=True)
    adapter = AdapterInfo(id="en0", name="Wi-Fi", description="en0")

    plan = backend.plan_adapter_forwarding_update(adapter, False)

    assert plan.commands == [
        [sys.executable, "-m", "py_nic_manager.macos_forwarding", "set", "en0", "disabled"]
    ]


def test_macos_forwarding_state_combines_global_and_disabled_interfaces() -> None:
    assert _macos_adapter_forwarding_state("en0", True, set()) is True
    assert _macos_adapter_forwarding_state("en0", True, {"en0"}) is False
    assert _macos_adapter_forwarding_state("en0", False, set()) is False
    assert _macos_adapter_forwarding_state("lo0", True, set()) is False
    assert _macos_adapter_forwarding_state("en0", None, set()) is None


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


def test_python_api_covers_snapshot_and_mutating_plans(tmp_path) -> None:
    manager = NetworkManager(_FakeWindowsBackend(dry_run=True), admin_checker=lambda: False)

    snapshot = manager.get_snapshot(concurrent=False)
    path = manager.export_snapshot(tmp_path / "snapshot.json", snapshot)
    loaded = manager.import_snapshot(path)
    adapter_plan = manager.plan_update_adapter(
        "Ethernet",
        address="192.0.2.50/24",
        gateway="192.0.2.1",
        dns_servers="1.1.1.1, 8.8.8.8",
        mac="00:11:22:33:44:66",
    )
    forwarding_plan = manager.plan_set_adapter_forwarding("Ethernet", False)
    create_loopback_plan = manager.plan_create_loopback()
    delete_loopback_plan = manager.plan_delete_loopback("py-loopback0")
    update_loopback_plan = manager.plan_update_loopback("py-loopback0", address="192.0.2.60/24")
    add_route_plan = manager.plan_add_route(
        "203.0.113.0/24",
        gateway="192.0.2.1",
        interface="Ethernet",
        metric=20,
    )
    update_route_plan = manager.plan_update_route(
        "198.51.100.0/24",
        "203.0.113.0/24",
        old_gateway="192.0.2.1",
        old_interface="Ethernet",
        gateway="192.0.2.1",
        interface="Ethernet",
        metric=20,
    )
    delete_route_plan = manager.plan_delete_route(
        "198.51.100.0/24",
        gateway="192.0.2.1",
        interface="Ethernet",
    )

    assert loaded.platform == "Windows"
    assert snapshot.adapters[0].name == "Ethernet"
    assert any("netsh" in command[0].lower() for command in adapter_plan.commands)
    assert "Set-NetIPInterface" in " ".join(forwarding_plan.commands[0])
    assert "py-loopback1" in create_loopback_plan.commands[0]
    assert delete_loopback_plan.title == "Delete loopback adapter"
    assert update_loopback_plan.commands
    assert add_route_plan.title == "Add route"
    assert update_route_plan.title == "Update route"
    assert delete_route_plan.title == "Delete route"

    results = manager.run_plan(add_route_plan)
    assert all(result.ok for result in results)


def test_python_api_requires_admin_for_real_mutations() -> None:
    manager = NetworkManager(_FakeWindowsBackend(dry_run=False), admin_checker=lambda: False)

    try:
        manager.run_plan(OperationPlan("Danger", [["would-run"]]))
    except PrivilegeError as exc:
        assert "Administrator/root" in str(exc)
    else:
        raise AssertionError("PrivilegeError was not raised.")


def test_python_api_route_sorting_uses_network_and_metric_types() -> None:
    routes = [
        RouteInfo("10.0.0.0/24", metric=100),
        RouteInfo("0.0.0.0/0", metric=10),
        RouteInfo("default", metric=50),
        RouteInfo("192.168.1.0/24", metric=None),
        RouteInfo("10.0.0.0/8", metric=5),
    ]

    by_destination = api_sort_routes(routes, sort_by="destination")
    by_metric = api_sort_routes(routes, sort_by="route_metric")

    assert [route.destination for route in by_destination] == [
        "0.0.0.0/0",
        "default",
        "10.0.0.0/8",
        "10.0.0.0/24",
        "192.168.1.0/24",
    ]
    assert [route.metric for route in by_metric] == [5, 10, 50, 100, None]


class _FakeWindowsBackend(WindowsBackend):
    def list_adapters(self):
        return [
            AdapterInfo(
                id="ethernet-id",
                name="Ethernet",
                description="Ethernet Adapter",
                mac="00-11-22-33-44-55",
                status="Up",
                addresses=[AddressInfo("192.0.2.10", 24)],
                gateways=["192.0.2.1"],
                dns_servers=["1.1.1.1"],
                dhcp_enabled=False,
                forwarding_enabled=True,
            ),
            AdapterInfo(
                id="loopback-id",
                name="py-loopback0",
                description="Microsoft KM-TEST Loopback Adapter",
                status="Up",
                addresses=[AddressInfo("192.0.2.20", 24)],
                is_loopback=True,
                forwarding_enabled=False,
            ),
        ]

    def list_routes(self):
        return [
            RouteInfo("198.51.100.0/24", "192.0.2.1", "Ethernet", 10),
            RouteInfo("127.0.0.0/8", "", "Loopback Pseudo-Interface 1", None, protocol="Local"),
        ]
