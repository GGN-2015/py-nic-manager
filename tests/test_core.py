from __future__ import annotations

import json
import sys
import time
import subprocess
from pathlib import Path
import pytest

from py_nic_manager.backends import (
    BackendError,
    LinuxBackend,
    MacOSBackend,
    WindowsBackend,
    _parse_iptables_nat_rules,
    _macos_adapter_forwarding_state,
    decode_command_output,
)
from py_nic_manager.api import NetworkManager, PrivilegeError, sort_routes as api_sort_routes
from py_nic_manager.app import NetworkManagerApp, _suggest_loopback_value, format_elapsed_time, route_sort_key
from py_nic_manager.io import import_snapshot
from py_nic_manager.__main__ import _gui_preference, _qt_runtime_available, _qt_supported_on_current_platform
from py_nic_manager.models import (
    AdapterInfo,
    AddressInfo,
    CommandResult,
    NatRule,
    NetworkSnapshot,
    OperationPlan,
    RouteInfo,
    VirtualAdapterInfo,
)
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


def test_command_result_error_message_hides_full_command() -> None:
    result = CommandResult(
        command=["powershell", "-Command", "function VeryLongScript { }"],
        returncode=1,
        stderr="A concise error.",
    )

    assert result.error_message() == "A concise error."
    assert "VeryLongScript" not in result.error_message()
    assert "VeryLongScript" in result.summary()


def test_command_result_error_message_strips_powershell_location_noise() -> None:
    result = CommandResult(
        command=["powershell", "-Command", "throw ..."],
        returncode=1,
        stderr=(
            "Failed through outbound interface 'WLAN'. inner reason\n"
            "At line:7 char:3\n"
            "+   throw \"Failed through outbound interface '$outboundInterface'\"\n"
            "+   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"
            "    + CategoryInfo          : OperationStopped: (...):String) [], RuntimeException\n"
            "    + FullyQualifiedErrorId : Failed through outbound interface 'WLAN'. inner reason\n"
        ),
    )

    assert result.error_message() == "Failed through outbound interface 'WLAN'. inner reason"
    assert "$outboundInterface" not in result.error_message()


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
        nat_rules=[NatRule("nat0", "192.168.0.0/24", "eth0")],
        virtual_adapters=[VirtualAdapterInfo("py-virtual0", "veth", address="192.168.56.1/24")],
        global_forwarding_enabled=True,
    )
    path.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")

    loaded = import_snapshot(path)

    assert loaded.platform == "TestOS"
    assert loaded.adapters[0].name == "eth0"
    assert loaded.routes[0].gateway == "192.0.2.1"
    assert loaded.routes[0].interface_metric is None
    assert loaded.routes[0].effective_metric is None
    assert loaded.nat_rules[0].source_cidr == "192.168.0.0/24"
    assert loaded.virtual_adapters[0].name == "py-virtual0"
    assert loaded.global_forwarding_enabled is True


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

        def list_nat_rules(self):
            time.sleep(0.2)
            return ["nat"]

        def list_virtual_adapters(self):
            time.sleep(0.2)
            return ["virtual"]

        def get_global_forwarding_enabled(self):
            time.sleep(0.2)
            return True

    class Loader:
        backend = SlowBackend()
        _load_network_state = NetworkManagerApp._load_network_state

    started_at = time.perf_counter()
    adapters, routes, nat_rules, virtual_adapters, global_forwarding = Loader()._load_network_state()
    elapsed = time.perf_counter() - started_at

    assert adapters == ["adapter"]
    assert routes == ["route"]
    assert nat_rules == ["nat"]
    assert virtual_adapters == ["virtual"]
    assert global_forwarding is True
    assert elapsed < 0.35


def test_network_state_loader_tolerates_optional_state_failures() -> None:
    class PartiallyFailingBackend:
        def list_adapters(self):
            return ["adapter"]

        def list_routes(self):
            return ["route"]

        def list_nat_rules(self):
            raise RuntimeError("NAT is unavailable.")

        def list_virtual_adapters(self):
            raise RuntimeError("Virtual adapter state is unavailable.")

        def get_global_forwarding_enabled(self):
            raise RuntimeError("Forwarding state is unavailable.")

    class Loader:
        backend = PartiallyFailingBackend()
        _load_network_state = NetworkManagerApp._load_network_state

    loader = Loader()
    adapters, routes, nat_rules, virtual_adapters, global_forwarding = loader._load_network_state()

    assert adapters == ["adapter"]
    assert routes == ["route"]
    assert nat_rules == []
    assert virtual_adapters == []
    assert global_forwarding is None
    assert loader._optional_load_errors == [
        "NAT rules unavailable: NAT is unavailable.",
        "Virtual adapters unavailable: Virtual adapter state is unavailable.",
        "Global forwarding state unavailable: Forwarding state is unavailable.",
    ]


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
    assert window.tabs.count() == 5
    window.close()


def test_gui_preference_env_values() -> None:
    assert _gui_preference({}) == "auto"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "qt"}) == "qt"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "pyqt6"}) == "qt"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "tkinter"}) == "tk"
    assert _gui_preference({"PY_NIC_MANAGER_GUI": "surprise"}) == "auto"


def test_qt_auto_mode_is_windows_only(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    assert _qt_supported_on_current_platform() is True

    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert _qt_supported_on_current_platform() is False

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert _qt_supported_on_current_platform() is False


def test_bundled_tk_font_assets_are_present() -> None:
    paths = bundled_font_paths()
    names = {path.name for path in paths}

    assert BUNDLED_FONT_FAMILY == "JetBrains Mono"
    assert names == {"JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"}
    assert all(path.exists() and path.stat().st_size > 100_000 for path in paths)
    assert (paths[0].parent / "JetBrainsMono-OFL.txt").exists()


def test_bundled_wintun_assets_are_present() -> None:
    root = Path(__file__).resolve().parents[1] / "py_nic_manager" / "assets" / "wintun"

    assert (root / "LICENSE.txt").exists()
    assert (root / "README.md").exists()
    for arch in ("amd64", "x86", "arm", "arm64"):
        dll = root / arch / "wintun.dll"
        assert dll.exists()
        assert dll.stat().st_size > 100_000


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


def test_windows_global_forwarding_plan_updates_ip_enable_router() -> None:
    backend = WindowsBackend(dry_run=True)

    plan = backend.plan_global_forwarding_update(True)
    rendered = " ".join(plan.commands[0])

    assert plan.restart_required is True
    assert "IPEnableRouter" in rendered
    assert "-Value 1" in rendered


def test_windows_global_forwarding_read_avoids_noisy_missing_value_cmdlet() -> None:
    class CapturingWindowsBackend(WindowsBackend):
        command: list[str] | None = None

        def run_json(self, command: list[str]) -> object:
            self.command = command
            return {"enabled": False}

    backend = CapturingWindowsBackend(dry_run=True)

    enabled = backend.get_global_forwarding_enabled()

    assert enabled is False
    assert backend.command is not None
    rendered = " ".join(backend.command)
    assert "Get-ItemProperty" in rendered
    assert "Get-ItemPropertyValue" not in rendered


def test_windows_nat_read_uses_managed_rras_ics_state_without_winnat() -> None:
    class CapturingWindowsBackend(WindowsBackend):
        command: list[str] | None = None

        def run_json(self, command: list[str]) -> object:
            self.command = command
            return {
                "name": "nat0",
                "source_cidr": "192.168.1.0/30",
                "outbound_interface": "WLAN",
                "enabled": True,
                "persistent": True,
                "managed": True,
                "family": "ipv4",
            }

    backend = CapturingWindowsBackend(dry_run=True)

    rules = backend.list_nat_rules()

    assert len(rules) == 1
    assert rules[0].name == "nat0"
    assert rules[0].outbound_interface == "WLAN"
    assert backend.command is not None
    rendered = " ".join(backend.command)
    assert "HNetCfg.HNetShare" in rendered
    assert "ProgramData" in rendered


def test_windows_nat_read_is_optional_when_rras_ics_state_is_unavailable() -> None:
    class FailingWindowsBackend(WindowsBackend):
        command: list[str] | None = None

        def run_json(self, command: list[str]) -> object:
            self.command = command
            raise BackendError("NAT state is unavailable.")

    backend = FailingWindowsBackend(dry_run=True)

    rules = backend.list_nat_rules()

    assert rules == []
    assert backend.command is not None
    rendered = " ".join(backend.command)
    assert "HNetCfg.HNetShare" in rendered


def test_linux_route_plan_uses_ipv4_ip_route() -> None:
    backend = LinuxBackend(dry_run=True)
    route = RouteInfo("198.51.100.0/24", "192.0.2.1", "eth0", 5)

    plan = backend.plan_route_add(route)

    assert plan.commands == [
        ["ip", "-4", "route", "replace", "198.51.100.0/24", "via", "192.0.2.1", "dev", "eth0", "metric", "5"]
    ]


def test_linux_route_plan_marks_link_local_gateways_onlink() -> None:
    backend = LinuxBackend(dry_run=True)
    route = RouteInfo("192.168.0.0/16", "169.254.197.202", "enp0s3", 0)

    plan = backend.plan_route_add(route)

    assert plan.commands == [
        [
            "ip",
            "-4",
            "route",
            "replace",
            "192.168.0.0/16",
            "via",
            "169.254.197.202",
            "dev",
            "enp0s3",
            "onlink",
            "metric",
            "0",
        ]
    ]


def test_linux_forwarding_plan_uses_sysctl() -> None:
    backend = LinuxBackend(dry_run=True)
    adapter = AdapterInfo(id="eth0", name="eth0")

    plan = backend.plan_adapter_forwarding_update(adapter, False)

    assert plan.commands == [["sysctl", "-w", "net.ipv4.conf.eth0.forwarding=0"]]


def test_linux_global_forwarding_plan_uses_ip_forward_sysctl() -> None:
    backend = LinuxBackend(dry_run=True)

    plan = backend.plan_global_forwarding_update(False)

    assert plan.restart_required is True
    assert plan.commands == [["sysctl", "-w", "net.ipv4.ip_forward=0"]]


def test_linux_nat_plan_uses_persistent_helper_without_restart() -> None:
    backend = LinuxBackend(dry_run=True)

    plan = backend.plan_nat_create(NatRule("nat0", "192.168.0.0/24", "eth0"))

    assert plan.restart_required is False
    assert plan.commands[0][:4] == [sys.executable, "-m", "py_nic_manager.nat_persistence", "add"]
    assert "--source-cidr" in plan.commands[0]
    assert "192.168.0.0/24" in plan.commands[0]
    assert "--outbound-interface" in plan.commands[0]


def test_windows_virtual_adapter_plan_uses_bundled_wintun_helper() -> None:
    backend = WindowsBackend(dry_run=True)

    plan = backend.plan_virtual_adapter_create("py-virtual0", AddressInfo("192.168.56.1", 24))
    delete_plan = backend.plan_virtual_adapter_delete(VirtualAdapterInfo("py-virtual0", "wintun"))

    assert plan.commands[0][:4] == [sys.executable, "-m", "py_nic_manager.windows_wintun", "create"]
    assert "--name" in plan.commands[0]
    assert "py-virtual0" in plan.commands[0]
    assert "--address" in plan.commands[0]
    assert "192.168.56.1/24" in plan.commands[0]
    assert "wintun.dll" in " ".join(plan.notes)
    assert delete_plan.commands[0][:4] == [sys.executable, "-m", "py_nic_manager.windows_wintun", "delete"]


def test_linux_virtual_adapter_plan_creates_veth_pair() -> None:
    backend = LinuxBackend(dry_run=True)

    plan = backend.plan_virtual_adapter_create("py-virtual0", AddressInfo("192.168.56.1", 24))

    assert ["ip", "link", "add", "py-virtual0", "type", "veth", "peer", "name", "py-virtual-peer"] in plan.commands
    assert ["ip", "addr", "add", "192.168.56.1/24", "dev", "py-virtual0"] in plan.commands
    assert plan.title == "Create virtual adapter"


def test_macos_virtual_adapter_plan_creates_bridge() -> None:
    backend = MacOSBackend(dry_run=True)

    plan = backend.plan_virtual_adapter_create("bridge9", AddressInfo("192.168.56.1", 24))

    assert ["ifconfig", "bridge9", "create"] in plan.commands
    assert ["ifconfig", "bridge9", "inet", "192.168.56.1/24", "up"] in plan.commands


def test_iptables_nat_parser_marks_managed_and_external_rules() -> None:
    rules = _parse_iptables_nat_rules(
        '-A POSTROUTING -s 192.168.0.0/24 -o eth0 -m comment --comment "py-nic-manager-nat:nat0" -j MASQUERADE\n'
        "-A POSTROUTING -s 10.0.0.0/8 -o wlan0 -j MASQUERADE\n"
    )

    assert rules[0].name == "nat0"
    assert rules[0].managed is True
    assert rules[1].managed is False
    assert rules[1].persistent is False


def test_windows_nat_plan_uses_rras_or_ics_only() -> None:
    backend = WindowsBackend(dry_run=True)

    plan = backend.plan_nat_create(NatRule("nat0", "192.168.1.0/30", "WLAN"))
    rendered = " ".join(plan.commands[0])

    assert plan.restart_required is False
    assert "HNetCfg.HNetShare" in rendered
    assert '"routing", "ip", "nat"' in rendered
    assert "Invoke-RrasNat" in rendered
    assert "Invoke-IcsNat" in rendered
    assert "Format-IcsError" in rendered
    assert "specified cast is invalid" in rendered
    assert "Windows ICS cannot use loopback adapter" in rendered
    assert "RRAS NAT is not available or rejected public interface" in rendered
    assert "*> $null" in rendered
    assert "Set-Service" in rendered
    assert "SharedAccess" in rendered
    assert 'InterfaceAlias $InterfaceAlias' in rendered
    assert '$outboundInterface = "WLAN"' in rendered
    assert '$sourceCidr = "192.168.1.0/30"' in rendered
    assert "Get-BestInternalInterface" in rendered
    assert "ProgramData" in rendered
    assert "4294967295" in rendered
    assert "0xffffffff" not in rendered
    assert "Test-IPv4PrefixOverlap" in rendered
    assert "Failed to create Windows RRAS/ICS NAT rule" in rendered
    assert "Stop-PyNicManagerCommand" in rendered


def test_windows_nat_requires_outbound_interface() -> None:
    backend = WindowsBackend(dry_run=True)

    with pytest.raises(BackendError, match="outbound interface"):
        backend.plan_nat_create(NatRule("nat0", "192.168.0.0/16", ""))


def test_windows_nat_rejects_default_route_as_internal_prefix() -> None:
    backend = WindowsBackend(dry_run=True)

    with pytest.raises(BackendError, match="not 0.0.0.0/0"):
        backend.plan_nat_create(NatRule("nat0", "0.0.0.0/0"))


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
        global_forwarding_enabled=True,
    )

    plan = backend.plan_snapshot_apply(snapshot)
    rendered = "\n".join(" ".join(command) for command in plan.commands)

    assert 'Remove-NetRoute' in rendered
    assert "IPEnableRouter" in rendered
    assert 'DestinationPrefix "198.51.100.0/24"' in rendered
    assert 'New-NetRoute' in rendered
    assert 'DestinationPrefix "203.0.113.0/24"' in rendered
    assert plan.restart_required is True


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
    global_forwarding_plan = manager.plan_set_global_forwarding(True)
    create_loopback_plan = manager.plan_create_loopback()
    delete_loopback_plan = manager.plan_delete_loopback("py-loopback0")
    update_loopback_plan = manager.plan_update_loopback("py-loopback0", address="192.0.2.60/24")
    create_virtual_plan = manager.plan_create_virtual_adapter("py-virtual1", address="192.168.56.1/24")
    delete_virtual_plan = manager.plan_delete_virtual_adapter("py-virtual0")
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
    nat_plan = manager.plan_create_nat_rule("nat1", "192.168.10.0/24", outbound_interface="Ethernet")
    delete_nat_plan = manager.plan_delete_nat_rule("nat0")
    restart_plan = manager.plan_restart_system()

    assert loaded.platform == "Windows"
    assert loaded.global_forwarding_enabled is False
    assert loaded.nat_rules[0].name == "nat0"
    assert snapshot.adapters[0].name == "Ethernet"
    assert any("netsh" in command[0].lower() for command in adapter_plan.commands)
    assert "Set-NetIPInterface" in " ".join(forwarding_plan.commands[0])
    assert global_forwarding_plan.restart_required is True
    assert "IPEnableRouter" in " ".join(global_forwarding_plan.commands[0])
    assert "py-loopback1" in create_loopback_plan.commands[0]
    assert delete_loopback_plan.title == "Delete loopback adapter"
    assert update_loopback_plan.commands
    assert create_virtual_plan.title == "Create virtual adapter"
    assert delete_virtual_plan.title == "Delete virtual adapter"
    assert add_route_plan.title == "Add route"
    assert update_route_plan.title == "Update route"
    assert delete_route_plan.title == "Delete route"
    assert nat_plan.title == "Create NAT rule"
    assert delete_nat_plan.title == "Delete NAT rule"
    assert restart_plan.title == "Restart system"

    results = manager.run_plan(add_route_plan)
    assert all(result.ok for result in results)
    restart_result = manager.restart_system()
    assert restart_result.ok


def test_python_api_concurrent_snapshot_tolerates_optional_state_failures() -> None:
    class PartiallyFailingBackend(_FakeWindowsBackend):
        def list_nat_rules(self):
            raise RuntimeError("NAT is unavailable.")

        def get_global_forwarding_enabled(self):
            raise RuntimeError("Forwarding state is unavailable.")

    manager = NetworkManager(PartiallyFailingBackend(dry_run=True), admin_checker=lambda: False)

    snapshot = manager.get_snapshot(concurrent=True)

    assert snapshot.adapters
    assert snapshot.routes
    assert snapshot.nat_rules == []
    assert snapshot.global_forwarding_enabled is None


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
    def get_global_forwarding_enabled(self):
        return False

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
            AdapterInfo(
                id="virtual-id",
                name="py-virtual0",
                description="Wintun Userspace Tunnel",
                status="Up",
                addresses=[AddressInfo("192.168.56.1", 24)],
                is_virtual=True,
                virtual_kind="wintun",
                forwarding_enabled=True,
            ),
        ]

    def list_routes(self):
        return [
            RouteInfo("198.51.100.0/24", "192.0.2.1", "Ethernet", 10),
            RouteInfo("127.0.0.0/8", "", "Loopback Pseudo-Interface 1", None, protocol="Local"),
        ]

    def list_nat_rules(self):
        return [NatRule("nat0", "192.168.0.0/24", "Ethernet")]

    def list_virtual_adapters(self):
        return [
            VirtualAdapterInfo(
                name="py-virtual0",
                kind="wintun",
                status="Up",
                address="192.168.56.1/24",
                source_cidr="192.168.56.0/24",
                backend_id="virtual-id",
            )
        ]
