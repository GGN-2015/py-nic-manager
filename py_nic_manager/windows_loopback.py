from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from .backends import decode_command_output


HARDWARE_ID = "*MSLOOP"
INSTALLFLAG_FORCE = 0x00000001
DICD_GENERATE_ID = 0x00000001
DIF_REGISTERDEVICE = 0x00000019
SPDRP_HARDWAREID = 0x00000001
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_CLASS_NAME_LEN = 32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


def create_loopback_adapter(name: str = "") -> None:
    _ensure_windows()
    inf_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "inf" / "netloop.inf"
    if not inf_path.exists():
        raise RuntimeError(f"Windows loopback driver INF was not found: {inf_path}")

    before = _loopback_adapters()
    _create_root_device(str(inf_path), HARDWARE_ID)
    time.sleep(2)

    if name:
        adapter = _find_created_adapter(before)
        if adapter:
            _rename_adapter(adapter["Name"], name)


def _create_root_device(inf_path: str, hardware_id: str) -> None:
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    newdev = ctypes.WinDLL("newdev", use_last_error=True)

    setupapi.SetupDiGetINFClassW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(GUID),
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    setupapi.SetupDiGetINFClassW.restype = wintypes.BOOL
    setupapi.SetupDiCreateDeviceInfoList.argtypes = [ctypes.POINTER(GUID), wintypes.HWND]
    setupapi.SetupDiCreateDeviceInfoList.restype = wintypes.HANDLE
    setupapi.SetupDiCreateDeviceInfoW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        ctypes.POINTER(GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.POINTER(SP_DEVINFO_DATA),
    ]
    setupapi.SetupDiCreateDeviceInfoW.restype = wintypes.BOOL
    setupapi.SetupDiSetDeviceRegistryPropertyW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(SP_DEVINFO_DATA),
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    setupapi.SetupDiSetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setupapi.SetupDiCallClassInstaller.argtypes = [
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(SP_DEVINFO_DATA),
    ]
    setupapi.SetupDiCallClassInstaller.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    newdev.UpdateDriverForPlugAndPlayDevicesW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.BOOL),
    ]
    newdev.UpdateDriverForPlugAndPlayDevicesW.restype = wintypes.BOOL

    class_guid = GUID()
    class_name = ctypes.create_unicode_buffer(MAX_CLASS_NAME_LEN)
    if not setupapi.SetupDiGetINFClassW(inf_path, ctypes.byref(class_guid), class_name, MAX_CLASS_NAME_LEN, None):
        _raise_last_error("SetupDiGetINFClassW")

    device_info_set = setupapi.SetupDiCreateDeviceInfoList(ctypes.byref(class_guid), None)
    if device_info_set == INVALID_HANDLE_VALUE:
        _raise_last_error("SetupDiCreateDeviceInfoList")

    try:
        device_info = SP_DEVINFO_DATA()
        device_info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
        if not setupapi.SetupDiCreateDeviceInfoW(
            device_info_set,
            class_name.value,
            ctypes.byref(class_guid),
            None,
            None,
            DICD_GENERATE_ID,
            ctypes.byref(device_info),
        ):
            _raise_last_error("SetupDiCreateDeviceInfoW")

        hardware_id_multi_sz = (hardware_id + "\0\0").encode("utf-16-le")
        buffer = ctypes.create_string_buffer(hardware_id_multi_sz)
        if not setupapi.SetupDiSetDeviceRegistryPropertyW(
            device_info_set,
            ctypes.byref(device_info),
            SPDRP_HARDWAREID,
            ctypes.cast(buffer, ctypes.c_void_p),
            len(hardware_id_multi_sz),
        ):
            _raise_last_error("SetupDiSetDeviceRegistryPropertyW")

        if not setupapi.SetupDiCallClassInstaller(DIF_REGISTERDEVICE, device_info_set, ctypes.byref(device_info)):
            _raise_last_error("SetupDiCallClassInstaller(DIF_REGISTERDEVICE)")

        reboot = wintypes.BOOL(False)
        if not newdev.UpdateDriverForPlugAndPlayDevicesW(
            None,
            hardware_id,
            inf_path,
            INSTALLFLAG_FORCE,
            ctypes.byref(reboot),
        ):
            _raise_last_error("UpdateDriverForPlugAndPlayDevicesW")
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info_set)


def _loopback_adapters() -> list[dict[str, object]]:
    script = r"""
Get-NetAdapter -IncludeHidden |
  Where-Object { $_.InterfaceDescription -match "Loopback|KM-TEST|Npcap Loopback" } |
  Sort-Object -Property InterfaceIndex |
  Select-Object Name, InterfaceIndex, InterfaceDescription, PnPDeviceID |
  ConvertTo-Json -Depth 3
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


def _find_created_adapter(before: list[dict[str, object]]) -> dict[str, object] | None:
    before_ids = {str(item.get("PnPDeviceID", "")) for item in before}
    for _attempt in range(8):
        after = _loopback_adapters()
        new_adapters = [item for item in after if str(item.get("PnPDeviceID", "")) not in before_ids]
        if new_adapters:
            return sorted(new_adapters, key=lambda item: int(item.get("InterfaceIndex", 0)), reverse=True)[0]
        if after:
            return sorted(after, key=lambda item: int(item.get("InterfaceIndex", 0)), reverse=True)[0]
        time.sleep(1)
    return None


def _rename_adapter(current_name: object, new_name: str) -> None:
    old = str(current_name)
    escaped_old = _ps_quote(old)
    escaped_new = _ps_quote(new_name)
    script = (
        f"if (Get-NetAdapter -Name {escaped_new} -ErrorAction SilentlyContinue) "
        f'{{ throw "An adapter named {new_name} already exists." }} '
        f"Rename-NetAdapter -Name {escaped_old} -NewName {escaped_new} -Confirm:$false"
    )
    _run_powershell(script)


def _run_powershell(script: str) -> str:
    executable = "powershell"
    completed = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        check=False,
    )
    stdout = decode_command_output(completed.stdout).strip()
    stderr = decode_command_output(completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or f"PowerShell failed with exit code {completed.returncode}.")
    return stdout


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _raise_last_error(api_name: str) -> None:
    error = ctypes.get_last_error()
    raise ctypes.WinError(error, f"{api_name} failed")


def _ensure_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Windows loopback adapter creation is only available on Windows.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a Windows KM-TEST Loopback Adapter.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create a loopback adapter.")
    create_parser.add_argument("--name", default="", help="Rename the created adapter.")
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            create_loopback_adapter(args.name.strip())
            print("Windows KM-TEST Loopback Adapter created.")
            return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
