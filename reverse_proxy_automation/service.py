import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .clients import ApiError, NpmPlusClient, PveScriptsLocalClient
from .integrations import NpmPlusDestinationAdapter, PslSourceAdapter
from .planner import DOMAIN_PATTERN, HOST_LABEL_PATTERN, parse_aliases
from .sync import adopted_host_ids, adopt_host, forget_host, load_state, reconcile, save_state


LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "pve_local_url": "",
    "pve_local_username": "",
    "pve_local_password": "",
    # A LAN endpoint usually serves a self-signed certificate, so the TLS check can
    # be relaxed per endpoint: this switch for PVE Scripts Local, and each
    # instance's own "verify_tls". Both default to verifying.
    "pve_local_verify_tls": True,
    "public_domain": "",
    # Any number of NPMPlus instances, keyed by a slug that VLAN mappings refer to.
    "instances": {},
    "vlan_map": {},
    "entry_aliases": {},
    "entry_port_overrides": {},
    "excluded_guest_hostnames": "",
    # Extra names a guest's proxy host answers to, one guest per line as
    # "guest = name, name"; a bare name is a label under the public domain.
    "aliases": "",
    "poll_interval_seconds": None,
    "watch_enabled": False,
    "auto_apply": False,
    "delete_removed_hosts": False,
    "max_deletes_per_run": None,
}


# An instance name prefixes every "<instance>:<domain>" ownership key in the state
# file, so keep it a slug: no colons and no whitespace.
INSTANCE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
INSTANCE_FIELDS = ("label", "url", "identity", "secret")
# Settings files written before instances were user-defined used these flat keys.
LEGACY_INSTANCE_KEYS = {
    "lab": ("npm_lab", "Lab NPMPlus"),
    "public": ("npm_public", "Public NPMPlus"),
}
# A secret field holds either the literal password or a reference such as
# "env:NPM_LAB_SECRET". The reference is read from the environment the dashboard
# was started from when a client is built, so the settings file can stay
# secret-free while the value never has to pass through the browser.
SECRET_ENV_PREFIX = "env:"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PSL_SECRET_ENV_PATTERN = re.compile(r"^PVE_LOCAL_PASSWORD$")


def npm_secret_env_pattern(instance):
    """Environment variable allowed for one named NPMPlus instance."""
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", str(instance)).upper()
    return re.compile(rf"^NPM_{re.escape(suffix)}_SECRET$")


def secret_env_name(value):
    """Environment variable a secret field references, or None for a literal value."""
    text = str(value or "")
    if text[: len(SECRET_ENV_PREFIX)].lower() != SECRET_ENV_PREFIX:
        return None
    return text[len(SECRET_ENV_PREFIX) :].strip()


def resolve_secret(value, environ=None):
    """The literal secret, or the value of the variable an "env:" reference names.

    A reference to an unset variable resolves to "", so callers can report the
    missing variable instead of sending an empty password to an API.
    """
    name = secret_env_name(value)
    if name is None:
        return str(value or "")
    return str((os.environ if environ is None else environ).get(name, ""))


def _secret_fields(config):
    """(description, secret field) for the PSL password and every instance."""
    yield "PVE Scripts Local password", config.get("pve_local_password")
    for instance, entry in (config.get("instances") or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        yield f"NPMPlus instance '{instance}' secret", entry.get("secret")


def unresolved_secret_references(config, environ=None):
    """Every (description, variable name) whose "env:" reference is not set."""
    unresolved = []
    for description, value in _secret_fields(config):
        name = secret_env_name(value)
        if name and not resolve_secret(value, environ):
            unresolved.append((description, name))
    return unresolved


def check_secret_references(config, environ=None):
    """Raise ValueError naming every "env:" reference that resolves to nothing.

    Without this check an unset variable would look like "no password
    configured" and a run would quietly talk to the API unauthenticated.
    """
    missing = unresolved_secret_references(config, environ)
    if not missing:
        return
    details = "; ".join(
        f"{description} references {name}, which is not set" for description, name in missing
    )
    raise ValueError(f"{details}. Set it in the environment the dashboard was started from")


def _check_reference_name(description, value, allowed_pattern=None):
    """Reject env references that do not follow the field's supported naming rule."""
    name = secret_env_name(value)
    if name is None:
        return
    if not ENV_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"{description} references '{name}', which is not a valid environment variable name"
        )
    if allowed_pattern is not None and not allowed_pattern.fullmatch(name):
        raise ValueError(
            f"{description} must reference the variable assigned to this secret field "
            "(PVE_LOCAL_PASSWORD for PSL or NPM_<INSTANCE>_SECRET for NPMPlus)"
        )


def _as_bool(value, default):
    """Coerce a stored or submitted switch to a bool, falling back to the default.

    A hand-edited settings file may hold "false" or 0 rather than a JSON bool, so
    the recognisable spellings are accepted; anything unrecognised keeps the
    default instead of failing the whole file.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def instance_label(name, entry):
    """Display name for an instance, falling back to "<name> NPMPlus"."""
    label = str((entry or {}).get("label") or "").strip()
    return label or f"{name} NPMPlus"


def _clean_instance(entry):
    entry = entry if isinstance(entry, dict) else {}
    return {
        "label": str(entry.get("label") or "").strip(),
        "url": str(entry.get("url") or "").strip(),
        "identity": str(entry.get("identity") or "").strip(),
        # A secret may legitimately contain spaces, so it is never stripped.
        "secret": str(entry.get("secret") or ""),
        # Coerced here, so a hand-edited "false" still relaxes verification and the
        # browser always receives a real bool.
        "verify_tls": _as_bool(entry.get("verify_tls"), True),
    }


def _instances_from_config(loaded):
    """Configured instances, folding in legacy npm_lab_*/npm_public_* keys."""
    instances = {}
    raw = loaded.get("instances")
    if isinstance(raw, dict):
        for name, entry in raw.items():
            key = str(name).strip().lower()
            if key:
                instances[key] = _clean_instance(entry)
    for name, (prefix, label) in LEGACY_INSTANCE_KEYS.items():
        if name in instances:
            continue
        legacy = {field: loaded.get(f"{prefix}_{field}") for field in ("url", "identity", "secret")}
        if any(str(value or "").strip() for value in legacy.values()):
            instances[name] = _clean_instance({**legacy, "label": label})
    return instances


def _vlan_map_from_config(raw):
    """Copy a VLAN map, normalising the tag/instance/zone spelling."""
    if not isinstance(raw, dict):
        return raw  # let validate_config reject it
    vlan_map = {}
    for tag, mapping in raw.items():
        if not isinstance(mapping, dict):
            vlan_map[str(tag).strip().lower()] = mapping
            continue
        vlan_map[str(tag).strip().lower()] = {
            "instance": str(mapping.get("instance") or "").strip().lower(),
            "zone": str(mapping.get("zone") or "").strip().lower().rstrip("."),
        }
    return vlan_map


def normalize_config(loaded):
    """Merge a stored config with the defaults and migrate legacy instance keys."""
    loaded = loaded if isinstance(loaded, dict) else {}
    config = {
        key: loaded.get(key, default)
        for key, default in DEFAULT_CONFIG.items()
        if not isinstance(default, dict)
    }
    config["instances"] = _instances_from_config(loaded)
    config["vlan_map"] = _vlan_map_from_config(loaded.get("vlan_map", {}))
    config["entry_aliases"] = loaded.get("entry_aliases", {})
    config["entry_port_overrides"] = loaded.get("entry_port_overrides", {})
    # Sent to the browser and used to build the PSL client, so keep it a real bool.
    config["pve_local_verify_tls"] = _as_bool(config.get("pve_local_verify_tls"), True)
    for key in ("watch_enabled", "auto_apply", "delete_removed_hosts"):
        config[key] = _as_bool(config.get(key), False)
    return config


def merge_instances(existing, submitted):
    """Validate submitted instances, keeping stored secrets when a field is blank."""
    if not isinstance(submitted, dict):
        raise ValueError("Instances must be a JSON object keyed by instance name")
    merged = {}
    for name, entry in submitted.items():
        key = str(name).strip().lower()
        if not key:
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"NPMPlus instance '{key}' must be a JSON object")
        stored = existing.get(key) if isinstance(existing.get(key), dict) else {}
        instance = _clean_instance({**stored, **entry})
        if not instance["secret"]:
            instance["secret"] = str(stored.get("secret") or "")
        merged[key] = instance
    return merged


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            if not self.path.exists():
                return normalize_config({})
            with self.path.open("r", encoding="utf-8") as config_file:
                loaded = json.load(config_file)
            return normalize_config(loaded)

    def save(self, submitted):
        with self.lock:
            old = self.load()
            config = {**old}
            for key in DEFAULT_CONFIG:
                if key in {"instances", "vlan_map"} or key not in submitted:
                    continue
                value = submitted[key]
                if key.endswith("_password") or key.endswith("_secret"):
                    if value:
                        config[key] = value
                else:
                    config[key] = value
            if "instances" in submitted:
                config["instances"] = merge_instances(old["instances"], submitted["instances"])
            if "vlan_map" in submitted:
                config["vlan_map"] = _vlan_map_from_config(submitted["vlan_map"])
            validate_config(config, allow_incomplete=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
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


def validate_config(config, allow_incomplete=False):
    instances = config.get("instances")
    if not isinstance(instances, dict):
        raise ValueError("Instances must be a JSON object keyed by NPMPlus instance name")
    for name, entry in instances.items():
        if not INSTANCE_NAME_PATTERN.fullmatch(str(name)):
            raise ValueError(f"Instance name '{name}' must use 1-32 characters of a-z, 0-9, '-' or '_'")
        if not isinstance(entry, dict):
            raise ValueError(f"NPMPlus instance '{name}' must be a JSON object")
        for field in INSTANCE_FIELDS:
            if entry.get(field) is not None and not isinstance(entry[field], str):
                raise ValueError(f"NPMPlus instance '{name}' field '{field}' must be a string")
        if not isinstance(entry.get("verify_tls", True), bool):
            raise ValueError(f"NPMPlus instance '{name}' field 'verify_tls' must be true or false")
        _check_reference_name(
            f"NPMPlus instance '{name}' secret",
            entry.get("secret"),
            npm_secret_env_pattern(name),
        )
    _check_reference_name(
        "PVE Scripts Local password",
        config.get("pve_local_password"),
        PSL_SECRET_ENV_PATTERN,
    )
    if not isinstance(config.get("pve_local_verify_tls", True), bool):
        raise ValueError("PVE Scripts Local field 'pve_local_verify_tls' must be true or false")
    for key in ("watch_enabled", "auto_apply", "delete_removed_hosts"):
        if not isinstance(config.get(key), bool):
            raise ValueError(f"'{key}' must be true or false")
    if not isinstance(config.get("vlan_map"), dict):
        raise ValueError("VLAN map must be a JSON object keyed by VLAN tag")
    entry_aliases = config.get("entry_aliases")
    if not isinstance(entry_aliases, dict):
        raise ValueError("Per-entry aliases must be a JSON object")
    public_domain = str(config.get("public_domain") or "").strip().lower()
    for key, names in entry_aliases.items():
        instance, separator, hostname = str(key).partition(":")
        if not separator or not INSTANCE_NAME_PATTERN.fullmatch(instance) or not HOST_LABEL_PATTERN.fullmatch(hostname):
            raise ValueError(f"Per-entry alias key '{key}' must be '<instance>:<guest-hostname>'")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError(f"Per-entry aliases for '{key}' must be a list of hostnames")
        if names:
            if not DOMAIN_PATTERN.fullmatch(public_domain):
                raise ValueError("Set a valid public hostname domain before saving per-entry aliases")
            normalized = parse_aliases(
                f"{hostname} = {', '.join(names)}",
                public_domain,
            )
            if hostname not in normalized:
                raise ValueError(f"Per-entry aliases for '{key}' must include a valid hostname")
    entry_port_overrides = config.get("entry_port_overrides")
    if not isinstance(entry_port_overrides, dict):
        raise ValueError("Per-entry port overrides must be a JSON object")
    for key, port in entry_port_overrides.items():
        instance, separator, hostname = str(key).partition(":")
        if not separator or not INSTANCE_NAME_PATTERN.fullmatch(instance) or not HOST_LABEL_PATTERN.fullmatch(hostname):
            raise ValueError(f"Per-entry port override key '{key}' must be '<instance>:<guest-hostname>'")
        if isinstance(port, bool) or not (
            isinstance(port, int) or isinstance(port, str) and port.isdecimal()
        ):
            raise ValueError(f"Per-entry port override for '{key}' must be an integer from 1 to 65535")
        try:
            numeric_port = int(port)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Per-entry port override for '{key}' must be an integer from 1 to 65535") from error
        if not 1 <= numeric_port <= 65535:
            raise ValueError(f"Per-entry port override for '{key}' must be an integer from 1 to 65535")
    for vlan, mapping in config["vlan_map"].items():
        if not isinstance(mapping, dict):
            raise ValueError(f"VLAN {vlan} must be a JSON object with 'instance' and 'zone'")
        instance = mapping.get("instance")
        if instance not in instances:
            raise ValueError(
                f"VLAN {vlan} points at NPMPlus instance '{instance}', which is not configured; "
                "add that instance or remove the VLAN row"
            )
        if not isinstance(mapping.get("zone"), str) or not mapping["zone"].strip():
            raise ValueError(f"VLAN {vlan} needs an upstream DNS zone such as internal.example.net")
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
    raw_max_deletes = config.get("max_deletes_per_run")
    if raw_max_deletes in (None, ""):
        max_deletes = None
        if config.get("delete_removed_hosts"):
            raise ValueError("Set the deletion limit before enabling Delete removed guests")
    else:
        try:
            max_deletes = int(raw_max_deletes)
        except (TypeError, ValueError) as error:
            raise ValueError("Max deletions per run must be an integer") from error
        if not 1 <= max_deletes <= 1000:
            raise ValueError("Max deletions per run must be between 1 and 1000")
    config["max_deletes_per_run"] = max_deletes
    aliases = config.get("aliases", "")
    if not isinstance(aliases, str):
        raise ValueError("Alias hostnames must be a text field, one guest per line")
    # A line that cannot be read is refused here, while the user is looking at the
    # settings, rather than silently dropping a name from a host at the next run.
    if aliases.strip() and DOMAIN_PATTERN.fullmatch(str(config.get("public_domain") or "").strip().lower()):
        parse_aliases(aliases, config.get("public_domain"))
    if not allow_incomplete and not config.get("pve_local_url"):
        raise ValueError("Set the PVE Scripts Local URL first")


def public_config(config, environ=None):
    """Config as sent to the browser; passwords and instance secrets are never included.

    An "env:" reference is reported by name plus whether it currently resolves,
    so the page can tell the user which variable to set without ever seeing a
    secret value.
    """
    result = {
        key: value
        for key, value in config.items()
        if key != "instances" and not (key.endswith("_password") or key.endswith("_secret"))
    }
    psl_password = config.get("pve_local_password")
    result["pve_local_password_configured"] = bool(resolve_secret(psl_password, environ))
    result["pve_local_password_env"] = secret_env_name(psl_password) or ""
    result["instances"] = {
        name: {
            "label": entry.get("label", ""),
            "url": entry.get("url", ""),
            "identity": entry.get("identity", ""),
            "secret_configured": bool(resolve_secret(entry.get("secret"), environ)),
            "secret_env": secret_env_name(entry.get("secret")) or "",
            # Reported so the page can restore an endpoint's self-signed switch.
            "verify_tls": _as_bool(entry.get("verify_tls"), True),
        }
        for name, entry in (config.get("instances") or {}).items()
    }
    result["pve_local_verify_tls"] = _as_bool(config.get("pve_local_verify_tls"), True)
    return result


class SyncService:
    def __init__(self, config_store, state_path, source_adapter=None, destination_adapters=None):
        self.config_store = config_store
        self.state_path = Path(state_path)
        self.source_adapter = source_adapter or PslSourceAdapter(self._source_client)
        self.destination_adapters = tuple(
            destination_adapters
            if destination_adapters is not None
            else [NpmPlusDestinationAdapter(self._instance_client, reconcile)]
        )
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
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
            validate_config(config)
            # An unresolved "env:" reference stops the run here, before any client
            # is built with an empty password.
            check_secret_references(config)
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
                "npmplus_seconds": destination_seconds.get("npmplus", 0),
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

    @staticmethod
    def _instance_client(instance, entry):
        return NpmPlusClient(
            instance_label(instance, entry),
            entry.get("url", ""),
            entry.get("identity", ""),
            # A stored "env:VAR" reference is swapped for its value only here, so
            # neither the settings file nor the browser ever holds the password.
            resolve_secret(entry.get("secret", "")),
            # Only this instance stops verifying, and only when its switch is off.
            verify_tls=_as_bool(entry.get("verify_tls"), True),
        )

    def test_connection(self, target):
        """Check saved credentials for 'pve' or any configured NPMPlus instance."""
        config = self.config_store.load()
        if target == "pve":
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
            return {"ok": True, "message": f"PVE Scripts Local reachable: {len(scripts)} installed record(s)."}
        instances = config.get("instances") or {}
        if target in instances:
            entry = instances[target]
            if not entry.get("url"):
                raise ValueError(f"Set the URL for NPMPlus instance '{target}' first")
            # Only this target's reference is required to pass, so an unfinished
            # instance elsewhere in the file cannot block a working test.
            check_secret_references({"instances": {target: entry}})
            client = self._instance_client(target, entry)
            hosts = client.proxy_hosts()
            certificates = client.certificates()
            return {
                "ok": True,
                "message": (
                    f"{instance_label(target, entry)} reachable: "
                    f"{len(hosts)} proxy host(s), {len(certificates)} certificate(s)."
                ),
            }
        raise ValueError(f"Unknown test target '{target}'")

    def list_entries(self):
        """Every proxy host on every configured instance, and how this app treats it.

        Read-only: it builds a client per instance and never touches the state file,
        so the page can show what an instance holds without starting a run. An
        instance that cannot be reached is reported in its own row instead of
        failing the whole list.
        """
        config = self.config_store.load()
        state = load_state(self.state_path)
        legacy_aliases = parse_aliases(config.get("aliases", ""), config.get("public_domain")) \
            if str(config.get("aliases") or "").strip() else {}
        instances = []
        configured_instances = sorted(config["instances"].items())
        clients = {
            instance: self._instance_client(instance, entry)
            for instance, entry in configured_instances
            if entry.get("url")
        }

        def read_hosts(instance, client):
            try:
                return instance, client.proxy_hosts(expand_certificate=False), None
            except (ApiError, OSError) as error:
                return instance, [], error

        client_items = list(clients.items())
        if len(client_items) > 1:
            with ThreadPoolExecutor(max_workers=min(len(client_items), 8)) as executor:
                host_results = list(executor.map(lambda row: read_hosts(*row), client_items))
        else:
            host_results = [read_hosts(*row) for row in client_items]
        hosts_by_instance = {
            instance: (hosts, error)
            for instance, hosts, error in host_results
        }

        managed_by_instance = {instance: set() for instance, _ in configured_instances}
        for key, host_id in (state.get("managed") or {}).items():
            instance = key.partition(":")[0]
            if instance in managed_by_instance:
                managed_by_instance[instance].add(str(host_id))
        adopted_by_instance = {
            instance: adopted_host_ids(state, instance)
            for instance, _ in configured_instances
        }

        for instance, entry in configured_instances:
            record = {
                "instance": instance,
                "label": instance_label(instance, entry),
                "url": entry.get("url", ""),
                "error": "",
                "hosts": [],
            }
            if not entry.get("url"):
                record["error"] = f"No URL is saved for {record['label']}."
                instances.append(record)
                continue
            hosts, error = hosts_by_instance[instance]
            if error is not None:
                record["error"] = str(error)
                instances.append(record)
                continue
            for host in sorted(hosts, key=lambda item: int(item.get("id") or 0)):
                guest_hostname = str(host.get("forward_host") or "").strip().lower().rstrip(".").split(".", 1)[0]
                configured_aliases = config["entry_aliases"].get(f"{instance}:{guest_hostname}")
                record["hosts"].append({
                    "id": str(host.get("id")),
                    "domain_names": [str(name) for name in host.get("domain_names", [])],
                    "guest_hostname": guest_hostname,
                    "aliases": (
                        list(configured_aliases)
                        if configured_aliases is not None
                        else legacy_aliases.get(guest_hostname, [
                            str(name) for name in host.get("domain_names", [])[1:]
                        ])
                    ),
                    "forward_scheme": str(host.get("forward_scheme") or ""),
                    "forward_host": str(host.get("forward_host") or ""),
                    "forward_port": host.get("forward_port"),
                    "port_override": config.get("entry_port_overrides", {}).get(
                        f"{instance}:{guest_hostname}"
                    ),
                    "enabled": bool(host.get("enabled", True)),
                    "ownership": (
                        "managed"
                        if str(host.get("id")) in managed_by_instance[instance]
                        else "adopted"
                        if str(host.get("id")) in adopted_by_instance[instance]
                        else "unmanaged"
                    ),
                })
            instances.append(record)
        return {"instances": instances}

    def set_entry_aliases(self, instance, host_id, aliases):
        """Save aliases for one guest route without changing another route's names."""
        instance = str(instance or "").strip().lower()
        if not isinstance(aliases, list) or any(not isinstance(name, str) for name in aliases):
            raise ValueError("Aliases must be a list of hostnames")
        with self._state_lock():
            config = self.config_store.load()
            entry, host, _ = self._known_host(instance, host_id)
            guest_hostname = (
                str(host.get("forward_host") or "").strip().lower().rstrip(".").split(".", 1)[0]
            )
            if not HOST_LABEL_PATTERN.fullmatch(guest_hostname):
                raise ValueError(
                    f"Cannot identify the guest hostname for host {host_id} on {instance_label(instance, entry)}"
                )
            alias_map = dict(config.get("entry_aliases") or {})
            key = f"{instance}:{guest_hostname}"
            cleaned = [name.strip().lower().rstrip(".") for name in aliases if name.strip()]
            if cleaned:
                parsed = parse_aliases(
                    f"{guest_hostname} = {', '.join(cleaned)}",
                    config.get("public_domain"),
                )
                alias_map[key] = parsed[guest_hostname]
            else:
                alias_map[key] = []
            self.config_store.save({"entry_aliases": alias_map})
            self.wake_loop()
        return {
            "ok": True,
            "guest_hostname": guest_hostname,
            "aliases": alias_map[key],
            "message": (
                f"Saved aliases for {guest_hostname}; they will be applied on the next run."
                if alias_map[key]
                else f"Cleared saved aliases for {guest_hostname}; they will be applied on the next run."
            ),
        }

    def set_entry_port(self, instance, host_id, port):
        """Save or clear the detected-port override for one guest route."""
        instance = str(instance or "").strip().lower()
        if port in (None, ""):
            numeric_port = None
        else:
            if isinstance(port, bool) or not (
                isinstance(port, int) or isinstance(port, str) and port.isdecimal()
            ):
                raise ValueError("Port override must be an integer from 1 to 65535, or blank to use detection")
            try:
                numeric_port = int(port)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Port override must be an integer from 1 to 65535, or blank to use detection"
                ) from error
            if not 1 <= numeric_port <= 65535:
                raise ValueError("Port override must be an integer from 1 to 65535, or blank to use detection")
        with self._state_lock():
            config = self.config_store.load()
            entry, host, _ = self._known_host(instance, host_id)
            guest_hostname = (
                str(host.get("forward_host") or "").strip().lower().rstrip(".").split(".", 1)[0]
            )
            if not HOST_LABEL_PATTERN.fullmatch(guest_hostname):
                raise ValueError(
                    f"Cannot identify the guest hostname for host {host_id} on {instance_label(instance, entry)}"
                )
            overrides = dict(config.get("entry_port_overrides") or {})
            key = f"{instance}:{guest_hostname}"
            if numeric_port is None:
                overrides.pop(key, None)
            else:
                overrides[key] = numeric_port
            self.config_store.save({"entry_port_overrides": overrides})
            self.wake_loop()
        return {
            "ok": True,
            "guest_hostname": guest_hostname,
            "port_override": numeric_port,
            "message": (
                f"Saved port override {numeric_port} for {guest_hostname}; it will be applied on the next run."
                if numeric_port is not None
                else f"Cleared the port override for {guest_hostname}; PSL detection will be used on the next run."
            ),
        }

    def set_ownership(self, instance, host_id, action):
        """Adopt an entry so a matching guest may update it, or release it again.

        Releasing removes every record kept for that host, so from then on the app
        leaves the entry as it stands - including when its guest disappears.
        """
        action = str(action or "").strip().lower()
        if action not in {"adopt", "release"}:
            raise ValueError("Ownership action must be 'adopt' or 'release'")
        instance = str(instance or "").strip().lower()
        entry, host, _ = self._known_host(instance, host_id)
        with self._state_lock():
            state = load_state(self.state_path)
            if action == "adopt":
                adopt_host(state, instance, host["id"])
            else:
                forget_host(state, instance, host["id"])
            save_state(self.state_path, state)
        label = instance_label(instance, entry)
        message = (
            f"{host['id']} on {label} is adopted: a matching guest updates it, and it is never deleted on its own."
            if action == "adopt"
            else f"{host['id']} on {label} is no longer owned by this app; it is left alone until you adopt it again."
        )
        return {"ok": True, "message": message, "ownership": "adopted" if action == "adopt" else "unmanaged"}

    def delete_entry(self, instance, host_id):
        """Delete one proxy host from an instance, whatever owns it.

        The watcher's own cleanup is guarded by its switches and deletion limit;
        this is the page's explicit removal, so it goes ahead once the host is
        confirmed to still be there. A host this app manages is created again by the
        next apply while its guest is still in PSL - release it first to keep it away.
        """
        instance = str(instance or "").strip().lower()
        entry, host, client = self._known_host(instance, host_id)
        label = instance_label(instance, entry)
        names = ", ".join(str(name) for name in host.get("domain_names", [])) or f"host {host['id']}"
        with self._state_lock():
            client.delete_proxy_host(host["id"])
            state = load_state(self.state_path)
            forget_host(state, instance, host["id"])
            save_state(self.state_path, state)
        return {"ok": True, "message": f"Deleted {names} from {label}."}

    def _known_host(self, instance, host_id):
        """The saved instance entry and the NPMPlus host it holds right now.

        The ID is checked against what the instance holds at this moment, so a page
        left open cannot change or delete an entry that has since been replaced.
        """
        entry = (self.config_store.load()["instances"] or {}).get(instance)
        if not isinstance(entry, dict):
            raise ValueError(f"NPMPlus instance '{instance}' is not configured")
        if not entry.get("url"):
            raise ValueError(f"No URL is saved for {instance_label(instance, entry)}")
        wanted = str(host_id or "").strip()
        client = self._instance_client(instance, entry)
        for host in client.proxy_hosts(expand_certificate=False):
            if str(host.get("id")) == wanted:
                return entry, host, client
        raise ValueError(
            f"Proxy host {wanted or '(none)'} is not on {instance_label(instance, entry)}; reload the entries list"
        )

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