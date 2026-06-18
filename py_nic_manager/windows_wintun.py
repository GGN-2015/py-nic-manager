from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import signal
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from .backends import decode_command_output
from .windows_device_policy import assert_ndis_net_adapter, ensure_ndis_device_install_policy


POOL_NAME = "PyNicManager"
TUNNEL_TYPE = "PyNIC"
STATE_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "py-nic-manager" / "wintun"


class NET_LUID(ctypes.Structure):
    _fields_ = [("Value", ctypes.c_uint64)]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


def create_virtual_adapter(name: str, address: str = "") -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stop_path = _stop_path(clean_name)
    if stop_path.exists():
        stop_path.unlink()
    dll_path = _wintun_dll_path()
    state = _load_state(clean_name)
    if _adapter_exists(clean_name):
        assert_ndis_net_adapter(name=clean_name)
        if address:
            _configure_address(clean_name, address)
        state.update({"name": clean_name, "dll_path": str(dll_path), "address": address, "stop_path": str(stop_path)})
        _save_state(clean_name, state)
        print(f"Wintun adapter already exists: {clean_name}")
        return

    ensure_ndis_device_install_policy()
    command = [
        sys.executable,
        "-m",
        "py_nic_manager.windows_wintun",
        "keeper",
        "--name",
        clean_name,
        "--dll",
        str(dll_path),
        "--stop-file",
        str(stop_path),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        adapter = _wait_for_adapter(clean_name, timeout=25)
    except Exception:
        _terminate_process(process.pid)
        raise
    try:
        assert_ndis_net_adapter(
            name=clean_name,
            interface_index=adapter.get("InterfaceIndex"),
            pnp_device_id=str(adapter.get("PnPDeviceID", "")),
        )
        if address:
            _configure_address(clean_name, address)
        task_name = _install_startup_task(clean_name, address)
        _save_state(
            clean_name,
            {
                "name": clean_name,
                "pid": process.pid,
                "dll_path": str(dll_path),
                "address": address,
                "stop_path": str(stop_path),
                "task_name": task_name,
                "created_at": int(time.time()),
            },
        )
    except Exception:
        _request_keeper_stop(clean_name, process.pid, stop_path)
        if _adapter_exists(clean_name):
            _terminate_process(process.pid)
        raise
    print(f"Wintun virtual adapter created: {clean_name}")


def delete_virtual_adapter(name: str) -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    state = _load_state(clean_name)
    task_name = str(state.get("task_name") or _task_name(clean_name))
    _remove_startup_task(task_name)
    pid = int(state.get("pid") or 0)
    stop_path = Path(str(state.get("stop_path") or _stop_path(clean_name)))
    graceful_stop_supported = bool(state.get("stop_path"))
    if pid and graceful_stop_supported:
        _request_keeper_stop(clean_name, pid, stop_path)
    elif pid:
        _terminate_process(pid)
        time.sleep(1)
    if _adapter_exists(clean_name):
        if pid:
            _terminate_process(pid)
            time.sleep(1)
        _remove_adapter_by_name(clean_name)
        _wait_for_adapter_removed(clean_name, timeout=20)
    state_path = _state_path(clean_name)
    if state_path.exists():
        state_path.unlink()
    if stop_path.exists():
        stop_path.unlink()
    print(f"Wintun virtual adapter deleted: {clean_name}")


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


def keeper(name: str, dll_path: str, stop_file: str = "") -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    stop_path = Path(stop_file) if stop_file else None
    dll = _load_wintun(dll_path)
    adapter = dll.WintunCreateAdapter(clean_name, TUNNEL_TYPE, None)
    if not adapter:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error, "WintunCreateAdapter failed")
    try:
        _set_interface_up(clean_name)
        while True:
            if stop_path and stop_path.exists():
                break
            time.sleep(1)
    finally:
        dll.WintunCloseAdapter(adapter)


def _virtual_item(name: str, adapter: dict[str, object] | None, state: dict[str, object]) -> dict[str, object]:
    address = str(state.get("address", ""))
    if not address and adapter:
        address = _first_ipv4_for_interface(name)
    source_cidr = _source_cidr(address)
    return {
        "name": name,
        "kind": "wintun",
        "status": str(adapter.get("Status", "")) if adapter else "Missing",
        "address": address,
        "source_cidr": source_cidr,
        "nat_capable": False,
        "persistent": True,
        "managed": bool(state),
        "backend_id": str(adapter.get("PnPDeviceID", "")) if adapter else "",
        "ics_compatible": False,
        "ics_note": "Wintun is a layer-3 TUN adapter. Windows ICS often rejects it as a private/shared interface.",
    }


def _load_wintun(dll_path: str):
    dll = ctypes.WinDLL(dll_path, use_last_error=True)
    dll.WintunCreateAdapter.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    dll.WintunCreateAdapter.restype = wintypes.HANDLE
    dll.WintunOpenAdapter.argtypes = [wintypes.LPCWSTR]
    dll.WintunOpenAdapter.restype = wintypes.HANDLE
    dll.WintunCloseAdapter.argtypes = [wintypes.HANDLE]
    dll.WintunCloseAdapter.restype = None
    dll.WintunGetRunningDriverVersion.argtypes = []
    dll.WintunGetRunningDriverVersion.restype = wintypes.DWORD
    return dll


def _wintun_dll_path() -> Path:
    machine = platform.machine().lower()
    arch = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "i386": "x86",
        "i686": "x86",
        "x86": "x86",
        "arm64": "arm64",
        "aarch64": "arm64",
        "arm": "arm",
    }.get(machine)
    if not arch:
        raise RuntimeError(f"Wintun is not bundled for this Windows architecture: {platform.machine()}")
    path = Path(__file__).resolve().parent / "assets" / "wintun" / arch / "wintun.dll"
    if not path.exists():
        raise RuntimeError(f"Bundled wintun.dll was not found: {path}")
    return path


def _configure_address(name: str, address: str) -> None:
    if "/" not in address:
        raise RuntimeError("Virtual NIC address must use CIDR notation, for example 192.168.50.1/24.")
    ip, prefix = address.split("/", 1)
    prefix_length = int(prefix)
    if not 0 <= prefix_length <= 32:
        raise RuntimeError("Virtual NIC IPv4 prefix length must be between 0 and 32.")
    adapter = _wait_for_adapter(name, timeout=45)
    adapter_name = str(adapter.get("Name") or name)
    interface_index = int(adapter.get("InterfaceIndex") or 0)
    if interface_index <= 0:
        raise RuntimeError(f"Wintun adapter '{name}' does not have a usable interface index yet.")
    script = f"""
$ErrorActionPreference = "Stop"
$interfaceIndex = {interface_index}
$adapterName = "{_ps_escape(adapter_name)}"
$ipAddress = "{_ps_escape(ip)}"
$prefixLength = {prefix_length}
$lastError = ""
for ($attempt = 0; $attempt -lt 24; $attempt++) {{
  try {{
    $adapter = Get-NetAdapter -IncludeHidden -InterfaceIndex $interfaceIndex -ErrorAction Stop
    Enable-NetAdapter -InputObject $adapter -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Milliseconds 250
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
[Console]::Error.WriteLine("Failed to configure IPv4 address {ip}/{prefix_length} on Wintun adapter '{_ps_escape(adapter_name)}' (interface index {interface_index}): $lastError")
exit 1
"""
    _run_powershell(script)


def _set_interface_up(name: str) -> None:
    try:
        adapter = _wait_for_adapter(name, timeout=15)
        interface_index = int(adapter.get("InterfaceIndex") or 0)
        if interface_index <= 0:
            return
        script = f"""
$adapter = Get-NetAdapter -IncludeHidden -InterfaceIndex {interface_index} -ErrorAction SilentlyContinue
if ($adapter) {{
  Enable-NetAdapter -InputObject $adapter -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}}
"""
        _run_powershell(script)
    except RuntimeError:
        pass


def _adapter_exists(name: str) -> bool:
    return _adapter_info(name) is not None


def _wait_for_adapter(name: str, timeout: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        adapter = _adapter_info(name)
        if adapter:
            return adapter
        time.sleep(1)
    raise RuntimeError(f"Wintun adapter '{name}' did not appear after creation.")


def _adapter_info(name: str) -> dict[str, object] | None:
    clean_name = name.lower()
    for item in _net_adapters():
        if str(item.get("Name", "")).lower() == clean_name:
            return item
    return None


def _net_adapters() -> list[dict[str, object]]:
    script = r"""
Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
  Where-Object { $_.InterfaceDescription -match "Wintun|WireGuard|PyNIC" -or $_.Name -like "py-virtual*" } |
  Sort-Object -Property InterfaceIndex |
  Select-Object Name, InterfaceIndex, InterfaceDescription, Status, PnPDeviceID |
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
        f'Get-NetIPAddress -InterfaceAlias "{_ps_escape(name)}" -AddressFamily IPv4 -ErrorAction SilentlyContinue | '
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


def _remove_adapter_by_name(name: str) -> None:
    adapter = _adapter_info(name)
    pnp_device_id = str(adapter.get("PnPDeviceID") or "") if adapter else ""
    pnp_filter = f'$_.InstanceId -eq "{_ps_escape(pnp_device_id)}" -or' if pnp_device_id else ""
    script = rf"""
$device = Get-PnpDevice -ErrorAction SilentlyContinue |
  Where-Object {{ {pnp_filter} $_.FriendlyName -eq "{_ps_escape(name)}" -or $_.FriendlyName -like "*{_ps_escape(name)}*" }} |
  Select-Object -First 1
if (-not $device) {{
  throw "Windows device for Wintun adapter '{_ps_escape(name)}' was not found."
}}
$output = pnputil /remove-device $device.InstanceId /subtree /force 2>&1
if ($LASTEXITCODE -ne 0) {{
  throw (($output | Out-String).Trim())
}}
"""
    _run_powershell(script)


def _request_keeper_stop(name: str, pid: int, stop_path: Path) -> None:
    try:
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    for _attempt in range(15):
        if not _process_exists(pid):
            return
        if not _adapter_exists(name):
            return
        time.sleep(1)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_process(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _attempt in range(10):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.3)
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    except OSError:
        pass


def _install_startup_task(name: str, address: str) -> str:
    task_name = _task_name(name)
    command = (
        f'"{sys.executable}" -m py_nic_manager.windows_wintun '
        f'create --name "{name}" --address "{address}"'
    )
    _run(
        [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/SC",
            "ONSTART",
            "/RU",
            "SYSTEM",
            "/RL",
            "HIGHEST",
            "/TR",
            command,
            "/F",
        ]
    )
    return task_name


def _remove_startup_task(task_name: str) -> None:
    try:
        _run(["schtasks", "/Delete", "/TN", task_name, "/F"])
    except RuntimeError:
        pass


def _task_name(name: str) -> str:
    return rf"\PyNicManager\Wintun-{uuid.uuid5(uuid.NAMESPACE_DNS, name.lower())}"


def _state_path(name: str) -> Path:
    return STATE_DIR / f"{uuid.uuid5(uuid.NAMESPACE_DNS, name.lower())}.json"


def _stop_path(name: str) -> Path:
    return STATE_DIR / f"{uuid.uuid5(uuid.NAMESPACE_DNS, name.lower())}.stop"


def _wait_for_adapter_removed(name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _adapter_exists(name):
            return
        time.sleep(1)
    raise RuntimeError(f"Wintun adapter '{name}' still exists after deletion.")


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


def _prefix_to_netmask(prefix: int) -> str:
    if prefix < 0 or prefix > 32:
        raise RuntimeError("IPv4 prefix length must be between 0 and 32.")
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))


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


def _ps_escape(value: str) -> str:
    return value.replace('"', '`"')


def _ensure_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Wintun virtual adapter management is only available on Windows.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Py NIC Manager Wintun virtual adapters.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create a Wintun virtual adapter.")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--address", default="")
    delete_parser = subparsers.add_parser("delete", help="Delete a Wintun virtual adapter.")
    delete_parser.add_argument("--name", required=True)
    subparsers.add_parser("list", help="List Py NIC Manager Wintun adapters.")
    keeper_parser = subparsers.add_parser("keeper", help=argparse.SUPPRESS)
    keeper_parser.add_argument("--name", required=True)
    keeper_parser.add_argument("--dll", required=True)
    keeper_parser.add_argument("--stop-file", default="")
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
        if args.command == "keeper":
            keeper(args.name, args.dll, args.stop_file)
            return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
