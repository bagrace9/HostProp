"""Destination plugins: the contract, the registry, and the shipped destinations.

The core knows nothing about any particular destination. It reads settings,
guests and state, then hands each registered plugin its own slice to plan and
apply. Adding a destination therefore means adding a package here - or
installing a distribution that advertises an entry point in
``reverse_proxy_automation.destinations`` - and changing nothing else.
"""

from .base import (
    STATE_ROOT,
    DestinationAdmin,
    DestinationPlugin,
    PluginContext,
    destination_state,
)
from .registry import (
    ENTRY_POINT_GROUP,
    PluginRegistry,
    default_registry,
    discover_plugins,
)


__all__ = [
    "ENTRY_POINT_GROUP",
    "STATE_ROOT",
    "DestinationAdmin",
    "DestinationPlugin",
    "PluginContext",
    "PluginRegistry",
    "default_registry",
    "destination_state",
    "discover_plugins",
]
