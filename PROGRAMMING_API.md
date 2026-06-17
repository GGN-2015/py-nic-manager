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
    print(adapter.name, adapter.mac, adapter.forwarding_enabled)

print("Global IPv4 forwarding:", manager.get_global_forwarding_enabled())

for route in manager.list_routes(sort_by="destination"):
    print(route.destination, route.gateway, route.interface, route.effective_metric)
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
- `get_global_forwarding_enabled()`
- `get_snapshot(concurrent=True)`
- `find_adapter(adapter)`
- `find_route(route, gateway="", interface="")`
- `suggest_loopback_value(adapters=None)`

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

Loopbacks:

- `plan_create_loopback(name=None)`
- `create_loopback(name=None, require_admin=True)`
- `plan_update_loopback(adapter, address=None, prefix_length=None, gateway="", dns_servers=None, mac="", dhcp_enabled=False)`
- `update_loopback(adapter, address=None, prefix_length=None, gateway="", dns_servers=None, mac="", dhcp_enabled=False, require_admin=True)`
- `plan_delete_loopback(adapter)`
- `delete_loopback(adapter, require_admin=True)`

Routes:

- `plan_add_route(route, gateway="", interface="", metric=None)`
- `add_route(route, gateway="", interface="", metric=None, require_admin=True)`
- `plan_update_route(old_route, new_route, old_gateway="", old_interface="", gateway="", interface="", metric=None)`
- `update_route(old_route, new_route, old_gateway="", old_interface="", gateway="", interface="", metric=None, require_admin=True)`
- `plan_delete_route(route, gateway="", interface="")`
- `delete_route(route, gateway="", interface="", require_admin=True)`

Plan execution:

- `run_plan(plan, require_admin=True)`

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
- `forwarding`
- `ipv4`
- `mac`
- `gateway`
- `dns`
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

Examples:

```python
adapters = manager.list_adapters(sort_by="forwarding", descending=True)
routes = manager.list_routes(sort_by="effective_metric")
```

IPv4 route destinations are sorted as `(address_as_32_bit_integer, prefix)`.
Numeric route metrics are sorted as integers, and text fields are sorted
case-insensitively.

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
