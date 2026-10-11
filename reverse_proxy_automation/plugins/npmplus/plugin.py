"""NPMPlus as one destination plugin: settings, run, panel and records together.

This class is the seam the core sees. It inherits the planning and reconciling
half from :class:`~.adapter.NpmPlusDestination`, delegates the settings half to
:mod:`~.config` and the panel half to :class:`~.admin.NpmPlusAdmin`, and reads its
context from the core instead of importing the service that loads it.
"""

from . import config as config_module
from . import state as state_module
from .admin import ACTIONS, NpmPlusAdmin
from .adapter import NpmPlusDestination


class NpmPlusPlugin(NpmPlusDestination):
    """NPMPlus: the settings it owns, the hosts it reconciles and the panel it shows."""

    key = "npmplus"
    label = "NPMPlus"
    #: The panel script the core mounts under this destination's tab, in its own
    #: package. The core serves the file without reading it.
    panel = "panel.js"

    def __init__(self):
        self._context = None
        self.admin = NpmPlusAdmin(self)

    # --- the context the core lends -------------------------------------------

    def context(self, context=None):
        """Set the :class:`PluginContext` the core lends this destination."""
        if context is not None:
            self._context = context
        return self._context

    @property
    def config_store(self):
        """The settings store, so the panel can read and save without the service."""
        return self.context().config_store

    @property
    def state_path(self):
        """The one state file every destination shares."""
        return self.context().state_path

    @property
    def lock(self):
        """The run lock: held while a sync run is in progress."""
        return self.context().lock

    def wake(self):
        """Ask the core for a run, so a saved change reaches the instance soon."""
        self.context().wake()

    # --- this destination's slice of the shared state file ---------------------

    def state_records(self, state):
        """This destination's records inside the whole state file, created on use."""
        return state_module.records(state)

    def adopted_host_ids(self, state, instance):
        """Proxy host IDs on one instance that the user handed to the automation."""
        return state_module.adopted_host_ids(state, instance)

    # --- settings, as the core asks for them ----------------------------------

    def config_defaults(self):
        return config_module.config_defaults()

    def config_keys(self):
        return config_module.config_keys()

    def clean_config(self, loaded):
        return config_module.clean_config(loaded)

    def merge_config(self, existing, submitted):
        return config_module.merge_config(existing, submitted)

    def validate(self, config, allow_incomplete=False):
        return config_module.validate(config, allow_incomplete)

    def secret_fields(self, config):
        return config_module.secret_fields(config)

    def public_config(self, config, environ=None):
        return config_module.public_config(config, environ)

    # --- what the page needs to render this destination ------------------------

    def describe(self):
        """The page's view of this destination: its tab, panel and actions."""
        return {
            "key": self.key,
            "label": self.label,
            "panel": self.panel,
            "settings": list(config_module.KEYS),
            "admin": list(ACTIONS),
        }
