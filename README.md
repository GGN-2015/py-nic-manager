# Py NIC Manager

Py NIC Manager is a cross-platform Python GUI for viewing and changing network
adapter settings, loopback-style adapters, route tables, and saved network
configuration snapshots.

The application is written in English. Windows uses a modern PyQt6 interface
with an automatic light/dark theme. Linux, macOS, and other POSIX systems use
the `tkinter` interface by default to avoid Qt platform-plugin and `sudo`
desktop-session issues. It can run on Windows and POSIX systems.
Administrative actions require Administrator/root privileges; when the app is
started without those privileges, it opens in read-only mode and clearly asks
the user to restart it with elevated permissions.

The package includes JetBrains Mono for the `tkinter` fallback interface so
Linux systems do not depend on rough default Tk fonts. JetBrains Mono is
distributed under the SIL Open Font License; the license text is bundled in the
package under `py_nic_manager/assets/fonts/JetBrainsMono-OFL.txt`.

## Features

- View network adapters, IPv4 addresses, MAC addresses, gateways, DNS servers,
  DHCP state, global and per-adapter IPv4 router-forwarding state, and
  loopback status.
- Edit existing adapter IPv4 address, prefix length, gateway, DNS servers, MAC
  address, and DHCP mode where the operating system backend supports it.
- Create, edit, and delete loopback-style adapters:
  - Windows: Microsoft KM-TEST Loopback Adapter through the built-in
    `netloop.inf` driver and Windows SetupAPI.
  - Linux: dummy interfaces through `ip link`.
  - macOS and generic POSIX: loopback aliases on `lo0`.
- View, add, update, and delete IPv4 routes through a visual route table editor.
- View, add, update, and delete persistent IPv4 NAT rules. Supported NAT rules
  masquerade traffic from a source CIDR when it leaves through the selected
  outbound interface or system-selected external route.
- Enable or disable global IPv4 router forwarding on supported systems.
- Enable or disable IPv4 router forwarding for a selected adapter where the
  operating system backend supports per-interface forwarding.
- Export the current adapters, routes, NAT rules, and global forwarding state
  to a JSON configuration snapshot.
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
detection. PyQt6 is installed only on Windows for the default Windows GUI.

## Running

```bash
py-nic-manager
```

Or:

```bash
python -m py_nic_manager
```

By default, the launcher uses the PyQt6 interface on Windows and the `tkinter`
interface on Linux, macOS, and other POSIX systems. On Windows, if Qt cannot
start, the launcher automatically falls back to `tkinter`.

You can force a GUI backend with:

```bash
PY_NIC_MANAGER_GUI=qt py-nic-manager
PY_NIC_MANAGER_GUI=tk py-nic-manager
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
by the local machine. Changing the global IPv4 forwarding setting may require a
restart before the setting is fully active, and the GUI asks whether to restart
immediately after a successful change.

### Windows

The Windows backend uses PowerShell networking cmdlets, `netsh`, `route`, and
Windows SetupAPI/NewDev calls through `ctypes`.

Creating a Microsoft KM-TEST Loopback Adapter uses the built-in
`%WINDIR%\inf\netloop.inf` driver directly. It does not require `devcon.exe` or
the Windows Driver Kit.

Per-adapter IPv4 router forwarding uses `Get-NetIPInterface` and
`Set-NetIPInterface -Forwarding`.

Global IPv4 router forwarding uses the Windows `IPEnableRouter` registry
setting under `Tcpip\Parameters`.

Persistent NAT uses Windows RRAS NAT when that netsh context is available and
falls back to Internet Connection Sharing (ICS) through `HNetCfg.HNetShare`.
Rules take effect immediately after the command succeeds and persist through the
Windows RRAS/ICS configuration. You still select an outbound interface in Py NIC
Manager; the Windows backend uses that interface as the public/shared interface
and infers the private/internal interface from the source CIDR. Windows ICS
supports one public shared interface at a time, so an ICS-backed rule may replace
another ICS sharing setup.

### Linux

The Linux backend uses `ip` from iproute2. DNS and DHCP persistence are handled
through NetworkManager (`nmcli`) when available, with `resolvectl` used as a DNS
fallback.

Routes that use an IPv4 link-local gateway such as `169.254.x.x` are created
with `onlink` when an interface is selected. This avoids Linux rejecting valid
same-link gateways with `Nexthop has invalid gateway`.

Loopback-style adapters are implemented as Linux dummy interfaces.

Per-adapter IPv4 router forwarding uses
`net.ipv4.conf.<interface>.forwarding`.

Global IPv4 router forwarding uses `net.ipv4.ip_forward`.

Persistent NAT uses iptables MASQUERADE rules with Py NIC Manager's own
configuration in `/etc/py-nic-manager/nat-rules.json` plus a systemd service
that reapplies the rules during boot. Creating, updating, or deleting a NAT rule
updates the persistent configuration and immediately reapplies the runtime NAT
table. If systemd or iptables is unavailable, the operation fails instead of
pretending to be persistent.

### macOS

The macOS backend uses `networksetup`, `ifconfig`, `route`, and `netstat`.
Loopback creation is implemented as an address alias on `lo0`, because macOS
does not create independent loopback NICs in the same way Linux creates dummy
interfaces.

macOS has a global IPv4 forwarding switch rather than the same per-interface
switch exposed by Windows and Linux. Py NIC Manager enables global forwarding
when needed and uses a `pf` anchor to block forwarded IPv4 packets received on
interfaces that are disabled in the UI.

Persistent NAT uses a Py NIC Manager `pf` anchor and updates `/etc/pf.conf`
when needed so the rules are loaded after reboot. Creating, updating, or
deleting a NAT rule rewrites the anchor and immediately reloads `pf`; no reboot
is required.

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
  "global_forwarding_enabled": false,
  "adapters": [],
  "routes": [],
  "nat_rules": []
}
```

When applying an imported snapshot, Py NIC Manager:

1. Matches adapters by backend ID first, then by adapter name.
2. Restores the saved global IPv4 forwarding state when the backend supports it.
3. Updates matched adapters with the saved IPv4, gateway, DNS, MAC, and DHCP
   values where supported.
4. Adds saved IPv4 routes.
5. Restores Py NIC Manager managed persistent NAT rules.
6. Shows skipped adapters and platform limitations in the command preview.

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
