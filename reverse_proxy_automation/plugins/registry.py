"""Discovery and aggregation for destination plugins.

The core never imports a destination directly. It asks this module for the
destinations that are available, and asks the registry - not any one plugin -
for settings, validation, secrets and the page description. A destination that
is added later is therefore indistinguishable from a shipped one.
"""

import logging
from importlib import import_module, metadata

from .base import PluginContext, destination_runner, destination_state


LOGGER = logging.getLogger(__name__)

# A third-party destination is installed as a distribution that advertises this
# group, so adding one needs no edit to this repository:
#
#   [project.entry-points."reverse_proxy_automation.destinations"]
#   my_destination = "my_package.plugin:PLUGIN"
#
ENTRY_POINT_GROUP = "reverse_proxy_automation.destinations"

# Destinations shipped with the app. A new in-tree destination is added here and
# nowhere else: the core has no other reference to any destination's name.
BUILT_IN_PLUGINS = ("reverse_proxy_automation.plugins.npmplus",)

# The attribute a plugin module exports.
PLUGIN_ATTRIBUTE = "PLUGIN"


class PluginRegistry:
    """The destinations a run may plan for, keyed by their plugin key."""

    def __init__(self, plugins=()):
        self._plugins = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin):
        """Add one destination, refusing a missing or duplicate key."""
        key = str(getattr(plugin, "key", "") or "").strip()
        if not key:
            raise ValueError("A destination plugin must declare a non-empty key")
        if key in self._plugins:
            raise ValueError(f"Two destination plugins both use the key '{key}'")
        self._plugins[key] = plugin
        return plugin

    def get(self, key):
        return self._plugins.get(str(key or "").strip())

    def keys(self):
        return tuple(self._plugins)

    def plugins(self):
        return tuple(self._plugins.values())

    def __len__(self):
        return len(self._plugins)

    def __iter__(self):
        return iter(self._plugins.values())

    # --- settings, aggregated over every destination --------------------------

    def config_defaults(self):
        """Every destination's settings keys, with their defaults."""
        defaults = {}
        for plugin in self._plugins.values():
            defaults.update(plugin.config_defaults())
        return defaults

    def config_keys(self):
        """Every settings key owned by a destination, in registration order."""
        keys = []
        for plugin in self._plugins.values():
            for key in plugin.config_keys():
                if key not in keys:
                    keys.append(key)
        return tuple(keys)

    def clean_config(self, loaded):
        """Each destination's settings as read from a loaded file, migrated."""
        cleaned = {}
        for plugin in self._plugins.values():
            cleaned.update(plugin.clean_config(loaded))
        return cleaned

    def merge_config(self, existing, submitted):
        """The stored-plus-submitted settings fragment for every destination."""
        merged = {}
        for plugin in self._plugins.values():
            merged.update(plugin.merge_config(existing, submitted))
        return merged

    def validate(self, config, allow_incomplete=False, enabled=None):
        """Let every destination check its own settings, naming the first problem.

        ``enabled`` is an optional predicate on a plugin key: a destination that is
        switched off is skipped, because its settings may be half done.
        """
        for plugin in self._plugins.values():
            if enabled is None or enabled(plugin.key):
                plugin.validate(config, allow_incomplete=allow_incomplete)

    def secret_fields(self, config, enabled=None):
        """(description, value) for every secret any enabled destination stores."""
        fields = []
        for plugin in self._plugins.values():
            if enabled is None or enabled(plugin.key):
                fields.extend(plugin.secret_fields(config))
        return fields

    def public_config(self, config, environ=None):
        """Every destination's page-safe settings, merged."""
        published = {}
        for plugin in self._plugins.values():
            published.update(plugin.public_config(config, environ=environ))
        return published

    # --- the run and the page --------------------------------------------------

    def adapters(self, config_store, state_path, lock, wake, run=None,
                 source_snapshot=None, refresh_source=None):
        """Lend each destination its context, then return them as plan/apply adapters.

        ``run`` is the core's way of running one destination, and is bound to each
        plugin's own key here, so a plugin asks for itself without ever naming
        itself. A registry built without a service is lent none of the three and
        simply cannot run from a panel.
        """
        for plugin in self._plugins.values():
            plugin.context(PluginContext(
                config_store=config_store,
                state_path=state_path,
                lock=lock,
                wake=wake,
                registry=self,
                run=destination_runner(run, plugin.key),
                source_snapshot=source_snapshot,
                refresh_source=refresh_source,
            ))
        return self.plugins()

    def describe(self):
        """The page description for every destination."""
        return [plugin.describe() for plugin in self._plugins.values()]

    def admin(self, key):
        """The panel API of one destination, or None when it has none.

        A destination may serve its own panel actions either through an ``admin``
        attribute or by implementing ``invoke`` itself, so the core reaches them
        through this one call without knowing which shape it chose.
        """
        plugin = self.get(key)
        if plugin is None:
            return None
        admin = getattr(plugin, "admin", None)
        if admin is not None and hasattr(admin, "invoke"):
            return admin
        return plugin if hasattr(plugin, "invoke") else None


def load_plugin(module_name):
    """Import one plugin module and return the object it exports."""
    module = import_module(module_name)
    plugin = getattr(module, PLUGIN_ATTRIBUTE, None)
    if plugin is None:
        raise ValueError(
            f"Destination plugin module '{module_name}' does not export {PLUGIN_ATTRIBUTE}"
        )
    return plugin


def built_in_plugins():
    """The destinations shipped with the app."""
    return [load_plugin(module_name) for module_name in BUILT_IN_PLUGINS]


def entry_point_plugins():
    """Destinations installed alongside the app, found through their entry points.

    A package that fails to load is reported and skipped: one broken third-party
    destination must not stop the dashboard from starting.
    """
    plugins = []
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded = entry_point.load()
        except Exception as error:  # a bad third-party plugin must not be fatal
            LOGGER.warning(
                "Ignoring destination plugin '%s': it could not be loaded (%s)",
                entry_point.name,
                error,
            )
            continue
        # An entry point may name the destination object or a factory for it.
        plugins.append(loaded if hasattr(loaded, "key") else loaded())
    return plugins


def discover_plugins():
    """Every destination the app can use: the shipped ones, then the installed ones.

    A shipped destination wins a key collision, so an installed package cannot
    displace NPMPlus by reusing its key.
    """
    plugins = []
    seen = set()
    for plugin in built_in_plugins() + entry_point_plugins():
        key = str(getattr(plugin, "key", "") or "").strip()
        if not key:
            LOGGER.warning("Ignoring destination plugin %r: it declares no key", plugin)
            continue
        if key in seen:
            LOGGER.warning(
                "Ignoring destination plugin '%s': its key is already registered", key
            )
            continue
        seen.add(key)
        plugins.append(plugin)
    return plugins


def default_registry():
    """The registry the app starts with."""
    return PluginRegistry(discover_plugins())


__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_ATTRIBUTE",
    "PluginRegistry",
    "built_in_plugins",
    "default_registry",
    "destination_state",
    "discover_plugins",
    "entry_point_plugins",
    "load_plugin",
]
