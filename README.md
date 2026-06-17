# Py NIC Manager

Py NIC Manager is a cross-platform Python GUI for viewing and changing network
adapter settings, loopback-style adapters, route tables, and saved network
configuration snapshots.

The application is written in English and uses a modern PyQt6 interface with an
automatic light/dark theme. A legacy `tkinter` interface remains available as a
fallback if PyQt6 cannot be imported. It can run on Windows and POSIX systems.
Administrative actions require Administrator/root privileges; when the app is
started without those privileges, it opens in read-only mode and clearly asks
the user to restart it with elevated permissions.

## Features

- View network adapters, IPv4 addresses, MAC addresses, gateways, DNS servers,
  DHCP state, IPv4 router-forwarding state, and loopback status.
- Edit existing adapter IPv4 address, prefix length, gateway, DNS servers, MAC
  address, and DHCP mode where the operating system backend supports it.
- Create, edit, and delete loopback-style adapters:
  - Windows: Microsoft KM-TEST Loopback Adapter through the built-in
    `netloop.inf` driver and Windows SetupAPI.
  - Linux: dummy interfaces through `ip link`.
  - macOS and generic POSIX: loopback aliases on `lo0`.
- View, add, update, and delete IPv4 routes through a visual route table editor.
- Enable or disable IPv4 router forwarding for a selected adapter where the
  operating system backend supports per-interface forwarding.
- Export the current adapters and routes to a JSON configuration snapshot.
- Import a saved snapshot and apply it as a best-effort one-click restore after
  previewing the system commands that will run.
- Preview every mutating command before execution.
- Use the headless Python programming API for the same adapter, loopback, route,
  forwarding, and snapshot operations exposed by the GUI.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

On Linux or macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

The project depends on
[`is-admin-user`](https://pypi.org/project/is-admin-user/) for privilege
detection and PyQt6 for the default GUI.

## Running

```bash
py-nic-manager
```

Or:

```bash
python -m py_nic_manager
```

Use an elevated shell when you want to change system settings:

- Windows: run PowerShell or Command Prompt as Administrator.
- Linux/macOS/POSIX: run with `sudo`, `doas`, or an equivalent root session.

Without elevation, the app can still view adapters/routes and export
configuration snapshots.

## Programming API

Py NIC Manager also provides a headless Python API:

```python
from py_nic_manager import NetworkManager

manager = NetworkManager(dry_run=True)
plan = manager.plan_create_loopback()
print(plan.as_text())
```

See [PROGRAMMING_API.md](PROGRAMMING_API.md) for the complete API reference.

## Platform Notes

IPv4 router forwarding means the operating system may forward IP packets that
arrive on one interface and are destined for another host. It is not required
for ordinary web browsing, Wi-Fi connectivity, DNS, or other traffic generated
by the local machine.

### Windows

The Windows backend uses PowerShell networking cmdlets, `netsh`, `route`, and
Windows SetupAPI/NewDev calls through `ctypes`.

Creating a Microsoft KM-TEST Loopback Adapter uses the built-in
`%WINDIR%\inf\netloop.inf` driver directly. It does not require `devcon.exe` or
the Windows Driver Kit.

Per-adapter IPv4 router forwarding uses `Get-NetIPInterface` and
`Set-NetIPInterface -Forwarding`.

### Linux

The Linux backend uses `ip` from iproute2. DNS and DHCP persistence are handled
through NetworkManager (`nmcli`) when available, with `resolvectl` used as a DNS
fallback.

Loopback-style adapters are implemented as Linux dummy interfaces.

Per-adapter IPv4 router forwarding uses
`net.ipv4.conf.<interface>.forwarding`.

### macOS

The macOS backend uses `networksetup`, `ifconfig`, `route`, and `netstat`.
Loopback creation is implemented as an address alias on `lo0`, because macOS
does not create independent loopback NICs in the same way Linux creates dummy
interfaces.

macOS has a global IPv4 forwarding switch rather than the same per-interface
switch exposed by Windows and Linux. Py NIC Manager enables global forwarding
when needed and uses a `pf` anchor to block forwarded IPv4 packets received on
interfaces that are disabled in the UI.

### Generic POSIX

For POSIX systems that are not Linux or macOS, the app uses a conservative
`ifconfig`/`route` fallback. Viewing should work on many Unix-like systems, but
some mutating operations are intentionally limited because network management
varies widely across BSDs and commercial Unix systems.

## Configuration Snapshots

Exported files are JSON documents with this high-level shape:

```json
{
  "schema_version": 1,
  "platform": "Windows",
  "captured_at": "2026-06-17T02:00:00+00:00",
  "adapters": [],
  "routes": []
}
```

When applying an imported snapshot, Py NIC Manager:

1. Matches adapters by backend ID first, then by adapter name.
2. Updates matched adapters with the saved IPv4, gateway, DNS, MAC, and DHCP
   values where supported.
3. Adds saved IPv4 routes.
4. Shows skipped adapters and platform limitations in the command preview.

Applying a snapshot from another operating system is allowed only after a
warning and is best-effort.

## Development

Run tests:

```bash
python -m pytest -q
```

Run a syntax check:

```bash
python -m compileall py_nic_manager tests
```

## Safety

Network configuration changes can disconnect the machine, break DNS resolution,
or remove routes that are needed for remote access. Always review the command
preview before applying changes, and export a known-good snapshot before making
large edits.
