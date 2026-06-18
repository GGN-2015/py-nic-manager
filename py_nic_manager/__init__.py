"""Cross-platform GUI network adapter manager."""

from .api import NetworkManager, PrivilegeError
from .backends import BackendError
from .models import (
    AdapterInfo,
    AddressInfo,
    CommandResult,
    NatRule,
    NetworkSnapshot,
    OperationPlan,
    RouteInfo,
    VirtualAdapterInfo,
)

__all__ = [
    "AdapterInfo",
    "AddressInfo",
    "BackendError",
    "CommandResult",
    "NetworkManager",
    "NetworkSnapshot",
    "NatRule",
    "OperationPlan",
    "PrivilegeError",
    "RouteInfo",
    "VirtualAdapterInfo",
    "__version__",
]

__version__ = "0.1.33"
