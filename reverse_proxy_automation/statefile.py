"""Reading and writing the one state file the app keeps.

This is deliberately dumb: it reads JSON, guarantees the shape
``{"destinations": {<plugin key>: {...}}}`` and writes it back atomically. What
goes inside a destination's slice is that destination's business, so the core can
no longer reject or rewrite another plugin's records.

The file is shared by every destination, so it is still one file to back up, and
such rules as "a state file written before destinations had their own slice used
to keep its records at the top level" are handled by the plugin that owned them.
"""

import json
import os
from pathlib import Path

from .plugins.base import STATE_ROOT


def load_state(path):
    """The state file as a dict, creating an empty one in memory when absent."""
    state_path = Path(path)
    if not state_path.exists():
        return {STATE_ROOT: {}}
    with state_path.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict):
        raise ValueError(f"Invalid sync state file: {state_path}")
    destinations = state.get(STATE_ROOT)
    if destinations is None:
        state[STATE_ROOT] = {}
    elif not isinstance(destinations, dict):
        raise ValueError(f"Invalid sync state file: {state_path}")
    return state


def save_state(path, state):
    """Write the state file atomically, so a crash cannot truncate it."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, sort_keys=True)
        state_file.write("\n")
    os.replace(temporary_path, state_path)
