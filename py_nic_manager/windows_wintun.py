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
    dll_path = _wintun_dll_path()
    state = _load_state(clean_name)
    if _adapter_exists(clean_name):
        if address:
            _configure_address(clean_name, address)
        state.update({"name": clean_name, "dll_path": str(dll_path), "address": address})
        _save_state(clean_name, state)
        print(f"Wintun adapter already exists: {clean_name}")
        return

    command = [
        sys.executable,
        "-m",
        "py_nic_manager.windows_wintun",
        "keeper",
        "--name",
        clean_name,
        "--dll",
        str(dll_path),
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
        _wait_for_adapter(clean_name, timeout=25)
    except Exception:
        _terminate_process(process.pid)
        raise
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
            "task_name": task_name,
            "created_at": int(time.time()),
        },
    )
    print(f"Wintun virtual adapter created: {clean_name}")


def delete_virtual_adapter(name: str) -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    state = _load_state(clean_name)
    task_name = str(state.get("task_name") or _task_name(clean_name))
    _remove_startup_task(task_name)
    pid = int(state.get("pid") or 0)
    if pid:
        _terminate_process(pid)
        time.sleep(2)
    if _adapter_exists(clean_name):
        dll = _load_wintun(str(state.get("dll_path") or _wintun_dll_path()))
        adapter = dll.WintunOpenAdapter(clean_name)
        if adapter:
            dll.WintunCloseAdapter(adapter)
        else:
            _remove_adapter_by_name(clean_name)
    state_path = _state_path(clean_name)
    if state_path.exists():
        state_path.unlink()
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


def keeper(name: str, dll_path: str) -> None:
    _ensure_windows()
    clean_name = _clean_adapter_name(name)
    dll = _load_wintun(dll_path)
    adapter = dll.WintunCreateAdapter(clean_name, TUNNEL_TYPE, None)
    if not adapter:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error, "WintunCreateAdapter failed")
    try:
        _set_interface_up(clean_name)
        while True:
            time.sleep(3600)
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
        "nat_capable": True,
        "persistent": True,
        "managed": bool(state),
        "backend_id": str(adapter.get("PnPDeviceID", "")) if adapter else "",
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
    netmask = _prefix_to_netmask(int(prefix))
    _run(["netsh", "interface", "ip", "set", "address", f"name={name}", "static", ip, netmask])
    _run(["netsh", "interface", "set", "interface", f"name={name}", "admin=enabled"])


def _set_interface_up(name: str) -> None:
    try:
        _run(["netsh", "interface", "set", "interface", f"name={name}", "admin=enabled"])
    except RuntimeError:
        pass


def _adapter_exists(name: str) -> bool:
    return any(str(item.get("Name", "")) == name for item in _net_adapters())


def _wait_for_adapter(name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _adapter_exists(name):
            return
        time.sleep(1)
    raise RuntimeError(f"Wintun adapter '{name}' did not appear after creation.")


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
    script = rf"""
$device = Get-PnpDevice |
  Where-Object {{ $_.FriendlyName -eq "{_ps_escape(name)}" -or $_.FriendlyName -like "*{_ps_escape(name)}*" }} |
  Select-Object -First 1
if ($device) {{
  pnputil /remove-device $device.InstanceId
}}
"""
    _run_powershell(script)


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
            keeper(args.name, args.dll)
            return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
