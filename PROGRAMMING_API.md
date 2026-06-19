# Py NIC Manager Programming API

Py NIC Manager exposes a headless Python API in `py_nic_manager.api`. The API
uses the same operating-system backends as the GUI, so anything the GUI can
view, preview, or change can also be driven from Python code.

The API is intentionally plan-first. Methods named `plan_*` return an
`OperationPlan` without changing the system. The matching methods without the
`plan_` prefix run those commands and return `CommandResult` objects.

Mutating calls require Administrator/root privileges unless you create the
manager with `dry_run=True` or pass `require_admin=False`.

## Quick Start

```python
from py_nic_manager import NetworkManager

manager = NetworkManager()

for adapter in manager.list_adapters(sort_by="name"):
    print(adapter.name, adapter.nature, adapter.mac, adapter.admin_enabled, adapter.forwarding_enabled)

print("Global IPv4 forwarding:", manager.get_global_forwarding_enabled())

for route in manager.list_routes(sort_by="destination"):
    print(route.destination, route.gateway, route.interface, route.metric)

for rule in manager.list_nat_rules(sort_by="source_cidr"):
    print(rule.name, rule.source_cidr, rule.outbound_interface)

for virtual in manager.list_virtual_adapters():
    print(virtual.name, virtual.kind, virtual.address, virtual.source_cidr)
```

Preview a change before running it:

```python
manager = NetworkManager(dry_run=True)
plan = manager.plan_add_route(
    "203.0.113.0/24",
    gateway="192.0.2.1",
    interface="Ethernet",
    metric=20,
)

print(plan.as_text())
results = manager.run_plan(plan)
```

## Core Objects

The main entry point is:

```python
from py_nic_manager import NetworkManager
```

Common data classes are also exported from `py_nic_manager`:

- `AdapterInfo`
- `AddressInfo`
- `RouteInfo`
- `NatRule`
- `VirtualAdapterInfo`
- `NetworkSnapshot`
- `OperationPlan`
- `CommandResult`
- `PrivilegeError`

These classes support normal dataclass-style construction. Most also provide
`to_dict()` and `from_dict()` helpers for JSON-friendly data.

## Manager Construction

```python
manager = NetworkManager()
```

Options:

- `NetworkManager(backend=None)` uses the current platform backend.
- `NetworkManager(dry_run=True)` generates commands but does not execute them.
- `NetworkManager(admin_checker=callable)` is useful for tests or embedding.

Useful properties:

- `manager.backend_name`
- `manager.dry_run`
- `manager.is_admin`

## Method Reference

Viewing and lookup:

- `list_adapters(sort_by=None, descending=False)`
- `list_routes(sort_by=None, descending=False)`
- `list_nat_rules(sort_by=None, descending=False)`
- `list_virtual_adapters()`
- `get_global_forwarding_enabled()`
- `get_snapshot(concurrent=True)`
- `find_adapter(adapter)`
- `find_route(route, gateway="", interface="")`
- `find_nat_rule(rule)`
- `suggest_loopback_value(adapters=None)`
- `suggest_virtual_adapter_value(adapters=None)`

Snapshots:

- `export_snapshot(path, snapshot=None)`
- `import_snapshot(path)`
- `plan_apply_snapshot(snapshot, allow_platform_mismatch=False)`
- `apply_snapshot(snapshot, allow_platform_mismatch=False, require_admin=True)`

Adapters:

- `plan_update_adapter(adapter, address=None, prefix_length=None, gateway="", dns_servers=None, mac="", dhcp_enabled=False)`
- `update_adapter(adapter, address=None, prefix_length=None, gateway="", dns_servers=None, mac="", dhcp_enabled=False, require_admin=True)`
- `plan_set_global_forwarding(enabled)`
- `set_global_forwarding(enabled, require_admin=True)`
- `plan_set_adapter_forwarding(adapter, enabled)`
- `set_adapter_forwarding(adapter, enabled, require_admin=True)`
- `plan_set_adapter_admin(adapter, enabled)`
- `set_adapter_admin(adapter, enabled, require_admin=True)`

Loopbacks:

- `plan_create_loopback(name=None)`
- `create_loopback(name=None, require_admin=True)`
- `plan_update_loopback(adapter, address=None, prefix_length=None, gateway="", dns_servers=None, mac="", dhcp_enabled=False)`
- `update_loopback(adapter, address=None, prefix_length=None, gateway="", dns_servers=None, mac="", dhcp_enabled=False, require_admin=True)`
- `plan_delete_loopback(adapter)`
- `delete_loopback(adapter, require_admin=True)`

Virtual NICs:

- `plan_create_virtual_adapter(name=None, address=None, prefix_length=None)`
- `create_virtual_adapter(name=None, address=None, prefix_length=None, require_admin=True)`
- `find_virtual_adapter(adapter)`
- `plan_delete_virtual_adapter(adapter)`
- `delete_virtual_adapter(adapter, require_admin=True)`

Routes:

- `plan_add_route(route, gateway="", interface="", metric=None)`
- `add_route(route, gateway="", interface="", metric=None, require_admin=True)`
- `plan_update_route(old_route, new_route, old_gateway="", old_interface="", gateway="", interface="", metric=None)`
- `update_route(old_route, new_route, old_gateway="", old_interface="", gateway="", interface="", metric=None, require_admin=True)`
- `plan_delete_route(route, gateway="", interface="")`
- `delete_route(route, gateway="", interface="", require_admin=True)`

NAT:

- `plan_create_nat_rule(name, source_cidr, outbound_interface="", enabled=True)`
- `create_nat_rule(name, source_cidr, outbound_interface="", enabled=True, require_admin=True)`
- `plan_update_nat_rule(old_rule, name, source_cidr, outbound_interface="", enabled=True)`
- `update_nat_rule(old_rule, name, source_cidr, outbound_interface="", enabled=True, require_admin=True)`
- `plan_delete_nat_rule(rule)`
- `delete_nat_rule(rule, require_admin=True)`

Plan execution:

- `run_plan(plan, require_admin=True)`
- `plan_restart_system()`
- `restart_system(require_admin=True)`

## GUI Feature Coverage

Every GUI operation has a headless equivalent:

| GUI capability | Headless API |
| --- | --- |
| Refresh/view adapters | `list_adapters()`, `get_snapshot()` |
| Refresh/view routes | `list_routes()`, `get_snapshot()` |
| Refresh/view NAT rules | `list_nat_rules()`, `get_snapshot()` |
| Refresh/view virtual NICs | `list_virtual_adapters()`, `get_snapshot()` |
| View global IPv4 forwarding | `get_global_forwarding_enabled()`, `get_snapshot()` |
| Sort adapter, route, and NAT tables | `list_adapters(sort_by=...)`, `list_routes(sort_by=...)`, `list_nat_rules(sort_by=...)` |
| Edit adapter IP, MAC, gateway, DNS, DHCP | `plan_update_adapter()`, `update_adapter()` |
| Edit loopback configuration | `plan_update_loopback()`, `update_loopback()` |
| Create loopback | `plan_create_loopback()`, `create_loopback()` |
| Delete loopback | `plan_delete_loopback()`, `delete_loopback()` |
| Create virtual NIC | `plan_create_virtual_adapter()`, `create_virtual_adapter()` |
| Delete virtual NIC | `plan_delete_virtual_adapter()`, `delete_virtual_adapter()` |
| Set per-adapter IPv4 forwarding | `plan_set_adapter_forwarding()`, `set_adapter_forwarding()` |
| Enable or disable an adapter | `plan_set_adapter_admin()`, `set_adapter_admin()` |
| Set global IPv4 forwarding | `plan_set_global_forwarding()`, `set_global_forwarding()` |
| Restart after a restart-required plan | `plan_restart_system()`, `restart_system()` |
| Add route | `plan_add_route()`, `add_route()` |
| Update route | `plan_update_route()`, `update_route()` |
| Delete route | `plan_delete_route()`, `delete_route()` |
| Add NAT rule | `plan_create_nat_rule()`, `create_nat_rule()` |
| Update NAT rule | `plan_update_nat_rule()`, `update_nat_rule()` |
| Delete NAT rule | `plan_delete_nat_rule()`, `delete_nat_rule()` |
| Export configuration | `export_snapshot()` |
| Import configuration | `import_snapshot()` |
| Apply imported configuration | `plan_apply_snapshot()`, `apply_snapshot()` |
| Preview command plan | Any `plan_*` method plus `OperationPlan.as_text()` |
| Execute confirmed plan | `run_plan()` |

## Viewing Adapters And Routes

```python
adapters = manager.list_adapters()
routes = manager.list_routes()
```

Sorting uses the same typed behavior as the GUI.

Adapter sort columns:

- `index`
- `name`
- `status`
- `admin`
- `forwarding`
- `ics`
- `ipv4`
- `mac`
- `gateway`
- `dns`
- `nature`
- `kind`

Route sort columns:

- `destination`
- `gateway`
- `interface`
- `route_metric`
- `interface_metric`
- `effective_metric`
- `protocol`
- `table`

`interface_metric` and `effective_metric` are populated by the Windows backend.
The GUI shows those two columns only on Windows; they remain part of the API and
snapshot schema so Windows route state can round-trip cleanly.

NAT sort columns:

- `name`
- `source_cidr`
- `outbound_interface`
- `enabled`
- `persistent`
- `managed`

Examples:

```python
adapters = manager.list_adapters(sort_by="forwarding", descending=True)
routes = manager.list_routes(sort_by="route_metric")
```

IPv4 route destinations are sorted as `(address_as_32_bit_integer, prefix)`.
Numeric route metrics are sorted as integers, and text fields are sorted
case-insensitively.

NAT source CIDRs use the same network sort behavior as route destinations.

## Snapshots

Export the current system state:

```python
manager.export_snapshot("network-good.json")
```

Import a snapshot:

```python
snapshot = manager.import_snapshot("network-good.json")
```

Preview or apply a snapshot:

```python
plan = manager.plan_apply_snapshot(snapshot)
print(plan.as_text())

results = manager.apply_snapshot(snapshot)
```

Applying a snapshot from a different backend is blocked by default:

```python
manager.apply_snapshot(snapshot, allow_platform_mismatch=True)
```

## Adapter Operations

Find an adapter by index, ID, or name:

```python
adapter = manager.find_adapter("Ethernet")
adapter = manager.find_adapter(0)
```

Preview or update adapter configuration:

```python
plan = manager.plan_update_adapter(
    "Ethernet",
    address="192.0.2.50/24",
    gateway="192.0.2.1",
    dns_servers=["1.1.1.1", "8.8.8.8"],
    mac="00:11:22:33:44:66",
    dhcp_enabled=False,
)

results = manager.update_adapter(
    "Ethernet",
    address="192.0.2.50/24",
    gateway="192.0.2.1",
    dns_servers="1.1.1.1, 8.8.8.8",
)
```

Enable DHCP:

```python
manager.update_adapter("Ethernet", dhcp_enabled=True)
```

## Adapter Forwarding

View global and per-adapter forwarding state:

```python
print(manager.get_global_forwarding_enabled())

for adapter in manager.list_adapters():
    print(adapter.name, adapter.forwarding_enabled)
```

Preview or set global IPv4 router forwarding:

```python
plan = manager.plan_set_global_forwarding(True)
print(plan.restart_required)
results = manager.set_global_forwarding(True)
```

Preview or set per-adapter IPv4 router forwarding:

```python
plan = manager.plan_set_adapter_forwarding("Ethernet", False)
results = manager.set_adapter_forwarding("Ethernet", False)
```

Preview or change the adapter administrative state:

```python
plan = manager.plan_set_adapter_admin("py-virtual0", True)
results = manager.set_adapter_admin("py-virtual0", True)
manager.set_adapter_admin("py-virtual0", False)
```

`AdapterInfo.status` reports the operating system's link or media state.
`AdapterInfo.admin_enabled` reports the administrative enable/disable state.
For TAP-style virtual NICs those can differ: Windows may report media
`Disconnected` when a TAP adapter is enabled but no TAP application has opened
the device.

Platform behavior is the same as the GUI: Windows and Linux use native
global and per-interface controls where the operating system exposes them.
macOS uses the global IPv4 forwarding switch plus Py NIC Manager's `pf` rules
to block forwarded packets received on disabled interfaces.

Plans that change global IPv4 forwarding set `OperationPlan.restart_required`
to `True` because the operating system may need a restart before the global
router setting is fully active.

## Loopback Operations

Get a non-conflicting recommended loopback value:

```python
name = manager.suggest_loopback_value()
```

Preview or create a loopback-style adapter:

```python
plan = manager.plan_create_loopback("py-loopback1")
results = manager.create_loopback("py-loopback1")
```

If no name is supplied, the API uses the same smart suggestion logic as the
GUI:

```python
manager.create_loopback()
```

Update loopback configuration:

```python
manager.update_loopback("py-loopback1", address="192.0.2.60/24")
```

Delete a loopback-style adapter:

```python
manager.delete_loopback("py-loopback1")
```

The exact backend behavior matches the GUI:

- Windows creates Microsoft KM-TEST Loopback Adapters.
- Linux creates dummy interfaces.
- macOS and generic POSIX create aliases on `lo0`.

## Virtual NIC Operations

Get a non-conflicting recommended virtual NIC name:

```python
name = manager.suggest_virtual_adapter_value()
```

Preview or create a non-loopback virtual NIC:

```python
plan = manager.plan_create_virtual_adapter(
    "py-virtual0",
    address="192.168.56.1/24",
)
print(plan.as_text())
results = manager.create_virtual_adapter(
    "py-virtual0",
    address="192.168.56.1/24",
)
```

Creation is considered successful only after the backend verifies that the local
host can ping the assigned IPv4 address. On Windows this check happens inside
the virtual NIC helper and a failed TAP creation is cleaned up before returning.
On POSIX backends the create plan includes a final `ping` command and plan
execution stops at the first failed command.

List virtual NICs and use their source CIDR for NAT:

```python
for adapter in manager.list_virtual_adapters():
    print(adapter.name, adapter.kind, adapter.address, adapter.source_cidr)
```

Delete a managed virtual NIC:

```python
manager.delete_virtual_adapter("py-virtual0")
```

Platform behavior matches the GUI:

- Windows tries the bundled TAP-Windows6 assets first because TAP is
  Ethernet-like and preferred for NAT source networks, then falls back to the
  bundled Wintun DLLs. Both paths create non-loopback, non-Hyper-V virtual NICs.
- Linux creates a `veth` pair and assigns the requested IPv4 CIDR to the
  primary side. Attach the peer side to a namespace, container, bridge, or test
  stack as needed.
- macOS creates a bridge interface with `ifconfig <bridgeN> create` and assigns
  the requested IPv4 CIDR. Existing `utun`, `tun`, `tap`, and bridge interfaces
  are also listed.
- Generic POSIX attempts portable bridge creation with `ifconfig`; unsupported
  systems fail clearly.

## Route Operations

Add a route:

```python
plan = manager.plan_add_route(
    "203.0.113.0/24",
    gateway="192.0.2.1",
    interface="Ethernet",
    metric=20,
)
results = manager.add_route(
    "203.0.113.0/24",
    gateway="192.0.2.1",
    interface="Ethernet",
    metric=20,
)
```

On Linux, IPv4 link-local gateways such as `169.254.x.x` are automatically
planned with `onlink` when an interface is supplied. That keeps API behavior in
line with the GUI and avoids `Nexthop has invalid gateway` for same-link
gateways.

Update a route:

```python
manager.update_route(
    "198.51.100.0/24",
    "203.0.113.0/24",
    old_gateway="192.0.2.1",
    old_interface="Ethernet",
    gateway="192.0.2.1",
    interface="Ethernet",
    metric=20,
)
```

Delete a route:

```python
manager.delete_route(
    "203.0.113.0/24",
    gateway="192.0.2.1",
    interface="Ethernet",
)
```

If a route selector matches more than one route, the API raises `LookupError`.
Pass `gateway` and `interface` to make the selector unambiguous.

## NAT Operations

View NAT rules:

```python
for rule in manager.list_nat_rules():
    print(rule.name, rule.source_cidr, rule.outbound_interface, rule.persistent)
```

Create or update a persistent NAT rule:

```python
plan = manager.plan_create_nat_rule(
    "lab-nat",
    "192.168.56.0/24",
    outbound_interface="Ethernet",
)
print(plan.as_text())
results = manager.create_nat_rule(
    "lab-nat",
    "192.168.56.0/24",
    outbound_interface="Ethernet",
)
```

The `outbound_interface` argument is always the outbound/public interface name
from the user's point of view. On Windows, Py NIC Manager uses RRAS NAT when
available, falls back to WinNAT source-prefix NAT, and then falls back to
Internet Connection Sharing (ICS). The source CIDR is used to infer the
private/internal interface, while `outbound_interface` selects the public/shared
interface. WinNAT uses the source CIDR directly, so a Py NIC Manager TAP virtual
NIC can remain the internal NAT source even when Windows ICS rejects that
adapter. Windows ICS supports one public shared interface at a time, so an
ICS-backed rule may replace another ICS sharing setup. Windows ICS also requires
a real private network adapter; it cannot use a loopback adapter as the
shared/private side.

For Windows NAT, a virtual NIC created with
`create_virtual_adapter("py-virtual0", address="192.168.56.1/24")` exposes
`192.168.56.0/24` as the internal source CIDR. Use that CIDR when creating the
NAT rule.

Delete a NAT rule:

```python
manager.delete_nat_rule("lab-nat")
```

On supported platforms, NAT create/update/delete operations are immediate and
persistent after the command plan succeeds. Windows uses persistent RRAS/ICS
configuration plus Py NIC Manager metadata under `ProgramData`. Linux writes
`/etc/py-nic-manager/nat-rules.json`, reapplies iptables MASQUERADE rules
immediately, and installs a systemd boot service. macOS writes a persistent
`pf` anchor and reloads `pf` immediately. If the backend cannot make the rule
persistent, the command fails rather than reporting success.

Rules discovered at runtime but not managed by Py NIC Manager may be shown with
`managed=False`; snapshot apply does not delete those external rules.

## Running Plans

Every mutating operation can be previewed:

```python
plan = manager.plan_create_loopback("py-loopback2")
print(plan.title)
print(plan.notes)
print(plan.commands)
print(plan.as_text())
```

Run a plan:

```python
results = manager.run_plan(plan)
for result in results:
    print(result.ok, result.returncode)
    print(result.summary())
```

`CommandResult.ok` is true when the command returned exit code `0`.

Restart the host after a restart-required plan:

```python
plan = manager.plan_set_global_forwarding(True)
results = manager.run_plan(plan)
if all(result.ok for result in results) and plan.restart_required:
    manager.restart_system()
```

## Error Handling

Common exceptions:

- `PrivilegeError`: a real mutating command was requested without elevation.
- `LookupError`: an adapter or route selector did not match, or matched more
  than one route.
- `ValueError`: invalid IP, network, prefix, MAC, or snapshot/platform input.
- `BackendError`: the selected platform backend cannot perform the requested
  operation or an operating-system command failed during discovery.

Example:

```python
from py_nic_manager import NetworkManager, PrivilegeError

manager = NetworkManager()

try:
    manager.create_loopback("py-loopback2")
except PrivilegeError:
    print("Restart the script as Administrator/root.")
```

## Testing And Dry Runs

Use `dry_run=True` to inspect plans or test automation without changing the
host network:

```python
manager = NetworkManager(dry_run=True)
results = manager.add_route("203.0.113.0/24", gateway="192.0.2.1")
assert all(result.ok for result in results)
```

Dry-run command results are synthetic success results, and no system command is
executed.
