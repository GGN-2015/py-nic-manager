from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .backends import decode_command_output
from .windows_device_policy import assert_ndis_net_adapter, ensure_ndis_device_install_policy


HARDWARE_ID = r"root\tap0901"
STATE_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "py-nic-manager" / "tap"


def create_virtual_adapter(name: str, address: str = "") -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_ndis_device_install_policy()
    state = _load_state(clean_name)
    if _adapter_info(clean_name):
        if address:
            _configure_address(clean_name, address)
        state.update({"name": clean_name, "address": address})
        _save_state(clean_name, state)
        print(f"TAP virtual adapter already exists: {clean_name}")
        return

    before = _net_adapters()
    driver_dir = _tap_driver_dir()
    inf_path = driver_dir / "OemVista.inf"
    devcon_path = driver_dir / "devcon.exe"
    if not inf_path.exists() or not devcon_path.exists():
        raise RuntimeError(f"Bundled TAP-Windows6 driver files were not found for this architecture: {driver_dir}")

    _run(["pnputil", "/add-driver", str(inf_path), "/install"])
    _run([str(devcon_path), "install", str(inf_path), HARDWARE_ID])
    adapter = _wait_for_new_adapter(clean_name, before, timeout=45)
    try:
        if str(adapter.get("Name") or "") != clean_name:
            _rename_adapter(str(adapter.get("Name") or ""), clean_name)
            adapter = _wait_for_adapter(clean_name, timeout=30)
        assert_ndis_net_adapter(
            name=clean_name,
            interface_index=adapter.get("InterfaceIndex"),
            pnp_device_id=str(adapter.get("PnPDeviceID", "")),
        )
        _set_always_connected(clean_name)
        if address:
            _configure_address(clean_name, address)
            _assert_address_pingable(clean_name, address)
        _save_state(
            clean_name,
            {
                "name": clean_name,
                "address": address,
                "driver": "tap-windows6",
                "hardware_id": HARDWARE_ID,
                "created_at": int(time.time()),
            },
        )
    except Exception:
        _cleanup_created_adapter(clean_name, adapter)
        raise
    print(f"TAP virtual adapter created: {clean_name}")


def delete_virtual_adapter(name: str) -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    adapter = _adapter_info(clean_name)
    if adapter:
        _remove_adapter(adapter)
        _wait_for_adapter_removed(clean_name, timeout=20)
    state_path = _state_path(clean_name)
    if state_path.exists():
        state_path.unlink()
    print(f"TAP virtual adapter deleted: {clean_name}")


def list_virtual_adapters() -> list[dict[str, object]]:
    _ensure_windows()
    states = []
    if STATE_DIR.exists():
        for path in STATE_DIR.glob("*.json"):
            try:
                states.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    adapters = _net_adapters()
    by_name = {str(item.get("Name", "")): item for item in adapters}
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for state in states:
        name = str(state.get("name", ""))
        if not name:
            continue
        seen.add(name.lower())
        adapter = by_name.get(name)
        items.append(_virtual_item(name, adapter, state))
    for adapter in adapters:
        name = str(adapter.get("Name", ""))
        if name and name.lower() not in seen:
            items.append(_virtual_item(name, adapter, {}))
    return items


def _virtual_item(name: str, adapter: dict[str, object] | None, state: dict[str, object]) -> dict[str, object]:
    address = str(state.get("address", ""))
    if not address and adapter:
        address = _first_ipv4_for_interface(name)
    return {
        "name": name,
        "kind": "tap",
        "status": str(adapter.get("Status", "")) if adapter else "Missing",
        "address": address,
        "source_cidr": _source_cidr(address),
        "nat_capable": True,
        "persistent": True,
        "managed": bool(state),
        "backend_id": str(adapter.get("PnPDeviceID", "")) if adapter else "",
        "admin_enabled": str(adapter.get("AdminStatus", "")).lower() == "up" if adapter else None,
        "ics_compatible": True,
        "ics_note": "TAP-Windows6 is an Ethernet-like NDIS adapter and is the preferred Windows ICS private interface.",
    }


def _tap_driver_dir() -> Path:
    machine = platform.machine().lower()
    arch = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "i386": "i386",
        "i686": "i386",
        "x86": "i386",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if not arch:
        raise RuntimeError(f"TAP-Windows6 is not bundled for this Windows architecture: {platform.machine()}")
    return Path(__file__).resolve().parent / "assets" / "tap-windows6" / "dist.win10" / arch


def _configure_address(name: str, address: str) -> None:
    if "/" not in address:
        raise RuntimeError("Virtual NIC address must use CIDR notation, for example 192.168.50.1/24.")
    ip, prefix = address.split("/", 1)
    prefix_length = int(prefix)
    if not 0 <= prefix_length <= 32:
        raise RuntimeError("Virtual NIC IPv4 prefix length must be between 0 and 32.")
    adapter = _wait_for_adapter(name, timeout=45)
    interface_index = int(adapter.get("InterfaceIndex") or 0)
    if interface_index <= 0:
        raise RuntimeError(f"TAP adapter '{name}' does not have a usable interface index yet.")
    script = f"""
$ErrorActionPreference = "Stop"
$interfaceIndex = {interface_index}
$ipAddress = "{_ps_escape(ip)}"
$prefixLength = {prefix_length}
$lastError = ""
for ($attempt = 0; $attempt -lt 24; $attempt++) {{
  try {{
    $adapter = Get-NetAdapter -IncludeHidden -InterfaceIndex $interfaceIndex -ErrorAction Stop
    Enable-NetAdapter -InputObject $adapter -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Set-NetIPInterface -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue | Out-Null
    Get-NetIPAddress -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object {{ $_.PrefixOrigin -ne "WellKnown" -and ($_.IPAddress -ne $ipAddress -or [int]$_.PrefixLength -ne $prefixLength) }} |
      Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    $existing = Get-NetIPAddress -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object {{ $_.IPAddress -eq $ipAddress -and [int]$_.PrefixLength -eq $prefixLength }} |
      Select-Object -First 1
    if (-not $existing) {{
      New-NetIPAddress -InterfaceIndex $interfaceIndex -IPAddress $ipAddress -PrefixLength $prefixLength -ErrorAction Stop | Out-Null
    }}
    exit 0
  }} catch {{
    $lastError = $_.Exception.Message
    Start-Sleep -Milliseconds 750
  }}
}}
[Console]::Error.WriteLine("Failed to configure IPv4 address {ip}/{prefix_length} on TAP adapter '{_ps_escape(name)}' (interface index {interface_index}): $lastError")
exit 1
"""
    _run_powershell(script)


def _rename_adapter(current_name: str, new_name: str) -> None:
    if not current_name:
        raise RuntimeError("The created TAP adapter did not expose a network connection name.")
    script = (
        f"if (Get-NetAdapter -Name {_ps_quote(new_name)} -ErrorAction SilentlyContinue) "
        f'{{ throw "An adapter named {new_name} already exists." }} '
        f"Rename-NetAdapter -Name {_ps_quote(current_name)} -NewName {_ps_quote(new_name)} -Confirm:$false"
    )
    _run_powershell(script)


def _set_always_connected(name: str) -> None:
    script = f"""
$adapter = Get-NetAdapter -Name {_ps_quote(name)} -IncludeHidden -ErrorAction Stop
$property = Get-NetAdapterAdvancedProperty -Name {_ps_quote(name)} -RegistryKeyword "MediaStatus" -ErrorAction SilentlyContinue
if ($property) {{
  Set-NetAdapterAdvancedProperty -Name {_ps_quote(name)} -RegistryKeyword "MediaStatus" -RegistryValue "1" -NoRestart -ErrorAction SilentlyContinue | Out-Null
  Disable-NetAdapter -InputObject $adapter -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
  Start-Sleep -Milliseconds 500
  Enable-NetAdapter -Name {_ps_quote(name)} -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}}
"""
    _run_powershell(script)


def _wait_for_new_adapter(name: str, before: list[dict[str, object]], timeout: int) -> dict[str, object]:
    before_ids = {str(item.get("PnPDeviceID", "")) for item in before}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exact = _adapter_info(name)
        if exact:
            return exact
        after = _net_adapters()
        new_adapters = [item for item in after if str(item.get("PnPDeviceID", "")) not in before_ids]
        if new_adapters:
            return sorted(new_adapters, key=lambda item: int(item.get("InterfaceIndex", 0)), reverse=True)[0]
        time.sleep(1)
    raise RuntimeError(f"TAP adapter '{name}' did not appear after creation.")


def _wait_for_adapter(name: str, timeout: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        adapter = _adapter_info(name)
        if adapter:
            return adapter
        time.sleep(1)
    raise RuntimeError(f"TAP adapter '{name}' did not appear.")


def _wait_for_adapter_removed(name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _adapter_info(name):
            return
        time.sleep(1)
    raise RuntimeError(f"TAP adapter '{name}' still exists after deletion.")


def _adapter_info(name: str) -> dict[str, object] | None:
    clean_name = name.lower()
    for item in _net_adapters():
        if str(item.get("Name", "")).lower() == clean_name:
            return item
    return None


def _net_adapters() -> list[dict[str, object]]:
    script = r"""
Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
  Where-Object { $_.InterfaceDescription -match "TAP-Windows|TAP Adapter|tap0901" -or $_.Name -like "py-virtual*" } |
  Sort-Object -Property InterfaceIndex |
  Select-Object Name, InterfaceIndex, InterfaceDescription, Status, AdminStatus, PnPDeviceID |
  ConvertTo-Json -Depth 4
"""
    result = _run_powershell(script)
    if not result:
        return []
    parsed = json.loads(result)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _first_ipv4_for_interface(name: str) -> str:
    script = (
        f"Get-NetIPAddress -InterfaceAlias {_ps_quote(name)} -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        'Where-Object { $_.IPAddress -and [int]$_.PrefixLength -lt 32 } | '
        'Select-Object -First 1 IPAddress, PrefixLength | ConvertTo-Json -Depth 3'
    )
    result = _run_powershell(script)
    if not result:
        return ""
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict) and parsed.get("IPAddress"):
        return f"{parsed['IPAddress']}/{parsed.get('PrefixLength', 24)}"
    return ""


def _source_cidr(address: str) -> str:
    if "/" not in address:
        return ""
    try:
        import ipaddress

        return str(ipaddress.ip_interface(address).network)
    except ValueError:
        return ""


def _address_ip(address: str) -> str:
    return address.split("/", 1)[0].strip()


def _assert_address_pingable(name: str, address: str) -> None:
    ip = _address_ip(address)
    if not ip:
        return
    last_error = ""
    for _attempt in range(12):
        completed = subprocess.run(["ping", "-n", "1", "-w", "1000", ip], capture_output=True, check=False)
        if completed.returncode == 0:
            return
        output = (decode_command_output(completed.stdout) + "\n" + decode_command_output(completed.stderr)).strip()
        last_error = output or f"ping exited with code {completed.returncode}"
        time.sleep(1)
    raise RuntimeError(
        f"TAP virtual adapter '{name}' was created with {address}, but the local host cannot ping {ip}. "
        f"The adapter is not usable like a loopback adapter. Last ping error: {last_error}"
    )


def _remove_adapter(adapter: dict[str, object]) -> None:
    pnp_device_id = str(adapter.get("PnPDeviceID") or "")
    if not pnp_device_id:
        raise RuntimeError("The TAP adapter does not expose a PnP device ID.")
    script = rf"""
$device = Get-PnpDevice -InstanceId "{_ps_escape(pnp_device_id)}" -ErrorAction SilentlyContinue
if (-not $device) {{
  throw "Windows device for TAP adapter '{_ps_escape(str(adapter.get("Name", "")))}' was not found."
}}
$output = pnputil /remove-device $device.InstanceId /subtree /force 2>&1
if ($LASTEXITCODE -ne 0) {{
  throw (($output | Out-String).Trim())
}}
"""
    _run_powershell(script)


def _cleanup_created_adapter(name: str, adapter: dict[str, object] | None) -> None:
    current = _adapter_info(name) or adapter
    if not current:
        return
    try:
        _remove_adapter(current)
    except Exception:
        return
    try:
        _wait_for_adapter_removed(name, timeout=15)
    except Exception:
        pass


def _state_path(name: str) -> Path:
    return STATE_DIR / f"{uuid.uuid5(uuid.NAMESPACE_DNS, name.lower())}.json"


def _load_state(name: str) -> dict[str, object]:
    path = _state_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(name: str, state: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(name).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _clean_adapter_name(name: str) -> str:
    clean = name.strip()
    if not clean:
        raise RuntimeError("A virtual adapter name is required.")
    if len(clean) > 128:
        raise RuntimeError("Virtual adapter name is too long.")
    return clean


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, check=False)
    stdout = decode_command_output(completed.stdout).strip()
    stderr = decode_command_output(completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or f"Command failed with exit code {completed.returncode}: {' '.join(command)}")
    return stdout


def _run_powershell(script: str) -> str:
    return _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_escape(value: str) -> str:
    return value.replace("`", "``").replace('"', '`"').replace("$", "`$")


def _ensure_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("TAP virtual adapter management is only available on Windows.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Py NIC Manager TAP virtual adapters.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create a TAP virtual adapter.")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--address", default="")
    delete_parser = subparsers.add_parser("delete", help="Delete a TAP virtual adapter.")
    delete_parser.add_argument("--name", required=True)
    subparsers.add_parser("list", help="List Py NIC Manager TAP adapters.")
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            create_virtual_adapter(args.name, args.address)
            return 0
        if args.command == "delete":
            delete_virtual_adapter(args.name)
            return 0
        if args.command == "list":
            print(json.dumps(list_virtual_adapters(), indent=2))
            return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
