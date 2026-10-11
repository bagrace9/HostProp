"""NPMPlus's own records inside the shared state file.

The core used to own a flat ``managed``/``adopted`` pair, which forced every
future destination to look like NPMPlus. They are now this plugin's private
slice, ``state["destinations"]["npmplus"]``, and may be shaped however NPMPlus
needs. ``managed`` maps "instance:domain" to the proxy host ID this app
created; ``adopted`` records "instance:host_id" that the user handed over.
"""

from ..base import STATE_ROOT, destination_state


# The plugin key this slice belongs to, and the prefix on every logged message.
KEY = "npmplus"


def records(state):
    """NPMPlus's slice of the state file, created on first use.

    Accepts either the whole state file (the core's shape, carrying a
    ``destinations`` map) or the slice itself, so the helpers below can be used on
    a run's state as directly as on a loaded file.
    """
    if isinstance(state.get(STATE_ROOT), dict):
        slice_ = destination_state(state, KEY)
        # A state file written before destinations had their own slice kept the
        # records at the top level. Folding them in on the first read is what makes
        # an upgrade a non-event: the next save writes the new shape, and the file
        # is left alone until then.
        for name in ("managed", "adopted"):
            legacy = state.pop(name, None)
            if isinstance(legacy, dict):
                slice_[name] = {**legacy, **(slice_.get(name) or {})}
    else:
        slice_ = state
    for name in ("managed", "adopted"):
        value = slice_.get(name)
        if value is None:
            slice_[name] = {}
        elif not isinstance(value, dict):
            raise ValueError(
                f"Invalid sync state file: 'destinations.{KEY}.{name}' must be an object"
            )
    # A host the user adopted was not created here, so it has no ownership key of
    # its own: it is recorded by instance and NPMPlus host ID instead.
    slice_["adopted"] = {str(key): True for key in slice_["adopted"]}
    return slice_


def adopted_host_ids(state, instance):
    """NPMPlus host IDs on one instance that the user handed to the automation.

    An adopted host is updated like a host this app created, but it is never
    deleted by itself: the user asked for that entry to be kept, so only an
    explicit delete in Host management removes it.
    """
    return {
        key.partition(":")[2]
        for key in records(state)["adopted"]
        if key.partition(":")[0] == instance
    }


def ownership_of(state, instance, host_id):
    """How this app treats one host: 'managed', 'adopted' or 'unmanaged'."""
    host_id = str(host_id)
    for key, recorded in records(state)["managed"].items():
        if key.partition(":")[0] == instance and str(recorded) == host_id:
            return "managed"
    if host_id in adopted_host_ids(state, instance):
        return "adopted"
    return "unmanaged"


def adopt_host(state, instance, host_id):
    """Hand an existing NPMPlus host to the automation, so a plan may update it."""
    host_id = str(host_id)
    slice_ = records(state)
    for key, recorded in list(slice_["managed"].items()):
        if key.partition(":")[0] == instance and str(recorded) == host_id:
            slice_["managed"].pop(key, None)
    slice_["adopted"][f"{instance}:{host_id}"] = True


def forget_host(state, instance, host_id):
    """Drop every record kept for one host; releasing and deleting share this edit.

    Returns the number of state entries that were removed.
    """
    host_id = str(host_id)
    slice_ = records(state)
    removed = 0
    for key in [
        key for key, recorded in slice_["managed"].items()
        if key.partition(":")[0] == instance and str(recorded) == host_id
    ]:
        slice_["managed"].pop(key, None)
        removed += 1
    for key in [
        key for key in slice_["adopted"]
        if key.partition(":")[0] == instance and key.partition(":")[2] == host_id
    ]:
        slice_["adopted"].pop(key, None)
        removed += 1
    return removed
