"""Configuration management for the reverse proxy automation core.
"""

import json
import os
import re
import threading
from pathlib import Path

from .settings import (
    as_bool as _as_bool,
    check_reference_name as _check_reference_name,
    check_secret_references as _check_secret_fields,
    plaintext_secret_fields as _plaintext_secret_fields,
    resolve_secret,
    secret_env_name,
    unresolved_secret_references as _unresolved_secret_fields,
)
from .plugins.registry import default_registry

# The statuses a destination's rows use. A change is something the run made or would
# make; the attention set is what a user has to look at afterwards.
CHANGE_STATUSES = {"CREATE", "UPDATE", "DELETE", "DRY-CREATE", "DRY-UPDATE", "DRY-DELETE"}
ATTENTION_STATUSES = {"CONFLICT", "SKIP", "ERROR"}

# The settings the core itself owns: the source it reads and the switches that
# govern when and whether a run happens.
DEFAULT_CONFIG = {
    "pve_local_url": "",
    "pve_local_username": "",
    "pve_local_password": "",
    "pve_local_verify_tls": True,
    "poll_interval_seconds": None,
    "watch_enabled": False,
    "auto_apply": False,
    "destinations_enabled": {},
    "destinations_auto_apply": {},
}

FLAG_MAPS = ("destinations_enabled", "destinations_auto_apply")

def destination_enabled(config, key):
    """Whether a destination takes part in a run. Destinations default to on."""
    flags = config.get("destinations_enabled")
    return _as_bool(flags.get(key), True) if isinstance(flags, dict) else True

def destination_auto_apply(config, key):
    """Whether the watcher may apply for this destination without being asked."""
    # The master switch is a "Check All/None" override.
    # If it's explicitly set to False, no destination can auto-apply.
    # If it's True, we respect the per-destination setting.
    if not _as_bool(config.get("auto_apply"), False):
        return False
    flags = config.get("destinations_auto_apply")
    return _as_bool(flags.get(key), True) if isinstance(flags, dict) else True

def destination_applies(config, key):
    """Whether the watcher may write for one destination by itself."""
    return (
        destination_enabled(config, key)
        and destination_auto_apply(config, key)
    )

PSL_SECRET_DESCRIPTION = "PVE Scripts Local password"
PSL_SECRET_ENV_PATTERN = re.compile(r"^PVE_LOCAL_PASSWORD$")
PSL_SECRET_HINT = " (use env:PVE_LOCAL_PASSWORD)"

def plugin_registry():
    """The destinations the app ships with, plus the ones installed beside it."""
    return default_registry()

def secret_fields(config, registry=None):
    """(description, value) for the source's password and every destination's secrets."""
    yield PSL_SECRET_DESCRIPTION, config.get("pve_local_password")
    yield from (registry or plugin_registry()).secret_fields(
        config, enabled=lambda key: destination_enabled(config, key)
    )

def unresolved_secret_references(config, environ=None, registry=None):
    """Every (description, variable name) whose "env:" reference is not set."""
    return _unresolved_secret_fields(secret_fields(config, registry), environ)

def plaintext_secret_fields(config, registry=None):
    """Descriptions of every secret the settings file stores as a literal password."""
    return _plaintext_secret_fields(secret_fields(config, registry))

def check_secret_references(config, environ=None, registry=None):
    """Raise ValueError naming every "env:" reference that resolves to nothing."""
    return _check_secret_fields(secret_fields(config, registry), environ)

def check_source_secret_references(config, environ=None):
    """Raise ValueError for the source's own unresolved "env:" reference only."""
    fields = [(PSL_SECRET_DESCRIPTION, config.get("pve_local_password"))]
    return _check_secret_fields(fields, environ)

def normalize_config(loaded, registry=None):
    """Merge a stored settings file with the defaults every destination declares."""
    loaded = loaded if isinstance(loaded, dict) else {}
    config = {key: loaded.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    config.update((registry or plugin_registry()).clean_config(loaded))
    config["pve_local_verify_tls"] = _as_bool(config.get("pve_local_verify_tls"), True)
    for key in ("watch_enabled", "auto_apply"):
        config[key] = _as_bool(config.get(key), False)
    for name in FLAG_MAPS:
        config[name] = _flag_map(loaded, name)
    return config

def _flag_map(loaded, name):
    """One {destination key: bool} map read out of a stored settings file."""
    flags = loaded.get(name)
    return {
        str(key): _as_bool(value, True)
        for key, value in (flags.items() if isinstance(flags, dict) else ())
    }

class ConfigStore:
    """The settings file, shared by the core and every destination."""

    def __init__(self, path, registry=None):
        self.path = Path(path)
        self.lock = threading.RLock()
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
            config.update(self.registry.merge_config(old, submitted))
            validate_config(config, allow_incomplete=True, registry=self.registry)
            parent = self.path.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
                os.chmod(parent, 0o700)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
                json.dump(config, config_file, indent=2)
                config_file.write("\n")
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            return config

def validate_core_config(config, allow_incomplete=False):
    """Raise ValueError naming the first problem in the settings the core owns."""
    _check_reference_name(
        PSL_SECRET_DESCRIPTION,
        config.get("pve_local_password"),
        PSL_SECRET_ENV_PATTERN,
        hint=PSL_SECRET_HINT,
    )
    if not isinstance(config.get("pve_local_verify_tls", True), bool):
        raise ValueError("PVE Scripts Local field 'pve_local_verify_tls' must be true or false")
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
    """Raise ValueError naming the first problem in the whole settings file."""
    validate_core_config(config, allow_incomplete)
    (registry or plugin_registry()).validate(
        config, allow_incomplete, enabled=lambda key: destination_enabled(config, key)
    )

def public_config(config, environ=None, registry=None):
    """Config as sent to the browser; no password and no secret is ever included."""
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
    result["plaintext_secrets"] = plaintext_secret_fields(config, registry)
    return result
