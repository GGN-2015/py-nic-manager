"""Cross-platform GUI network adapter manager."""

from .api import NetworkManager, PrivilegeError
from .backends import BackendError
from .models import AdapterInfo, AddressInfo, CommandResult, NetworkSnapshot, OperationPlan, RouteInfo

__all__ = [
    "AdapterInfo",
    "AddressInfo",
    "BackendError",
    "CommandResult",
    "NetworkManager",
    "NetworkSnapshot",
    "OperationPlan",
    "PrivilegeError",
    "RouteInfo",
    "__version__",
]

__version__ = "0.1.14"
