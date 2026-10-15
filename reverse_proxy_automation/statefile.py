"""Reading and writing the one state file the app keeps.

This is deliberately dumb: it reads JSON, guarantees the shape
``{"destinations": {<plugin key>: {...}}}`` and writes it back atomically. What
goes inside a destination's slice is that destination's business, so the core can
no longer reject or rewrite another plugin's records.

The file is shared by every destination, so it is still one file to back up, and
such rules as "a state file written before destinations had their own slice used
to keep its records at the top level" are handled by the plugin that owned them.

File-level locking is applied using portalocker on a separate lock file to prevent
data corruption during concurrent writes.
"""

import json
import os
from pathlib import Path

import portalocker

from .plugins.base import STATE_ROOT


def _get_lock_path(state_path):
    """Return the path to the lock file for the given state file."""
    return state_path.with_suffix(state_path.suffix + '.lock')


def load_state(path):
    """The state file as a dict, creating an empty one in memory when absent."""
    state_path = Path(path)
    lock_path = _get_lock_path(state_path)
    # Ensure the lock file's parent directory exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        portalocker.lock(lock_file, portalocker.LOCK_EX)
        try:
            if not state_path.exists():
                return {STATE_ROOT: {}}
            with state_path.open("r", encoding="utf-8") as state_file:
                state = json.load(state_file)
        finally:
            portalocker.unlock(lock_file)
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
    lock_path = _get_lock_path(state_path)
    # Ensure the lock file's parent directory exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        portalocker.lock(lock_file, portalocker.LOCK_EX)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
            with temporary_path.open("w", encoding="utf-8") as state_file:
                json.dump(state, state_file, indent=2, sort_keys=True)
                state_file.write("\n")
            os.replace(temporary_path, state_path)
        finally:
            portalocker.unlock(lock_file)
