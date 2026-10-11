"""NPMPlus's settings: its instances, VLAN routing, aliases and delete policy.

These keys used to sit in the core's DEFAULT_CONFIG, which meant the settings page,
the settings file and validate_config all knew what an "instance" or a "VLAN map"
was. They are the destination's own vocabulary, so they live with the destination:
the core asks this module which keys it owns and hands back whatever the file holds.
"""

import re

from ...planner import DOMAIN_PATTERN, HOST_LABEL_PATTERN, parse_aliases
from ...settings import as_bool, check_reference_name, resolve_secret, secret_env_name


# The settings keys this destination owns. They are the keys the core round-trips
# and the keys a page may submit, so a second destination cannot collide with them.
KEYS = (
    "public_domain",
    "instances",
    "vlan_map",
    "entry_aliases",
    "entry_port_overrides",
    "excluded_guest_hostnames",
    "aliases",
    "delete_removed_hosts",
    "max_deletes_per_run",
)

DEFAULTS = {
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
# Switches stored as bools, whichever way a hand-edited file spells them.
SWITCH_KEYS = ("delete_removed_hosts",)
# Where a reader is sent when an "env:" reference does not name what it should.
SECRET_NAME_HINT = " (PVE_LOCAL_PASSWORD for PSL or NPM_<INSTANCE>_SECRET for NPMPlus)"


def secret_env_pattern(instance):
    """Environment variable allowed for one named instance's secret."""
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", str(instance)).upper()
    return re.compile(rf"^NPM_{re.escape(suffix)}_SECRET$")


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
        "verify_tls": as_bool(entry.get("verify_tls"), True),
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
        return raw  # let validate() reject it
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


def config_defaults():
    """Settings keys this destination adds, with their default values."""
    return dict(DEFAULTS)


def config_keys():
    """The settings keys this destination owns, so the core can round-trip them."""
    return KEYS


def clean_config(loaded):
    """This destination's settings as read from a settings file, migrated.

    The core hands over the whole loaded file: an older file spells instances as
    flat ``npm_lab_*`` keys, and reading them here is what lets that file keep
    working without the core knowing what an instance is.
    """
    loaded = loaded if isinstance(loaded, dict) else {}
    fragment = {
        key: loaded.get(key, default)
        for key, default in DEFAULTS.items()
        if not isinstance(default, dict)
    }
    fragment["instances"] = _instances_from_config(loaded)
    fragment["vlan_map"] = _vlan_map_from_config(loaded.get("vlan_map", {}))
    fragment["entry_aliases"] = loaded.get("entry_aliases", {})
    fragment["entry_port_overrides"] = loaded.get("entry_port_overrides", {})
    for key in SWITCH_KEYS:
        fragment[key] = as_bool(fragment.get(key), False)
    return fragment


def merge_config(existing, submitted):
    """Submitted settings over the stored ones, keeping a blank instance secret.

    Only the keys this destination owns are read, so a page that posts one
    destination's form cannot disturb another one's settings or the core's.
    """
    merged = {}
    for key in KEYS:
        if key in {"instances", "vlan_map"} or key not in submitted:
            continue
        value = submitted[key]
        if key.endswith("_password") or key.endswith("_secret"):
            if value:
                merged[key] = value
        else:
            merged[key] = value
    if "instances" in submitted:
        merged["instances"] = merge_instances(
            existing.get("instances") or {}, submitted["instances"]
        )
    if "vlan_map" in submitted:
        merged["vlan_map"] = _vlan_map_from_config(submitted["vlan_map"])
    return merged


def validate(config, allow_incomplete=False):
    """Raise ValueError naming the first problem in this destination's settings.

    ``allow_incomplete`` is unused here: the core uses it to save a settings page
    that is still missing the endpoint the source needs, while anything NPMPlus
    states has to be coherent the moment it is stated. It stays in the signature so
    every destination is handed the same arguments.
    """
    instances = config.get("instances")
    if not isinstance(instances, dict):
        raise ValueError("Instances must be a JSON object keyed by NPMPlus instance name")
    for name, entry in instances.items():
        if not INSTANCE_NAME_PATTERN.fullmatch(str(name)):
            raise ValueError(
                f"Instance name '{name}' must use 1-32 characters of a-z, 0-9, '-' or '_'"
            )
        if not isinstance(entry, dict):
            raise ValueError(f"NPMPlus instance '{name}' must be a JSON object")
        for field in INSTANCE_FIELDS:
            if entry.get(field) is not None and not isinstance(entry[field], str):
                raise ValueError(f"NPMPlus instance '{name}' field '{field}' must be a string")
        if not isinstance(entry.get("verify_tls", True), bool):
            raise ValueError(f"NPMPlus instance '{name}' field 'verify_tls' must be true or false")
        check_reference_name(
            f"NPMPlus instance '{name}' secret",
            entry.get("secret"),
            secret_env_pattern(name),
            hint=SECRET_NAME_HINT,
        )
    # A fragment of a settings file may be validated before it is merged with this
    # destination's defaults, so a switch that is absent is its default, not an
    # error; only a value that is present and not a boolean is refused.
    for key in SWITCH_KEYS:
        if not isinstance(config.get(key, DEFAULTS[key]), bool):
            raise ValueError(f"'{key}' must be true or false")
    if not isinstance(config.get("vlan_map"), dict):
        raise ValueError("VLAN map must be a JSON object keyed by VLAN tag")
    entry_aliases = config.get("entry_aliases")
    if not isinstance(entry_aliases, dict):
        raise ValueError("Per-entry aliases must be a JSON object")
    public_domain = str(config.get("public_domain") or "").strip().lower()
    for key, names in entry_aliases.items():
        instance, separator, hostname = str(key).partition(":")
        if (
            not separator
            or not INSTANCE_NAME_PATTERN.fullmatch(instance)
            or not HOST_LABEL_PATTERN.fullmatch(hostname)
        ):
            raise ValueError(f"Per-entry alias key '{key}' must be '<instance>:<guest-hostname>'")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError(f"Per-entry aliases for '{key}' must be a list of hostnames")
        if names:
            if not DOMAIN_PATTERN.fullmatch(public_domain):
                raise ValueError("Set a valid public hostname domain before saving per-entry aliases")
            normalized = parse_aliases(f"{hostname} = {', '.join(names)}", public_domain)
            if hostname not in normalized:
                raise ValueError(f"Per-entry aliases for '{key}' must include a valid hostname")
    entry_port_overrides = config.get("entry_port_overrides")
    if not isinstance(entry_port_overrides, dict):
        raise ValueError("Per-entry port overrides must be a JSON object")
    for key, port in entry_port_overrides.items():
        instance, separator, hostname = str(key).partition(":")
        if (
            not separator
            or not INSTANCE_NAME_PATTERN.fullmatch(instance)
            or not HOST_LABEL_PATTERN.fullmatch(hostname)
        ):
            raise ValueError(
                f"Per-entry port override key '{key}' must be '<instance>:<guest-hostname>'"
            )
        if isinstance(port, bool) or not (
            isinstance(port, int) or isinstance(port, str) and port.isdecimal()
        ):
            raise ValueError(
                f"Per-entry port override for '{key}' must be an integer from 1 to 65535"
            )
        try:
            numeric_port = int(port)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Per-entry port override for '{key}' must be an integer from 1 to 65535"
            ) from error
        if not 1 <= numeric_port <= 65535:
            raise ValueError(
                f"Per-entry port override for '{key}' must be an integer from 1 to 65535"
            )
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
    if aliases.strip() and DOMAIN_PATTERN.fullmatch(public_domain):
        parse_aliases(aliases, config.get("public_domain"))


def secret_fields(config):
    """(description, secret field) for every instance, and nothing else.

    Only the description reaches a log or a browser; the value never does.
    """
    for instance, entry in (config.get("instances") or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        yield f"NPMPlus instance '{instance}' secret", entry.get("secret")


def public_config(config, environ=None):
    """This destination's settings as they may be sent to the browser.

    An "env:" reference is reported by name plus whether it currently resolves, so
    the page can tell the user which variable to set without ever seeing a secret.
    """
    result = {
        key: value
        for key, value in config.items()
        if key in KEYS and key != "instances" and not key.endswith("_secret")
    }
    result["instances"] = {
        name: {
            "label": entry.get("label", ""),
            "url": entry.get("url", ""),
            "identity": entry.get("identity", ""),
            "secret_configured": bool(resolve_secret(entry.get("secret"), environ)),
            "secret_env": secret_env_name(entry.get("secret")) or "",
            # Reported so the page can restore an endpoint's self-signed switch.
            "verify_tls": as_bool(entry.get("verify_tls"), True),
        }
        for name, entry in (config.get("instances") or {}).items()
    }
    return result
