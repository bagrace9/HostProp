"""Settings, state and the run, wired to destination plugins.

The core owns the source (PVE Scripts Local), the settings file, the state file,
the run lock and the watcher loop. Everything a destination knows about itself -
its settings keys, its secrets, its records, its panel actions - lives in its own
package and is reached through the plugin registry, so adding a destination edits
nothing here.
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
from .settings import (
    as_bool as _as_bool,
    check_reference_name as _check_reference_name,
    check_secret_references as _check_secret_fields,
    plaintext_secret_fields as _plaintext_secret_fields,
    resolve_secret,
    secret_env_name,
    unresolved_secret_references as _unresolved_secret_fields,
)
from .statefile import load_state, save_state


LOGGER = logging.getLogger(__name__)

# The settings the core itself owns: the source it reads and the switches that
# govern when and whether a run happens. A destination's keys are not listed here
# at all - they are round-tripped through the plugin registry - so this dict is
# the whole of the core's settings vocabulary.
DEFAULT_CONFIG = {
    "pve_local_url": "",
    "pve_local_username": "",
    "pve_local_password": "",
    # A LAN endpoint usually serves a self-signed certificate, so the TLS check can
    # be relaxed per endpoint: this switch for PVE Scripts Local, and each
    # destination's own switch. Both default to verifying.
    "pve_local_verify_tls": True,
    "poll_interval_seconds": None,
    "watch_enabled": False,
    "auto_apply": False,
}

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
    yield "PVE Scripts Local password", config.get("pve_local_password")
    yield from (registry or plugin_registry()).secret_fields(config)


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
        "PVE Scripts Local password",
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
    (registry or plugin_registry()).validate(config, allow_incomplete)


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
        # lent the way to ask for a run, never the event itself: clearing a
        # wake-up that was meant for the watcher is the core's business.
        lent = self.registry.adapters(
            self.config_store, self.state_path, self.lock, self.wake.set
        )
        self.destination_adapters = tuple(lent if destination_adapters is None else destination_adapters)
        self.status = {
            "state": "idle",
            "message": "Configure the PSL endpoint to begin.",
            "last_check": None,
            "last_apply": None,
            "guest_count": 0,
            "planned": [],
            "skipped": [],
            "timings": {},
        }
        self.thread = threading.Thread(target=self._run_loop, name="psl-sync", daemon=True)
        self.thread.start()

    def get_status(self):
        with self.lock:
            return dict(self.status)

    def _set_status(self, **updates):
        with self.lock:
            self.status.update(updates)

    def run_once(self, apply_changes=False, refresh_lxc_config=False):
        # Only one preview/apply may run at a time (watcher thread vs HTTP handlers).
        # A manual run must wait for a watcher run, rather than returning the
        # watcher's status as if the requested fresh Preview/Apply had run.
        if not self.run_lock.acquire(blocking=refresh_lxc_config):
            status = self.get_status()
            return {
                **status,
                "state": "busy",
                "message": "A sync run is already in progress; this request did not start a new preview or apply.",
            }
        try:
            return self._run_once(apply_changes, refresh_lxc_config)
        finally:
            self.run_lock.release()

    def _run_once(self, apply_changes, refresh_lxc_config=False):
        self._set_status(
            state="checking",
            message=(
                "Refreshing guest configuration from Proxmox through PVE Scripts Local."
                if refresh_lxc_config
                else "Reading installed records from PVE Scripts Local."
            ),
            guest_count=0,
            planned=[],
            skipped=[],
            timings={},
        )
        try:
            config = self.config_store.load()
            validate_config(config, registry=self.registry)
            # An unresolved "env:" reference stops the run here, before any client
            # is built with an empty password.
            check_secret_references(config, registry=self.registry)
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
            self._set_status(
                message=(
                    f"Read {len(guests)} guest record(s) from PSL in {psl_seconds:.2f}s; "
                    "building destination plans."
                )
            )
            planning_started = perf_counter()
            destination_plans = [
                (adapter, adapter.plan(guests, config))
                for adapter in self.destination_adapters
            ]
            planning_seconds = perf_counter() - planning_started

            state = load_state(self.state_path)
            try:
                self._set_status(
                    message=(
                        f"Built {len(destination_plans)} destination plan(s) in "
                        f"{planning_seconds:.2f}s; applying destinations."
                    )
                )
                rows = []
                destination_seconds = {}
                for adapter, destination_plan in destination_plans:
                    destination_started = perf_counter()
                    destination_rows = adapter.apply(
                        destination_plan,
                        config,
                        state,
                        apply_changes=apply_changes,
                    )
                    destination_seconds[adapter.key] = round(
                        perf_counter() - destination_started, 3
                    )
                    rows.extend((adapter.label, *row) for row in destination_rows)
                    for row in destination_rows:
                        if row[0] != "OK":
                            LOGGER.info(
                                "Sync result: destination=%s status=%s target=%s resource=%s",
                                adapter.label,
                                row[0],
                                row[1],
                                row[2],
                            )
            finally:
                # Persist ownership of anything already created, even if a later step failed.
                if apply_changes:
                    save_state(self.state_path, state)
            now = datetime.now(timezone.utc).isoformat()
            actionable = sum(
                row[1] in {"CREATE", "UPDATE", "DELETE", "DRY-CREATE", "DRY-UPDATE", "DRY-DELETE"}
                for row in rows
            )
            skipped_rows = [
                {"name": name, "reason": reason, "destination": adapter.label}
                for adapter, destination_plan in destination_plans
                for name, reason in destination_plan.get("skipped", [])
            ]
            skipped_rows.extend(
                {
                    "name": row[3],
                    "reason": row[4],
                    "instance": row[2],
                    "destination": row[0],
                    "status": row[1],
                }
                for row in rows if row[1] in {"CONFLICT", "SKIP", "ERROR"}
            )
            mode = "Applied" if apply_changes else "Previewed"
            timings = {
                "psl_read_seconds": round(psl_seconds, 3),
                "planning_seconds": round(planning_seconds, 3),
                "destinations_seconds": destination_seconds,
                "source_records": len(guests),
            }
            guest_count = max(
                (plan.get("containers", len(guests)) for _, plan in destination_plans),
                default=len(guests),
            )
            self._set_status(
                state="ok",
                message=f"{mode} {actionable} change(s); {len(skipped_rows)} item(s) need attention.",
                last_check=now,
                last_apply=now if apply_changes else self.get_status().get("last_apply"),
                guest_count=guest_count,
                planned=[
                    {
                        "destination": row[0],
                        "status": row[1],
                        "instance": row[2],
                        "domain": row[3],
                        "details": row[4],
                    }
                    for row in rows
                ],
                skipped=skipped_rows,
                timings=timings,
            )
            return self.get_status()
        except Exception as error:
            self._set_status(
                state="error",
                message=str(error),
                last_check=datetime.now(timezone.utc).isoformat(),
                guest_count=0,
                planned=[],
                skipped=[],
            )
            return self.get_status()

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
                    self.run_once(
                        apply_changes=config.get("auto_apply", False),
                        refresh_lxc_config=True,
                    )
            except Exception as error:
                LOGGER.exception("Background watcher iteration failed")
                self._set_status(
                    state="error",
                    message=f"Background watcher failed and will retry: {error}",
                    last_check=datetime.now(timezone.utc).isoformat(),
                )
                interval = 30