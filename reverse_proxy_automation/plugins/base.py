"""The contract a destination plugin implements, and the helpers around it.

A destination is anything that can turn source-neutral guest records into
changes in another system: NPMPlus proxy hosts today, a monitoring inventory or
a DNS zone tomorrow. Everything a destination needs beyond the methods below -
its settings, its secrets, its slice of the shared state file, its panel in the
dashboard - the destination owns itself.
"""

from typing import Protocol


# Where each plugin's private records live inside the one shared state file.
STATE_ROOT = "destinations"


class DestinationPlugin(Protocol):
    """What the core requires of a destination, and what it may optionally offer."""

    #: Stable identifier. It names the plugin's settings keys, its slice of the
    #: state file and its dashboard panel, so it never changes once shipped.
    key: str
    #: Human-readable name, used for plan rows and page headings.
    label: str

    # --- settings this destination contributes ---------------------------------

    def config_defaults(self):
        """Settings keys this destination adds, with their default values."""

    def config_keys(self):
        """The settings keys the destination owns, so the core can round-trip them."""

    def clean_config(self, loaded):
        """Read this destination's settings out of a loaded file, migrating if needed."""

    def merge_config(self, existing, submitted):
        """Merge submitted settings over stored ones, keeping blank secrets."""

    def validate(self, config, allow_incomplete=False):
        """Raise ValueError naming the first problem in this destination's settings."""

    def secret_fields(self, config):
        """(description, value) for each secret this destination stores.

        Only the description reaches a log or a browser; the value never does.
        """

    def public_config(self, config, environ=None):
        """This destination's settings as they may be sent to the browser."""

    # --- the run ---------------------------------------------------------------

    def plan(self, guests, config):
        """Turn source-neutral guest records into this destination's plan."""

    def apply(self, plan, config, state, apply_changes=False):
        """Apply or preview a plan, returning (status, target, resource, details) rows.

        ``state`` is the whole shared state file; the destination reads and
        writes only ``destination_state(state, self.key)``.
        """

    # --- dashboard ---------------------------------------------------------------

    def describe(self):
        """Everything the page needs to render this destination.

        Returned keys: ``key``, ``label``, ``panel`` (the plugin's panel script,
        or "" for none), ``settings`` (the field specification) and ``admin``
        (whether the destination serves its own panel actions).
        """

    def context(self):
        """Set the :class:`PluginContext` the core lends this destination."""

    # Optional: a destination that serves its own panel actions exposes an
    # ``admin`` object holding :class:`DestinationAdmin`'s methods, or implements
    # ``invoke`` itself. The registry finds it either way; a destination with no
    # actions of its own simply declares none.


class DestinationAdmin(Protocol):
    """Optional half of a plugin: the actions its own dashboard panel calls.

    A destination exposes this as its ``admin`` attribute, or as methods on the
    plugin itself. Each action name it dispatches is named in its own
    ``describe()``, so the core can forward a call without understanding it.
    """

    def describe(self, config):
        """The panel's shape: fields, tables and buttons, as data."""

    def invoke(self, action, payload):
        """Run one named action and return a JSON-serialisable result."""


class UnknownTarget(ValueError):
    """Raised by an action asked about something the destination does not own.

    The dashboard's "test this endpoint" button sends a name, not a plugin key, so
    the core offers that name to every destination in turn. A destination that has
    never heard of the name raises this, which tells the core to ask the next one;
    any other error is a real failure and is reported as it stands.
    """


class PluginContext:
    """What the core lends a destination so it can act on the page's behalf.

    A plugin never reaches into the service object. It is handed the settings
    store, the state file path, the shared lock and a wake callable, so a
    destination can save its own settings or ask for a run without knowing how
    the core is wired.
    """

    def __init__(self, config_store, state_path, lock, wake, registry=None):
        self.config_store = config_store
        self.state_path = state_path
        self.lock = lock
        self.wake = wake
        self.registry = registry


def destination_state(state, key):
    """The slice of the shared state file that belongs to one destination.

    Every plugin used to share one flat ``managed`` map, which meant a second
    destination would have had to agree with NPMPlus about what its keys looked
    like. A plugin now gets its own dictionary under ``state["destinations"]``,
    keyed by its ``key``, and may shape the contents however it needs.
    """
    destinations = state.setdefault(STATE_ROOT, {})
    if not isinstance(destinations, dict):
        raise ValueError("Invalid sync state file: 'destinations' must be an object")
    records = destinations.setdefault(str(key), {})
    if not isinstance(records, dict):
        raise ValueError(f"Invalid sync state file: 'destinations.{key}' must be an object")
    return records
