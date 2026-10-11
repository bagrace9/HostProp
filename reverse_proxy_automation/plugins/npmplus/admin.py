"""The server side of NPMPlus's own dashboard panel.

Every action below was a hard-coded route on the core's web server
(``/api/entries/aliases`` and friends). They are the destination's vocabulary -
"instance", "proxy host ID", "ownership" - so they now travel as one
``/api/destinations/npmplus`` request naming an action, and the core forwards the
payload without understanding a word of it.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from ...clients import ApiError
from ...planner import HOST_LABEL_PATTERN, parse_aliases
from ...settings import check_secret_references
from ...statefile import load_state, save_state
from ..base import UnknownTarget
from .config import instance_label, secret_fields
from .state import adopt_host, forget_host


# Each action the panel may call, with the payload keys it is given, in order.
# A page cannot reach a method that is not listed here.
ACTIONS = {
    "test_connection": ("target",),
    "list_entries": (),
    "aliases": ("instance", "host_id", "aliases"),
    "port": ("instance", "host_id", "port"),
    "ownership": ("instance", "host_id", "action"),
    "delete": ("instance", "host_id"),
}


class NpmPlusAdmin:
    """The actions the NPMPlus panel calls, one method per action name."""

    def __init__(self, plugin):
        self.plugin = plugin

    def invoke(self, action, payload=None):
        """Run one listed action, or refuse anything else by name."""
        name = str(action or "").strip()
        if name not in ACTIONS:
            raise ValueError(f"Unknown action '{name}' for destination '{self.plugin.key}'")
        payload = payload if isinstance(payload, dict) else {}
        return getattr(self, name)(*(payload.get(key) for key in ACTIONS[name]))

    # --- credentials ----------------------------------------------------------

    def test_connection(self, target):
        """Check the saved credentials of one configured NPMPlus instance."""
        instance = str(target or "").strip().lower()
        instances = self.plugin.config_store.load().get("instances") or {}
        if instance not in instances:
            # Not ours: another destination may know this name.
            raise UnknownTarget(f"Unknown test target '{target}'")
        entry = instances[instance]
        if not entry.get("url"):
            raise ValueError(f"Set the URL for NPMPlus instance '{instance}' first")
        # Only this target's reference is required to pass, so an unfinished
        # instance elsewhere in the file cannot block a working test.
        check_secret_references(secret_fields({"instances": {instance: entry}}))
        client = self.plugin.client_factory(instance, entry)
        hosts = client.proxy_hosts()
        certificates = client.certificates()
        return {
            "ok": True,
            "message": (
                f"{instance_label(instance, entry)} reachable: "
                f"{len(hosts)} proxy host(s), {len(certificates)} certificate(s)."
            ),
        }

    def list_entries(self):
        """Every proxy host on every configured instance, and how this app treats it.

        Read-only: it builds a client per instance and never touches the state file,
        so the page can show what an instance holds without starting a run. An
        instance that cannot be reached is reported in its own row instead of
        failing the whole list.
        """
        config = self.plugin.config_store.load()
        state = load_state(self.plugin.state_path)
        legacy_aliases = parse_aliases(config.get("aliases", ""), config.get("public_domain")) \
            if str(config.get("aliases") or "").strip() else {}
        instances = []
        configured_instances = sorted(config["instances"].items())
        clients = {
            instance: self.plugin.client_factory(instance, entry)
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
        for key, host_id in self.plugin.state_records(state)["managed"].items():
            instance = key.partition(":")[0]
            if instance in managed_by_instance:
                managed_by_instance[instance].add(str(host_id))
        adopted_by_instance = {
            instance: self.plugin.adopted_host_ids(state, instance)
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
                configured_aliases = (config.get("entry_aliases") or {}).get(
                    f"{instance}:{guest_hostname}"
                )
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
                    "port_override": (config.get("entry_port_overrides") or {}).get(
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

    # --- saved per-entry settings ---------------------------------------------

    def aliases(self, instance, host_id, aliases):
        """Save aliases for one guest route without changing another route's names."""
        instance = str(instance or "").strip().lower()
        if not isinstance(aliases, list) or any(not isinstance(name, str) for name in aliases):
            raise ValueError("Aliases must be a list of hostnames")
        with self._state_lock():
            config = self.plugin.config_store.load()
            entry, host, _ = self._known_host(instance, host_id)
            guest_hostname = _guest_hostname(host, instance, entry, host_id)
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
            self.plugin.config_store.save({"entry_aliases": alias_map})
            self.plugin.wake()
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

    def port(self, instance, host_id, port):
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
            config = self.plugin.config_store.load()
            entry, host, _ = self._known_host(instance, host_id)
            guest_hostname = _guest_hostname(host, instance, entry, host_id)
            overrides = dict(config.get("entry_port_overrides") or {})
            key = f"{instance}:{guest_hostname}"
            if numeric_port is None:
                overrides.pop(key, None)
            else:
                overrides[key] = numeric_port
            self.plugin.config_store.save({"entry_port_overrides": overrides})
            self.plugin.wake()
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

    # --- ownership and deletion -----------------------------------------------

    def ownership(self, instance, host_id, action):
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
            state = load_state(self.plugin.state_path)
            if action == "adopt":
                adopt_host(state, instance, host["id"])
            else:
                forget_host(state, instance, host["id"])
            save_state(self.plugin.state_path, state)
        label = instance_label(instance, entry)
        message = (
            f"{host['id']} on {label} is adopted: a matching guest updates it, and it is never deleted on its own."
            if action == "adopt"
            else f"{host['id']} on {label} is no longer owned by this app; it is left alone until you adopt it again."
        )
        return {"ok": True, "message": message, "ownership": "adopted" if action == "adopt" else "unmanaged"}

    def delete(self, instance, host_id):
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
            state = load_state(self.plugin.state_path)
            forget_host(state, instance, host["id"])
            save_state(self.plugin.state_path, state)
        return {"ok": True, "message": f"Deleted {names} from {label}."}

    # --- helpers --------------------------------------------------------------

    def _known_host(self, instance, host_id):
        """The saved instance entry and the NPMPlus host it holds right now.

        The ID is checked against what the instance holds at this moment, so a page
        left open cannot change or delete an entry that has since been replaced.
        """
        entry = (self.plugin.config_store.load().get("instances") or {}).get(instance)
        if not isinstance(entry, dict):
            raise ValueError(f"NPMPlus instance '{instance}' is not configured")
        if not entry.get("url"):
            raise ValueError(f"No URL is saved for {instance_label(instance, entry)}")
        wanted = str(host_id or "").strip()
        client = self.plugin.client_factory(instance, entry)
        for host in client.proxy_hosts(expand_certificate=False):
            if str(host.get("id")) == wanted:
                return entry, host, client
        raise ValueError(
            f"Proxy host {wanted or '(none)'} is not on {instance_label(instance, entry)}; reload the entries list"
        )

    @contextmanager
    def _state_lock(self):
        """Hold the run lock, so a manual state change cannot race a sync run."""
        if not self.plugin.lock.acquire(blocking=False):
            raise ValueError("A sync run is in progress; try again once it finishes")
        try:
            yield
        finally:
            self.plugin.lock.release()


def _guest_hostname(host, instance, entry, host_id):
    """The guest a proxy host forwards to, or an error naming the host instead."""
    guest_hostname = (
        str(host.get("forward_host") or "").strip().lower().rstrip(".").split(".", 1)[0]
    )
    if not HOST_LABEL_PATTERN.fullmatch(guest_hostname):
        raise ValueError(
            f"Cannot identify the guest hostname for host {host_id} on {instance_label(instance, entry)}"
        )
    return guest_hostname

