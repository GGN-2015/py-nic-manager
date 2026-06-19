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

- View network adapters, each adapter's NIC nature, non-loopback virtual NICs,
  IPv4 addresses, MAC addresses, gateways, DNS servers,
  DHCP state, administrative enable/disable state, global and per-adapter IPv4
  router-forwarding state, and loopback status.
- Edit existing adapter IPv4 address, prefix length, gateway, DNS servers, MAC
  address, and DHCP mode where the operating system backend supports it.
- Create, edit, and delete loopback-style adapters:
  - Windows: Microsoft KM-TEST Loopback Adapter through the built-in
    `netloop.inf` driver and Windows SetupAPI.
  - Linux: dummy interfaces through `ip link`.
  - macOS and generic POSIX: loopback aliases on `lo0`.
- Create and delete NAT-capable non-loopback virtual NICs from the adapter page:
  - Windows: bundled Wintun DLLs for amd64, x86, arm, and arm64.
  - Linux: `veth` pairs through `ip link`.
  - macOS and generic POSIX: bridge interfaces through `ifconfig`.
- View, add, update, and delete IPv4 routes through a visual route table editor.
  Windows-only `Interface Metric` and `Effective Metric` columns are shown only
  on Windows, where those values are reported by the backend.
- View, add, update, and delete persistent IPv4 NAT rules. Supported NAT rules
  masquerade traffic from a source CIDR when it leaves through the selected
  outbound interface or system-selected external route.
- Enable or disable global IPv4 router forwarding on supported systems.
- Enable or disable IPv4 router forwarding for a selected adapter where the
  operating system backend supports per-interface forwarding.
- Enable or disable a selected adapter's administrative state.
- Run a modal ping test with a source IP and destination IP while streaming
  command output live in the GUI.
- Export the current adapters, virtual NICs, routes, NAT rules, and global
  forwarding state to a JSON configuration snapshot.
- Import a saved snapshot and apply it as a best-effort one-click restore after
  previewing the system commands that will run.
- Preview every mutating command before execution.
- Use the headless Python programming API for the same adapter, loopback, route,
  forwarding, virtual NIC, and snapshot operations exposed by the GUI.

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
The `tkinter` interface uses Py NIC Manager's own English modal dialogs instead
of system-localized messagebox buttons, so confirmation buttons stay in English
regardless of the desktop locale.

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

Windows loopback, TAP, and Wintun virtual NIC creation target the Windows
Net/NDIS adapter class. Before creating either adapter type, Py NIC Manager
updates the local Device Installation Restrictions policy to allow administrator
installation, enable layered allow/deny evaluation, allow the Net setup class
`{4d36e972-e325-11ce-bfc1-08002be10318}`, and remove local deny entries that
explicitly match Py NIC Manager's loopback/TAP/Wintun device IDs. If a domain or MDM
policy later reapplies a deny rule, Windows may still block installation; in
that case the helper reports a policy-specific error instead of hiding it.

Non-loopback virtual NIC creation tries bundled OpenVPN TAP-Windows6 9.27.0
first. TAP is an Ethernet-like NDIS adapter and is the preferred Windows virtual
NIC for NAT source networks. If TAP
creation fails, Py NIC Manager falls back to bundled Wintun 0.14.1. Wintun is a
layer-3 TUN adapter and is marked as not ICS-compatible because Windows ICS
often rejects it as the private/shared interface. TAP assets keep their GPLv2
license in `py_nic_manager/assets/tap-windows6/COPYRIGHT.GPL`; Wintun binaries
keep their WireGuard LLC prebuilt-binaries license in
`py_nic_manager/assets/wintun/LICENSE.txt`.
Py NIC Manager sets created TAP adapters to TAP's "Always Connected" media
mode. The adapter table marks each entry as `Physical NIC`, `Loopback`, or
`Non-loopback Virtual NIC`. The table still separates `Status` from `Admin`:
`Status` is Windows' media/link state, while `Admin` is the enable/disable state
controlled by the Enable/Disable buttons.
TAP is marked as NAT-capable, but Windows ICS acceptance is verified when a NAT
rule is applied. If ICS rejects the TAP private interface, Py NIC Manager falls
back to Windows WinNAT source-prefix NAT for the selected source CIDR.
After assigning the requested IPv4 address, virtual NIC creation verifies that
the local host can ping that address. If the check fails, creation fails instead
of treating an unusable virtual NIC as successful.
Use the virtual NIC's source CIDR as the NAT internal network. The adapter list
shows an "ICS Compatible" column so the selected internal interface is not a
guess.

Per-adapter IPv4 router forwarding uses `Get-NetIPInterface` and
`Set-NetIPInterface -Forwarding`.

Global IPv4 router forwarding uses the Windows `IPEnableRouter` registry
setting under `Tcpip\Parameters`.

Persistent NAT uses Windows RRAS NAT when that netsh context is available,
falls back to Windows WinNAT source-prefix NAT, and then falls back to Internet
Connection Sharing (ICS) through `HNetCfg.HNetShare`.
Rules take effect immediately after the command succeeds and persist through the
Windows RRAS/WinNAT/ICS configuration. You still select an outbound interface in
Py NIC Manager; the Windows backend uses that interface as the public/shared
interface and infers the private/internal interface from the source CIDR. WinNAT
uses the source CIDR directly, so a Py NIC Manager TAP virtual NIC can be used
as the internal NAT source even when Windows ICS refuses that adapter. Windows ICS
supports one public shared interface at a time, so an ICS-backed rule may replace
another ICS sharing setup. Windows ICS also requires a real private network
adapter; it cannot use a loopback adapter as the shared/private side.

### Linux

The Linux backend uses `ip` from iproute2. DNS and DHCP persistence are handled
through NetworkManager (`nmcli`) when available, with `resolvectl` used as a DNS
fallback.

Routes that use an IPv4 link-local gateway such as `169.254.x.x` are created
with `onlink` when an interface is selected. This avoids Linux rejecting valid
same-link gateways with `Nexthop has invalid gateway`.

Loopback-style adapters are implemented as Linux dummy interfaces. Linux often
reports dummy interfaces as Ethernet-like links at the link layer, so Py NIC
Manager keeps their `NIC Nature` display as `Physical NIC` when appropriate.
They are still marked internally as managed loopback-style adapters and can be
edited or removed with the loopback controls.

Non-loopback virtual NIC creation uses a `veth` pair. The primary side receives
the requested IPv4 CIDR and is intended to be the NAT internal interface; the
peer side is brought up so users can attach it to a namespace, container,
bridge, or test stack. The runtime interface is created immediately. Persist it
with your distribution's network manager if it must survive reboot.
Creation verifies local ping reachability to the assigned IPv4 address.

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

Non-loopback virtual NIC creation uses `ifconfig <bridgeN> create` and assigns
the requested IPv4 CIDR to that bridge. Existing `utun`, `tun`, `tap`, and
bridge interfaces created by VPN/Network Extension providers are also shown in
the virtual NIC list when present. These interfaces can be used as the internal
side/source network for `pf` NAT rules.
Creation verifies local ping reachability to the assigned IPv4 address.

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
Virtual NIC creation attempts portable `ifconfig <bridgeN> create` bridge
creation; unsupported systems fail clearly during the command preview/run.
Creation verifies local ping reachability to the assigned IPv4 address when the
interface is created.

## Configuration Snapshots

Exported files are JSON documents with this high-level shape:

```json
{
  "schema_version": 1,
  "platform": "Windows",
  "captured_at": "2026-06-17T02:00:00+00:00",
  "global_forwarding_enabled": false,
  "adapters": [],
  "virtual_adapters": [],
  "routes": [],
  "nat_rules": []
}
```

When applying an imported snapshot, Py NIC Manager:

1. Matches adapters by backend ID first, then by adapter name.
2. Restores the saved global IPv4 forwarding state when the backend supports it.
3. Recreates saved managed virtual NICs when the backend supports them.
4. Updates matched adapters with the saved IPv4, gateway, DNS, MAC, and DHCP
   values where supported.
5. Adds saved IPv4 routes.
6. Restores Py NIC Manager managed persistent NAT rules.
7. Shows skipped adapters and platform limitations in the command preview.

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
