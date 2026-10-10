import re
from collections import defaultdict


HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
# The VLAN key a guest that states no tag routes by.
UNTAGGED_TAG = "untagged"
# PVE Scripts Local names the script it installed, and those names end in ".sh"
# ("photo-service.sh"): the word before the period is the guest's hostname. The same
# holds for a cached guest hostname that carries the suffix, so the rule is
# applied to whichever name wins, not just to the script-name fallback.
SCRIPT_SUFFIX = ".sh"


def guest_hostname(guest):
    """The guest hostname, using its reported hostname or source name."""
    name = str(guest.get("hostname") or guest.get("name") or "")
    name = name.strip().lower().rstrip(".")
    if name.endswith(SCRIPT_SUFFIX):
        name = name[: -len(SCRIPT_SUFFIX)].strip()
    return name


def vlan_from_address(address):
    """The VLAN tag a guest address carries, or "" when it carries none.

    A PSL deployment whose response has no `lxc_config` still reports the guest's
    web UI address, and a network that segments by VLAN repeats the tag in the
    second and third octet. Only that repeated spelling is read as a tag: anything
    else is left alone rather than guessed at.
    """
    octets = str(address or "").strip().split(".")
    if len(octets) != 4:
        return ""
    try:
        numbers = [int(octet) for octet in octets]
    except ValueError:
        return ""
    if any(number < 0 or number > 255 for number in numbers):
        return ""
    # The second and third octets must repeat, or the VLAN is left unspecified.
    if not numbers[1] or octets[1] != octets[2]:
        return ""
    return str(numbers[1])


def vlan_key_for(guest):
    """The VLAN tag a guest routes by, plus a note when its address supplied it."""
    raw_vlan = guest.get("network_vlan")
    if raw_vlan is not None and str(raw_vlan).strip():
        return str(raw_vlan).strip(), ""
    if guest.get("has_network_config"):
        return UNTAGGED_TAG, ""
    address = str(guest.get("address") or "").strip()
    tag = vlan_from_address(address)
    if tag:
        return tag, f" (from web_ui_ip {address})"
    if address:
        return UNTAGGED_TAG, f" (no lxc_config, and web_ui_ip {address} carries no VLAN)"
    return UNTAGGED_TAG, " (no lxc_config, and no web_ui_ip to read a VLAN from)"


def container_id_of(guest):
    """The container a source record describes, or "" when it states none.

    A record's own `id` identifies the record, not the guest: PSL keeps one record
    per install attempt, so the same container can be described by several records,
    and `container_id` is what ties them together. A record that states no
    container id is left as a guest of its own, because nothing says it shares a
    container with another record.
    """
    return str(guest.get("container_id") or "").strip()


def usable_web_ui_port(guest):
    """The guest service port a record states, or 0 when it cannot route."""
    try:
        port = int(guest.get("port"))
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def has_stored_web_ui_endpoint(guest):
    """Whether a guest has a saved address and valid port after refresh failure."""
    return bool(str(guest.get("address") or "").strip()) and bool(usable_web_ui_port(guest))


def record_priority(guest):
    """Prefer the newest record, without interpreting PSL's install status."""
    try:
        record_id = int(guest.get("id"))
    except (TypeError, ValueError):
        record_id = 0
    return record_id


def parse_aliases(text, public_domain):
    """The extra hostnames a guest's proxy host also serves, from the settings text.

    One guest per line, written `guest = name, name`. A name without a dot is a
    label under the public domain and a name with one is used as it stands, so
    `media-service = photos` gives the guest media-service one host that answers to both
    media-service.<public domain> and photos.<public domain>. Blank lines and anything
    after a # are ignored, and a line that cannot be read is reported rather than
    guessed at: a typo must not quietly drop a name from a host.
    """
    domain = str(public_domain or "").strip().lower().rstrip(".")
    aliases = {}
    for number, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        guest, separator, names = line.partition("=")
        guest = guest.strip().lower().rstrip(".")
        if not separator or not guest or not names.strip():
            raise ValueError(f"Alias line {number} must read 'guest = name, name': {line}")
        if not HOST_LABEL_PATTERN.fullmatch(guest):
            raise ValueError(f"Alias line {number} names guest '{guest}', which is not a guest hostname")
        if guest in aliases:
            raise ValueError(f"Alias line {number} lists guest '{guest}' a second time; keep one line per guest")
        own = f"{guest}.{domain}"
        resolved = []
        for name in names.split(","):
            name = name.strip().lower().rstrip(".")
            if not name:
                continue
            hostname = name if "." in name else f"{name}.{domain}"
            if not DOMAIN_PATTERN.fullmatch(hostname):
                raise ValueError(f"Alias line {number} names '{name}', which is not a hostname")
            if hostname == own:
                raise ValueError(f"Alias line {number} repeats the guest's own hostname {hostname}")
            if hostname not in resolved:
                resolved.append(hostname)
        if not resolved:
            raise ValueError(f"Alias line {number} lists no alias name")
        aliases[guest] = resolved
    return aliases


def normalize_aliases(aliases, public_domain):
    """The alias map as hostnames, whether the settings text or a mapping was given.

    A mapping is read as it stands - its names are resolved the same way, and an
    entry that names no usable hostname is dropped - so a caller that already
    validated the settings text does not have to render it again.
    """
    if not isinstance(aliases, dict):
        return parse_aliases(aliases, public_domain)
    domain = str(public_domain or "").strip().lower().rstrip(".")
    normalized = {}
    for raw_guest, raw_names in aliases.items():
        guest = str(raw_guest or "").strip().lower().rstrip(".")
        if not HOST_LABEL_PATTERN.fullmatch(guest):
            continue
        own = f"{guest}.{domain}"
        names = raw_names if isinstance(raw_names, (list, tuple, set)) else str(raw_names).split(",")
        resolved = []
        for raw_name in names:
            name = str(raw_name or "").strip().lower().rstrip(".")
            if not name:
                continue
            hostname = name if "." in name else f"{name}.{domain}"
            if not DOMAIN_PATTERN.fullmatch(hostname) or hostname == own or hostname in resolved:
                continue
            resolved.append(hostname)
        if resolved:
            normalized[guest] = resolved
    return normalized


def plan_hosts(scripts, vlan_map, public_domain, exclusions=None, aliases=None, instance_aliases=None):
    exclusions = {str(name).strip().lower() for name in (exclusions or set()) if str(name).strip()}
    normalized_domain = str(public_domain or "").strip().lower()
    if not DOMAIN_PATTERN.fullmatch(normalized_domain):
        raise ValueError("PUBLIC_DOMAIN must be a valid DNS domain name")
    # Extra names a guest's host answers to. They are part of the guest's own host,
    # not guests of their own, so a name is only ever written to one host.
    alias_names = normalize_aliases(aliases, normalized_domain)
    scoped_alias_names = {}
    scoped_alias_keys = set()
    if isinstance(instance_aliases, dict):
        for key, names in instance_aliases.items():
            instance, separator, hostname = str(key).partition(":")
            if not separator:
                continue
            scoped = normalize_aliases({hostname: names}, normalized_domain)
            route_key = (instance.strip().lower(), hostname)
            scoped_alias_keys.add(route_key)
            scoped_alias_names[route_key] = scoped.get(hostname, [])

    # PSL keeps one record per install attempt, so one container can be described
    # by several records. Reduce each container to the record that describes it
    # best, so a container is planned once, from its own record, and never from a
    # stale one. A record that states no container id cannot be tied to another
    # record, so it stands alone rather than being merged by name.
    records_by_container = {}
    container_keys = set()
    for index, guest in enumerate(scripts):
        key = container_id_of(guest) or f"record {index}"
        container_keys.add(key)
        incumbent = records_by_container.get(key)
        if incumbent is None or record_priority(guest) > record_priority(incumbent):
            records_by_container[key] = guest

    planned = []
    skipped = []
    present = set()
    # Every name this run knows for a guest, keyed by the guest's own domain. A host
    # that serves a name outside its own set is never deleted, so this is also what
    # keeps a host carrying an unknown extra name from being swept away.
    known_names = {}
    candidates_by_domain = defaultdict(list)

    for guest in records_by_container.values():
        hostname = guest_hostname(guest)
        names = []

        # Every container PSL still knows about counts as "present", even if it is
        # not routable right now. Only containers that vanish from PSL are pruned,
        # and a record that lost to another record of its own container is not a
        # container at all. A guest's aliases count as present too, so a host that
        # serves one is kept while the guest that owns it is still here.
        if HOST_LABEL_PATTERN.fullmatch(hostname):
            names = [f"{hostname}.{normalized_domain}", *alias_names.get(hostname, [])]
            present.update(names)
            known_names[names[0]] = names

        refresh_error = guest.get("refresh_error")
        if refresh_error and not has_stored_web_ui_endpoint(guest):
            skipped.append((
                hostname or guest.get("name") or container_id_of(guest) or "unknown",
                refresh_error,
            ))
            continue
        if not container_id_of(guest):
            label = hostname or guest.get("name") or "unknown"
            skipped.append((label, "PSL record has no container ID"))
            continue

        if hostname in exclusions:
            skipped.append((hostname or guest.get("name", "unknown"), "excluded proxy guest"))
            continue
        if guest.get("is_vm"):
            skipped.append((hostname or guest.get("name", "unknown"), "VM VLAN config is not exposed by PVE Scripts Local"))
            continue
        if not HOST_LABEL_PATTERN.fullmatch(hostname):
            skipped.append((hostname or guest.get("name") or "unknown", "missing or invalid guest hostname"))
            continue

        vlan_key, vlan_note = vlan_key_for(guest)
        mapping = vlan_map.get(vlan_key)
        instance = str(mapping.get("instance") or "").strip().lower() if isinstance(mapping, dict) else ""
        if not instance:
            skipped.append((hostname, f"no NPMPlus instance mapped for VLAN {vlan_key}{vlan_note}"))
            continue

        zone = str(mapping.get("zone", "")).strip().lower().rstrip(".")
        if not DOMAIN_PATTERN.fullmatch(zone):
            skipped.append((hostname, f"invalid upstream DNS zone for VLAN {vlan_key}"))
            continue

        route_key = (instance, hostname)
        scoped_names = scoped_alias_names.get(route_key, [])
        if route_key in scoped_alias_keys:
            names = [f"{hostname}.{normalized_domain}", *scoped_names]
        present.update(scoped_names)
        known_names[f"{hostname}.{normalized_domain}"] = names

        port = usable_web_ui_port(guest)
        if not port:
            skipped.append((hostname, "web_ui_port is missing or invalid"))
            continue

        candidate = {
            "source_id": str(guest.get("id", "")),
            "container_id": container_id_of(guest),
            "guest_hostname": hostname,
            "instance": instance,
            "domain_name": f"{hostname}.{normalized_domain}",
            # The guest's own name first, then the aliases configured for it, so the
            # first name stays the name this app records the host under.
            "domain_names": list(names),
            "forward_scheme": "http",
            "forward_host": f"{hostname}.{zone}",
            "forward_port": port,
        }
        candidates_by_domain[candidate["domain_name"]].append(candidate)

    for domain_name, candidates in candidates_by_domain.items():
        if len(candidates) > 1:
            for candidate in candidates:
                skipped.append((candidate["guest_hostname"], f"duplicate hostname {domain_name}"))
            continue
        planned.append(candidates[0])

    planned.sort(key=lambda item: (item["instance"], item["domain_name"]))

    # A name belongs to one host only: the guest whose hostname it is keeps it, and
    # every other claim on that name is reported instead of being written, so two
    # hosts never answer to the same name.
    claimed = {row["domain_name"]: row["guest_hostname"] for row in planned}
    for row in planned:
        accepted = [row["domain_name"]]
        for name in row["domain_names"]:
            if name == row["domain_name"]:
                continue
            owner = claimed.get(name)
            if owner is None:
                claimed[name] = row["guest_hostname"]
                accepted.append(name)
                continue
            skipped.append((row["guest_hostname"], f"alias {name} is already served by guest {owner}"))
        row["domain_names"] = accepted
        known_names[row["domain_name"]] = accepted

    skipped.sort(key=lambda item: (item[0], item[1]))
    return {
        "plan": planned,
        "skipped": skipped,
        "present": present,
        # What each guest's host must answer to, so a host serving a name this run
        # does not know for that guest is never deleted by itself.
        "names_by_domain": known_names,
        # Guests are counted per container, not per record: a container PSL
        # describes with several records is still one guest.
        "containers": len(container_keys),
    }


def parse_exclusions(names=""):
    return {name.strip().lower() for name in str(names or "").split(",") if name.strip()}