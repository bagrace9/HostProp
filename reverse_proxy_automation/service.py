"""Main service facade for reverse proxy automation.
"""

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .clients import PveScriptsLocalClient
from .integrations import PslSourceAdapter
from .plugins.base import UnknownTarget
from .plugins.registry import default_registry
from .settings import resolve_secret, as_bool as _as_bool, secret_env_name, check_reference_name as _check_reference_name, plaintext_secret_fields as _plaintext_secret_fields, check_secret_references as _check_secret_fields, unresolved_secret_references as _unresolved_secret_fields
from .statefile import load_state, save_state
from .config import (
    ConfigStore,
    validate_core_config,
    validate_config,
    check_secret_references,
    normalize_config,
    plaintext_secret_fields,
    public_config,
    unresolved_secret_references,
    destination_applies,
    destination_enabled,
    secret_env_name,
    DEFAULT_CONFIG,
    FLAG_MAPS,
    _flag_map,
)
from .executor import SyncExecutor
from .sync_manager import SyncManager

LOGGER = logging.getLogger(__name__)

# The statuses a destination's rows use.
CHANGE_STATUSES = {"CREATE", "UPDATE", "DELETE", "DRY-CREATE", "DRY-UPDATE", "DRY-DELETE"}
ATTENTION_STATUSES = {"CONFLICT", "SKIP", "ERROR"}

class SyncService:
    def __init__(self, config_store, state_path, source_adapter=None,
                 destination_adapters=None, registry=None):
        self.config_store = config_store
        self.state_path = Path(state_path)
        self.registry = registry or getattr(config_store, "registry", None) or default_registry()
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        
        # Initialize executors and manager
        self.executor = SyncExecutor(self)
        self.manager = SyncManager(self)
        
        self.source_adapter = source_adapter or PslSourceAdapter(self._source_client)
        
        lent = self.registry.adapters(
            self.config_store, self.state_path, self.lock, self.wake.set,
            run=self._run_destination_for,
            source_snapshot=self.source_snapshot,
            refresh_source=self.refresh_source,
        )
        self.destination_adapters = tuple(lent if destination_adapters is None else destination_adapters)
        
        self.snapshot = None
        self.results = {}
        self.status = {
            "state": "idle",
            "message": "Configure the PSL endpoint to begin.",
            "last_check": None,
            "last_apply": None,
            "guest_count": 0,
            "timings": {},
        }
        # Load previous run metrics from state if available
        try:
            state = load_state(self.state_path)
            metrics = state.get("_last_run_metrics")
            if metrics:
                self.status.update(metrics)
        except Exception:
            LOGGER.debug("Could not load previous run metrics from state")

        self.thread = threading.Thread(target=self.manager._run_loop, name="psl-sync", daemon=True)
        self.thread.start()

    def get_status(self):
        with self.lock:
            status = dict(self.status)
            results = self._results_in_order()
            status["destinations"] = results
            status["planned"] = [
                row for result in results.values() for row in result.get("planned", [])
            ]
            status["skipped"] = [
                row for result in results.values() for row in result.get("skipped", [])
            ]
            return status

    def _results_in_order(self):
        order = [adapter.key for adapter in self.destination_adapters]
        keys = [key for key in order if key in self.results]
        keys.extend(key for key in self.results if key not in order)
        return {key: dict(self.results[key]) for key in keys}

    def _set_status(self, **updates):
        with self.lock:
            self.status.update(updates)

    def _busy(self, message):
        return {**self.get_status(), "state": "busy", "message": message}

    def _fail(self, error):
        self._set_status(
            state="error",
            message=str(error),
            last_check=datetime.now(timezone.utc).isoformat(),
            guest_count=0,
        )
        return self.get_status()

    def _run_destination_for(self, key, apply_changes=False, refresh_lxc_config=True):
        return self.run_destination(key, apply_changes, refresh_lxc_config)

    def run_once(self, apply_changes=False, refresh_lxc_config=False):
        return self.manager.run_once(apply_changes, refresh_lxc_config)

    def source_snapshot(self):
        with self.lock:
            return dict(self.snapshot) if self.snapshot else None

    def refresh_source(self, refresh_lxc_config=False, wait=True):
        return self.manager.refresh_source(refresh_lxc_config, wait)

    def run_destination(self, key, apply_changes=False, refresh_lxc_config=False, wait=True):
        adapter = next((item for item in self.destination_adapters if item.key == key), None)
        if adapter is None:
            raise ValueError(f"Unknown destination '{key}'")
        
        if not self.run_lock.acquire(blocking=wait):
            return self._busy(f"A sync run is already in progress; {adapter.label} did not run.")
        try:
            config = self.config_store.load()
            from .config import destination_enabled
            if not destination_enabled(config, key):
                return self._fail(f"{adapter.label} is switched off; turn it on to run it.")
            
            snapshot = self.source_snapshot()
            if snapshot is None or refresh_lxc_config:
                guests, psl_seconds = self.executor.read_source(config, refresh_lxc_config)
                if guests is None:
                    return self.get_status()
            else:
                guests = snapshot["guests"]
                psl_seconds = snapshot["seconds"]
            
            return self.executor.run_destinations(config, guests, [adapter], apply_changes, psl_seconds)
        except Exception as error:
            return self._fail(error)
        finally:
            self.run_lock.release()

    def auto_apply(self, config=None):
        return self.manager.auto_apply(config)

    def _validate_destination(self, config, key):
        validate_core_config(config)
        plugin = self.registry.get(key)
        if plugin is not None:
            plugin.validate(config)

    def _destination_message(self, rows, plan, apply_changes):
        mode = "Applied" if apply_changes else "Previewed"
        actionable = sum(row[0] in CHANGE_STATUSES for row in rows)
        attention = len(plan.get("skipped", ())) + sum(row[0] in ATTENTION_STATUSES for row in rows)
        return f"{mode} {actionable} change(s); {attention} item(s) need attention."

    def _record_destination_result(self, adapter, *, state, message, apply_changes,
                                   guest_count=0, rows=(), plan=None):
        now = datetime.now(timezone.utc).isoformat()
        planned = [
            {"destination": adapter.label, "status": row[0], "instance": row[1], "domain": row[2], "details": row[3]}
            for row in rows
        ]
        skipped = [
            {"name": name, "reason": reason, "destination": adapter.label}
            for name, reason in (plan or {}).get("skipped", ())
        ]
        skipped.extend([
            {"name": row[2], "reason": row[3], "instance": row[1], "destination": adapter.label, "status": row[0]}
            for row in rows if row[0] in ATTENTION_STATUSES
        ])
        with self.lock:
            previous = self.results.get(adapter.key, {})
            self.results[adapter.key] = {
                "state": state,
                "message": message,
                "last_check": now,
                "last_apply": now if apply_changes else previous.get("last_apply"),
                "guest_count": guest_count,
                "planned": planned,
                "skipped": skipped,
            }

    @staticmethod
    def _source_client(config):
        return PveScriptsLocalClient(
            config["pve_local_url"],
            config["pve_local_username"],
            resolve_secret(config["pve_local_password"]),
            verify_tls=_as_bool(config.get("pve_local_verify_tls"), True),
        )

    def destinations(self):
        return self.registry.describe()

    def invoke(self, key, action, payload=None):
        admin = self.registry.admin(key)
        if admin is None:
            raise ValueError(f"Destination '{key}' does not offer panel actions")
        return admin.invoke(action, payload if isinstance(payload, dict) else {})

    def test_connection(self, target):
        if str(target or "").strip().lower() == "pve":
            config = self.config_store.load()
            if not config.get("pve_local_url"):
                raise ValueError("Set the PVE Scripts Local URL first")
            password = config["pve_local_password"]
            from .config import check_secret_references
            check_secret_references({"pve_local_password": password})
            scripts = PveScriptsLocalClient(
                config["pve_local_url"],
                config["pve_local_username"],
                resolve_secret(password),
                verify_tls=_as_bool(config.get("pve_local_verify_tls"), True),
            )
            scripts.list_guests()
            return {"state": "ok", "message": "Connection to PVE Scripts Local successful."}
        
        adapter = next((item for item in self.destination_adapters if item.key == target), None)
        if adapter is None:
            raise UnknownTarget(target)
        
        config = self.config_store.load()
        # Only check this destination's settings
        from .config import validate_core_config
        validate_core_config(config)
        plugin = self.registry.get(adapter.key)
        if plugin:
            plugin.validate(config)
            
        return adapter.test_connection(config)

def _actionable(rows, status_key):
    """How many of these rows describe a change the run made or would make.

    A destination's own rows are tuples and the aggregate's are the dictionaries
    sent to the browser, so the caller says where the status lives.
    """
    return sum(row[status_key] in CHANGE_STATUSES for row in rows)


def _needs_attention(rows, status_key):
    """How many of these rows are a conflict, a skip or an error."""
    return sum(row[status_key] in ATTENTION_STATUSES for row in rows)


# The name the core's own secret is described by, wherever it is reported.
PSL_SECRET_DESCRIPTION = "PVE Scripts Local password"
# The one env reference the core's own secret field accepts. Each destination
# names the variables its own secrets accept.
PSL_SECRET_ENV_PATTERN = re.compile(r"^PVE_LOCAL_PASSWORD$")
# Where a reader is sent when the reference does not name that one variable.
PSL_SECRET_HINT = " (use env:PVE_LOCAL_PASSWORD)"

# The registry the core asks when its caller has none to hand. It is built once per
# process, so every destination is imported one time however often it is used.
_REGISTRY = None


def plugin_registry():
    """The destinations the app ships with, plus the ones installed beside it."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = default_registry()
    return _REGISTRY


def secret_fields(config, registry=None):
    """(description, value) for the source's password and every destination's secrets.

    The descriptions are the core's own field plus whatever each destination names,
    so a log line or a settings page can point at the field that needs setting
    without the core knowing which destination owns it.
    """
    yield PSL_SECRET_DESCRIPTION, config.get("pve_local_password")
    yield from (registry or plugin_registry()).secret_fields(
        config, enabled=lambda key: destination_enabled(config, key)
    )


def unresolved_secret_references(config, environ=None, registry=None):
    """Every (description, variable name) whose "env:" reference is not set."""
    return _unresolved_secret_fields(secret_fields(config, registry), environ)


def plaintext_secret_fields(config, registry=None):
    """Descriptions of every secret the settings file stores as a literal password.

    A field holding an "env:" reference is not listed, because the reference is
    not itself a secret. Only the human-readable description is returned, so the
    result is safe to print at startup and to send to the browser.
    """
    return _plaintext_secret_fields(secret_fields(config, registry))


def check_secret_references(config, environ=None, registry=None):
    """Raise ValueError naming every "env:" reference that resolves to nothing.

    Without this check an unset variable would look like "no password
    configured" and a run would quietly talk to the API unauthenticated.
    """
    return _check_secret_fields(secret_fields(config, registry), environ)


def check_source_secret_references(config, environ=None):
    """Raise ValueError for the source's own unresolved "env:" reference only.

    Reading PSL needs the source's credentials and nothing else, so the watcher's
    one read must not be stopped by a destination's unset variable - that is the
    destination's own problem, reported when it is used.
    """
    fields = [(PSL_SECRET_DESCRIPTION, config.get("pve_local_password"))]
    return _check_secret_fields(fields, environ)


def normalize_config(loaded, registry=None):
    """Merge a stored settings file with the defaults every destination declares.

    Only the core's own keys are listed here. Each destination reads its own keys
    out of the raw file, which is what lets an older file - one that spells a
    destination's settings the way an earlier release did - keep working without
    the core knowing what those keys were.
    """
    loaded = loaded if isinstance(loaded, dict) else {}
    config = {key: loaded.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    config.update((registry or plugin_registry()).clean_config(loaded))
    # Sent to the browser and used to build the source client, so keep it a real bool.
    config["pve_local_verify_tls"] = _as_bool(config.get("pve_local_verify_tls"), True)
    for key in ("watch_enabled", "auto_apply"):
        config[key] = _as_bool(config.get(key), False)
    for name in FLAG_MAPS:
        config[name] = _flag_map(loaded, name)
    return config


class ConfigStore:
    """The settings file, shared by the core and every destination."""

    def __init__(self, path, registry=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        # The destinations whose keys this file round-trips. Each one reads and
        # writes only its own keys, so the store never has to name any of them.
        self.registry = registry or plugin_registry()

    def load(self):
        with self.lock:
            if not self.path.exists():
                return normalize_config({}, self.registry)
            with self.path.open("r", encoding="utf-8") as config_file:
                loaded = json.load(config_file)
            return normalize_config(loaded, self.registry)

    def save(self, submitted):
        with self.lock:
            old = self.load()
            config = {**old}
            for key in DEFAULT_CONFIG:
                if key not in submitted:
                    continue
                if key in FLAG_MAPS:
                    if not isinstance(submitted[key], dict):
                        raise ValueError(f"'{key}' must be a JSON object keyed by destination")
                    config[key] = {
                        **old[key],
                        **{str(name): _as_bool(flag, True) for name, flag in submitted[key].items()},
                    }
                    continue
                value = submitted[key]
                if key.endswith("_password") or key.endswith("_secret"):
                    if value:
                        config[key] = value
                else:
                    config[key] = value
            # Each destination merges the fragment of its own keys that was submitted,
            # so one page's form cannot disturb another destination's settings - or
            # the core's.
            config.update(self.registry.merge_config(old, submitted))
            validate_config(config, allow_incomplete=True, registry=self.registry)
            parent = self.path.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
                # The settings file can hold literal credentials, so keep the
                # directory that stores it readable only by its owner.
                os.chmod(parent, 0o700)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            # Create the file with 0600 from the start so secrets are never
            # readable under a permissive umask.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
                json.dump(config, config_file, indent=2)
                config_file.write("\n")
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            return config


def validate_core_config(config, allow_incomplete=False):
    """Raise ValueError naming the first problem in the settings the core owns.

    The source's endpoint, its credentials and the switches that govern when and
    whether a run happens are the core's own vocabulary. Everything else belongs to
    a destination and is checked by :func:`validate_config`, which asks each plugin
    in turn.
    """
    _check_reference_name(
        PSL_SECRET_DESCRIPTION,
        config.get("pve_local_password"),
        PSL_SECRET_ENV_PATTERN,
        hint=PSL_SECRET_HINT,
    )
    if not isinstance(config.get("pve_local_verify_tls", True), bool):
        raise ValueError("PVE Scripts Local field 'pve_local_verify_tls' must be true or false")
    # A caller may hand this a fragment of a settings file - the tests do, and so
    # does a page saving one tab of the settings - so a switch that is absent is
    # the default it would be given when the whole file is read, not an error.
    for key in ("watch_enabled", "auto_apply"):
        if not isinstance(config.get(key, DEFAULT_CONFIG[key]), bool):
            raise ValueError(f"'{key}' must be true or false")
    for name in FLAG_MAPS:
        flags = config.get(name, {})
        if not isinstance(flags, dict) or any(not isinstance(flag, bool) for flag in flags.values()):
            raise ValueError(f"'{name}' must hold a true/false flag for each destination")
    raw_interval = config.get("poll_interval_seconds")
    if raw_interval in (None, ""):
        interval = None
        if config.get("watch_enabled"):
            raise ValueError("Set the check interval before enabling Watch PSL")
    else:
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError) as error:
            raise ValueError("Poll interval must be an integer number of seconds") from error
        if not 30 <= interval <= 86400:
            raise ValueError("Poll interval must be between 30 and 86400 seconds")
    config["poll_interval_seconds"] = interval
    if not allow_incomplete and not config.get("pve_local_url"):
        raise ValueError("Set the PVE Scripts Local URL first")


def validate_config(config, allow_incomplete=False, registry=None):
    """Raise ValueError naming the first problem in the whole settings file.

    The core checks its own keys first, then each destination checks the keys it
    owns, so a plugin's rules are enforced at save time exactly as the core's are.
    """
    validate_core_config(config, allow_incomplete)
    (registry or plugin_registry()).validate(
        config, allow_incomplete, enabled=lambda key: destination_enabled(config, key)
    )


def public_config(config, environ=None, registry=None):
    """Config as sent to the browser; no password and no secret is ever included.

    The core reports the keys it owns, then asks each destination to report the
    keys it owns, so a destination's settings reach its own panel without the core
    reading them. An "env:" reference is reported by name plus whether it
    currently resolves, so the page can tell the user which variable to set
    without ever seeing a secret value.
    """
    registry = registry or plugin_registry()
    owned = set(registry.config_keys())
    result = {
        key: value
        for key, value in config.items()
        if key not in owned and not (key.endswith("_password") or key.endswith("_secret"))
    }
    psl_password = config.get("pve_local_password")
    result["pve_local_password_configured"] = bool(resolve_secret(psl_password, environ))
    result["pve_local_password_env"] = secret_env_name(psl_password) or ""
    result["pve_local_verify_tls"] = _as_bool(config.get("pve_local_verify_tls"), True)
    result.update(registry.public_config(config, environ=environ))
    # Named, never valued: the page warns that this settings file holds
    # credentials in the clear and points at the "env:" alternative.
    result["plaintext_secrets"] = plaintext_secret_fields(config, registry)
    return result


class SyncService:
    def __init__(self, config_store, state_path, source_adapter=None,
                 destination_adapters=None, registry=None):
        self.config_store = config_store
        self.state_path = Path(state_path)
        # The destinations this run plans for. Lending every plugin its context
        # here is what lets a destination read its own settings, write its own
        # records and answer its own panel without the core wiring any of it up.
        self.registry = registry or getattr(config_store, "registry", None) or plugin_registry()
        self.source_adapter = source_adapter or PslSourceAdapter(self._source_client)
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        # Calling for the adapters is also what lends each plugin its context, so
        # it happens even when a caller supplied adapters of its own. A plugin is
        # lent the way to ask for one run of itself, never the event itself:
        # clearing a wake-up that was meant for the watcher is the core's business.
        lent = self.registry.adapters(
            self.config_store, self.state_path, self.lock, self.wake.set,
            run=self._run_destination_for,
            source_snapshot=self.source_snapshot,
            refresh_source=self.refresh_source,
        )
        self.destination_adapters = tuple(lent if destination_adapters is None else destination_adapters)
        # The one read of PSL that every destination plans against, published by
        # refresh_source() and reused by every destination until the watcher
        # refreshes it again. Held under the lock with the results below.
        self.snapshot = None
        # {destination key: what that destination last did}. The top-level status
        # is the aggregate of these, so a plugin's page can show its own rows
        # without the core knowing what any destination's rows are called.
        self.results = {}
        self.status = {
            "state": "idle",
            "message": "Configure the PSL endpoint to begin.",
            "last_check": None,
            "last_apply": None,
            "guest_count": 0,
            "timings": {},
        }
        self.thread = threading.Thread(target=self._run_loop, name="psl-sync", daemon=True)
        self.thread.start()

    def get_status(self):
        """The page's view of the last runs: the source's read plus each destination.

        The plan rows stay published at the top level, because that is what a
        result table reads, and every destination's own rows are published under
        ``destinations``, keyed by plugin key, so a plugin can show only its own
        without the core having to filter anything.
        """
        with self.lock:
            status = dict(self.status)
            results = self._results_in_order()
            status["destinations"] = results
            status["planned"] = [
                row for result in results.values() for row in result.get("planned", [])
            ]
            status["skipped"] = [
                row for result in results.values() for row in result.get("skipped", [])
            ]
            return status

    def _results_in_order(self):
        """Per-destination results, in the order the destinations are registered.

        The caller holds the lock. Rows for a destination that was removed from
        the settings file still appear, last, rather than being silently dropped.
        """
        order = [adapter.key for adapter in self.destination_adapters]
        keys = [key for key in order if key in self.results]
        keys.extend(key for key in self.results if key not in order)
        return {key: dict(self.results[key]) for key in keys}

    def _set_status(self, **updates):
        with self.lock:
            self.status.update(updates)

    def _busy(self, message):
        """The status a caller gets when another run already holds the lock."""
        return {**self.get_status(), "state": "busy", "message": message}

    def _fail(self, error):
        """Publish a failure the way a run does: a state, a message, and no rows."""
        self._set_status(
            state="error",
            message=str(error),
            last_check=datetime.now(timezone.utc).isoformat(),
            guest_count=0,
        )
        return self.get_status()

    def _run_destination_for(self, key, apply_changes=False, refresh_lxc_config=True):
        """Run one destination on its plugin's own request, bound to that plugin's key.

        This is what a plugin is lent as ``PluginContext.run``: the plugin decides
        when its own page's Preview and Apply should read PSL again, and the core
        keeps the locking, the snapshot and the status to itself.
        """
        return self.run_destination(
            key,
            apply_changes=apply_changes,
            refresh_lxc_config=refresh_lxc_config,
        )

    def run_once(self, apply_changes=False, refresh_lxc_config=False):
        # Only one preview/apply may run at a time (watcher thread vs HTTP handlers).
        # A manual run must wait for a watcher run, rather than returning the
        # watcher's status as if the requested fresh Preview/Apply had run.
        if not self.run_lock.acquire(blocking=refresh_lxc_config):
            return self._busy(
                "A sync run is already in progress; this request did not start a new preview or apply."
            )
        try:
            return self._run_once(apply_changes, refresh_lxc_config)
        finally:
            self.run_lock.release()

    def _run_once(self, apply_changes, refresh_lxc_config=False):
        """Read the source once, then run every enabled destination off that snapshot.

        This is the whole-app entry point: the HTTP shell no longer calls it, but a
        full run still reads PSL once, plans every destination that is switched on
        against the same records, and publishes one aggregate status.
        """
        try:
            config = self.config_store.load()
            validate_config(config, registry=self.registry)
            # An unresolved "env:" reference stops the run here, before any client
            # is built with an empty password.
            check_secret_references(config, registry=self.registry)
            guests, psl_seconds = self._read_source(config, refresh_lxc_config)
            if guests is None:
                return self.get_status()
            active = [
                adapter for adapter in self.destination_adapters
                if destination_enabled(config, adapter.key)
            ]
            return self._run_destinations(config, guests, active, apply_changes, psl_seconds)
        except Exception as error:
            return self._fail(error)

    def source_snapshot(self):
        """The guest records the last read produced, or None when none are held.

        Every destination plans against this one snapshot, so a single read of PSL
        serves all of them. The records are shared, so treat them as read-only.
        """
        with self.lock:
            return dict(self.snapshot) if self.snapshot else None

    def _read_source(self, config, refresh_lxc_config=False):
        """Read PSL once and publish the snapshot. Returns (guests, seconds).

        Returns (None, None) when the read failed, having published the failure the
        way a run does. A failed read drops every destination's rows: they were
        planned against records the source can no longer confirm.
        """
        self._set_status(
            state="checking",
            message=(
                "Refreshing guest configuration from Proxmox through PVE Scripts Local."
                if refresh_lxc_config
                else "Reading installed records from PVE Scripts Local."
            ),
            guest_count=0,
            timings={},
        )
        try:
            psl_started = perf_counter()
            guests = self.source_adapter.read(
                config,
                refresh_lxc_config=refresh_lxc_config,
                progress_callback=(
                    lambda current, total, script_id, container_id: self._set_status(
                        message=(
                            f"Refreshing PSL record {current} of {total} "
                            f"(record {script_id}, container {container_id}): "
                            "reading current Proxmox config and detecting the current Web UI endpoint."
                        )
                    )
                ) if refresh_lxc_config else None,
            )
            psl_seconds = perf_counter() - psl_started
        except Exception as error:
            with self.lock:
                self.snapshot = None
                self.results.clear()
            LOGGER.warning("Reading the source failed: %s", error)
            # Publish the failure the way a run does, so a page that is waiting on
            # this read sees what went wrong rather than the "checking" it set above.
            self._fail(error)
            return None, None
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.snapshot = {
                "guests": list(guests),
                "read_at": now,
                "seconds": round(psl_seconds, 3),
                "refresh_lxc_config": bool(refresh_lxc_config),
            }
        self._set_status(
            state="ok",
            message=(
                f"Read {len(guests)} guest record(s) from PSL in {psl_seconds:.2f}s; "
                "building destination plans."
            ),
            last_check=now,
            guest_count=len(guests),
            timings={"psl_read_seconds": round(psl_seconds, 3), "source_records": len(guests)},
        )
        return guests, psl_seconds

    def refresh_source(self, refresh_lxc_config=False, wait=True):
        """Read PSL once and keep the records for every destination to plan against.

        This is the whole of what the watcher does on its interval: the plugins then
        pull this snapshot when their own preview, apply or automatic run asks for
        it. Only the core's own settings are checked, because a destination that is
        half-configured must not stop the one read all the others depend on.
        """
        if not self.run_lock.acquire(blocking=wait):
            return self._busy("A sync run is already in progress; the source was not read again.")
        try:
            try:
                config = self.config_store.load()
                validate_core_config(config)
                # An unresolved "env:" reference stops the read here, before any
                # client is built with an empty password. This read is the source's
                # own, so a destination's unset variable is not this run's problem.
                check_source_secret_references(config)
            except Exception as error:
                return self._fail(error)
            self._read_source(config, refresh_lxc_config)
            return self.get_status()
        finally:
            self.run_lock.release()

    def run_destination(self, key, apply_changes=False, refresh_lxc_config=False, wait=True):
        """Preview or apply for one destination, and only that destination.

        A destination asked right now reads PSL again when it was told to or when
        nothing has been read yet, so a plugin's own page never depends on the
        watcher having run. Its rows are published under its own key, and its
        settings are the only plugin settings checked.
        """
        adapter = next((item for item in self.destination_adapters if item.key == key), None)
        if adapter is None:
            raise ValueError(f"Unknown destination '{key}'")
        if not self.run_lock.acquire(blocking=wait):
            return self._busy(f"A sync run is already in progress; {adapter.label} did not run.")
        try:
            config = self.config_store.load()
            if not destination_enabled(config, key):
                return self._fail(f"{adapter.label} is switched off; turn it on to run it.")
            snapshot = self.source_snapshot()
            if snapshot is None or refresh_lxc_config:
                guests, psl_seconds = self._read_source(config, refresh_lxc_config)
                if guests is None:
                    return self.get_status()
            else:
                guests = snapshot["guests"]
                psl_seconds = snapshot["seconds"]
            return self._run_destinations(config, guests, [adapter], apply_changes, psl_seconds)
        except Exception as error:
            return self._fail(error)
        finally:
            self.run_lock.release()

    def auto_apply(self, config=None):
        """Apply the current snapshot to every destination left to apply on its own.

        The watcher reads the source first and then calls this. Two switches have to
        agree - the master "Automatically apply" and the destination's own entry -
        so one destination can be left to be applied by hand while the rest are
        written on the watcher's interval.
        """
        if not self.run_lock.acquire(blocking=False):
            return self._busy("A sync run is already in progress; this background apply was skipped.")
        try:
            config = config if isinstance(config, dict) else self.config_store.load()
            validate_core_config(config)
            appliers = [
                adapter for adapter in self.destination_adapters
                if destination_applies(config, adapter.key)
            ]
            if not appliers:
                return self.get_status()
            snapshot = self.source_snapshot()
            if snapshot is None:
                # The watcher reads before it applies, so a gap here is a read that
                # failed: read now rather than write what the source last said.
                guests, psl_seconds = self._read_source(config, True)
                if guests is None:
                    return self.get_status()
            else:
                guests = snapshot["guests"]
                psl_seconds = snapshot["seconds"]
            return self._run_destinations(config, guests, appliers, True, psl_seconds)
        except Exception as error:
            return self._fail(error)
        finally:
            self.run_lock.release()

    def _validate_destination(self, config, key):
        """Check the core's settings and this destination's, and no other's.

        A destination that is misconfigured used to stop every other one from being
        planned at all. Only the plugin being run is asked now, so a broken plugin
        costs its own page and nothing else.
        """
        validate_core_config(config)
        plugin = self.registry.get(key)
        if plugin is not None:
            plugin.validate(config)

    def _run_destinations(self, config, guests, active, apply_changes=False, psl_seconds=0.0):
        """Plan and apply for the destinations named, recording what each one did.

        One destination failing must not stop or hide the others: its own rows are
        replaced by its error and the rest still run. A run that asked for a single
        destination raises instead, so the page that asked for it is told what went
        wrong rather than being shown an aggregate.
        """
        state = load_state(self.state_path)
        failures = []
        planning_seconds = 0.0
        destination_seconds = {}
        guest_counts = []
        self._set_status(state="checking", message="Building destination plans.")
        try:
            for adapter in active:
                try:
                    # Only the destination about to run is asked about its own
                    # settings, so one broken plugin cannot stop the others.
                    self._validate_destination(config, adapter.key)
                    planning_started = perf_counter()
                    destination_plan = adapter.plan(guests, config)
                    destination_planning = perf_counter() - planning_started
                    self._set_status(message=f"Planned {adapter.label}; applying.")
                    destination_started = perf_counter()
                    destination_rows = adapter.apply(
                        destination_plan,
                        config,
                        state,
                        apply_changes=apply_changes,
                    )
                except Exception as error:
                    if len(active) == 1:
                        raise
                    # One destination failing must not stop or hide the others.
                    LOGGER.exception("Destination %s failed", adapter.label)
                    failures.append(f"{adapter.label}: {error}")
                    self._record_destination_result(
                        adapter,
                        state="error",
                        message=str(error),
                        apply_changes=apply_changes,
                        guest_count=len(guests),
                    )
                    continue
                planning_seconds += destination_planning
                destination_seconds[adapter.key] = round(
                    perf_counter() - destination_started, 3
                )
                for row in destination_rows:
                    if row[0] == "OK":
                        continue
                    LOGGER.info(
                        "Sync result: destination=%s status=%s target=%s resource=%s",
                        adapter.label, row[0], row[1], row[2],
                    )
                guest_count = destination_plan.get("containers", len(guests))
                guest_counts.append(guest_count)
                self._record_destination_result(
                    adapter,
                    state="ok",
                    message=self._destination_message(destination_rows, destination_plan, apply_changes),
                    apply_changes=apply_changes,
                    guest_count=guest_count,
                    rows=destination_rows,
                    plan=destination_plan,
                )
        finally:
            # Persist ownership of anything already created, even if a later step failed.
            if apply_changes:
                save_state(self.state_path, state)
        now = datetime.now(timezone.utc).isoformat()
        results = self._results_in_order()
        planned = [row for result in results.values() for row in result.get("planned", [])]
        skipped = [row for result in results.values() for row in result.get("skipped", [])]
        mode = "Applied" if apply_changes else "Previewed"
        message = (
            f"{mode} {_actionable(planned, status_key='status')} change(s); "
            f"{len(skipped)} item(s) need attention."
        )
        if not active:
            message = f"Read {len(guests)} guest record(s) from PSL; no destination is enabled."
        if failures:
            message = f"{'; '.join(failures)}. {message}"
        self._set_status(
            state="error" if failures else "ok",
            message=message,
            last_check=now,
            last_apply=now if apply_changes else self.get_status().get("last_apply"),
            guest_count=max(guest_counts, default=len(guests)),
            timings={
                "psl_read_seconds": round(psl_seconds, 3),
                "planning_seconds": round(planning_seconds, 3),
                "destinations_seconds": destination_seconds,
                "source_records": len(guests),
            },
        )
        return self.get_status()

    @staticmethod
    def _destination_message(rows, plan, apply_changes):
        """One destination's own summary, counted the way the aggregate counts it.

        ``rows`` are the destination's own shape - ``(status, instance, domain,
        details)`` - because that is what the plugin hands back.
        """
        mode = "Applied" if apply_changes else "Previewed"
        actionable = _actionable(rows, status_key=0)
        attention = len(plan.get("skipped", ())) + _needs_attention(rows, status_key=0)
        return f"{mode} {actionable} change(s); {attention} item(s) need attention."

    def _record_destination_result(self, adapter, *, state, message, apply_changes,
                                   guest_count=0, rows=(), plan=None):
        """Store what one destination last did, for its own page and the results table.

        A destination's rows are its own: the core keeps them under the destination's
        key without knowing what a row means, which is what lets each plugin render
        its own results and nothing else's. Each row is named with the destination it
        came from as it is kept, so one table can show every destination's rows
        without any plugin having to know it is not the only one on the page.
        """
        now = datetime.now(timezone.utc).isoformat()
        planned = [
            {
                "destination": adapter.label,
                "status": row[0],
                "instance": row[1],
                "domain": row[2],
                "details": row[3],
            }
            for row in rows
        ]
        skipped = [
            {"name": name, "reason": reason, "destination": adapter.label}
            for name, reason in (plan or {}).get("skipped", ())
        ]
        skipped.extend(
            {
                "name": row[2],
                "reason": row[3],
                "instance": row[1],
                "destination": adapter.label,
                "status": row[0],
            }
            for row in rows if row[0] in ATTENTION_STATUSES
        )
        with self.lock:
            previous = self.results.get(adapter.key, {})
            self.results[adapter.key] = {
                "state": state,
                "message": message,
                "last_check": now,
                "last_apply": now if apply_changes else previous.get("last_apply"),
                "guest_count": guest_count,
                "planned": planned,
                "skipped": skipped,
            }

    @staticmethod
    def _source_client(config):
        """Build PSL's client without exposing source-specific setup to destinations."""
        return PveScriptsLocalClient(
            config["pve_local_url"],
            config["pve_local_username"],
            resolve_secret(config["pve_local_password"]),
            verify_tls=_as_bool(config.get("pve_local_verify_tls"), True),
        )

    # --- destinations, one plugin at a time -----------------------------------

    def destinations(self):
        """What the page needs to render every destination: key, label, panel."""
        return self.registry.describe()

    def invoke(self, key, action, payload=None):
        """Run one destination's own panel action, naming only the destination.

        The core forwards the action and its payload without reading either, so a
        destination's vocabulary - "instance", "proxy host ID", "ownership" - never
        has to appear here. A destination that offers no panel actions is refused
        by name rather than by a missing method.
        """
        admin = self.registry.admin(key)
        if admin is None:
            raise ValueError(f"Destination '{key}' does not offer panel actions")
        return admin.invoke(action, payload if isinstance(payload, dict) else {})

    def test_connection(self, target):
        """Check the source's credentials, or whichever destination owns the name.

        The page sends a name rather than a plugin key, so every destination that
        offers panel actions is asked in turn. One that has never heard of the name
        raises UnknownTarget and the next one is asked; any other failure is a real
        one and is reported as it stands.
        """
        if str(target or "").strip().lower() == "pve":
            config = self.config_store.load()
            if not config.get("pve_local_url"):
                raise ValueError("Set the PVE Scripts Local URL first")
            password = config["pve_local_password"]
            check_secret_references({"pve_local_password": password})
            scripts = PveScriptsLocalClient(
                config["pve_local_url"],
                config["pve_local_username"],
                resolve_secret(password),
                verify_tls=_as_bool(config.get("pve_local_verify_tls"), True),
            ).installed_scripts()
            return {
                "ok": True,
                "message": f"PVE Scripts Local reachable: {len(scripts)} installed record(s).",
            }
        for plugin in self.registry.plugins():
            admin = self.registry.admin(plugin.key)
            if admin is None:
                continue
            try:
                return admin.invoke("test_connection", {"target": target})
            except UnknownTarget:
                continue
        raise ValueError(f"Unknown test target '{target}'")


    @contextmanager
    def _state_lock(self):
        """Hold the run lock, so a manual state change cannot race a sync run."""
        if not self.run_lock.acquire(blocking=False):
            raise ValueError("A sync run is in progress; try again once it finishes")
        try:
            yield
        finally:
            self.run_lock.release()

    def wake_loop(self):
        self.wake.set()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        self.thread.join(timeout=2)

    def _run_loop(self):
        first_iteration = True
        interval = 30
        while not self.stop_event.is_set():
            if not first_iteration:
                self.wake.wait(interval)
                self.wake.clear()
            if self.stop_event.is_set():
                break
            first_iteration = False
            try:
                config = self.config_store.load()
                configured_interval = config.get("poll_interval_seconds")
                interval = 30 if configured_interval is None else max(30, int(configured_interval))
                if config.get("watch_enabled"):
                    # The watcher's own job is the source: one read that every
                    # destination plans against. Whether a destination is written is
                    # decided afterwards, per destination, by its own switch.
                    self.refresh_source(refresh_lxc_config=True)
                    self.auto_apply(config)
            except Exception as error:
                LOGGER.exception("Background watcher iteration failed")
                self._set_status(
                    state="error",
                    message=f"Background watcher failed and will retry: {error}",
                    last_check=datetime.now(timezone.utc).isoformat(),
                )
                interval = 30