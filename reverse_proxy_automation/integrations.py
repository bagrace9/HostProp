"""Adapters between the PSL source model and destination systems."""

from typing import Protocol

from .planner import parse_exclusions, plan_hosts


class SourceAdapter(Protocol):
    def read(self, config, refresh_lxc_config=False, progress_callback=None):
        """Return source-neutral guest records."""


class DestinationAdapter(Protocol):
    key: str
    label: str

    def plan(self, guests, config):
        """Build a destination-specific plan from source-neutral guest records."""

    def apply(self, plan, config, state, apply_changes=False):
        """Apply or preview a plan, returning (status, target, resource, details) rows.

        The persisted state is shared; new destinations should keep their ownership
        records in a namespace keyed by their adapter key.
        """


class PslSourceAdapter:
    """Read PSL records and translate them into the shared guest-record shape."""

    def __init__(self, client_factory):
        self.client_factory = client_factory

    def read(self, config, refresh_lxc_config=False, progress_callback=None):
        records = self.client_factory(config).installed_scripts(
            refresh_lxc_config=refresh_lxc_config,
            progress_callback=progress_callback,
        )
        return [normalize_psl_record(record) for record in records]


def normalize_psl_record(record):
    """Map a PSL install record to fields that describe a guest, not a PSL response."""
    if not isinstance(record, dict):
        raise ValueError("PVE Scripts Local returned an installed record that is not an object")
    network_config = record.get("lxc_config")
    network_config = network_config if isinstance(network_config, dict) else {}
    return {
        "id": record.get("id"),
        "name": str(record.get("script_name") or ""),
        "container_id": str(record.get("container_id") or "").strip(),
        "hostname": str(network_config.get("hostname") or ""),
        "network_vlan": network_config.get("net_vlan"),
        "has_network_config": bool(network_config),
        "address": str(record.get("web_ui_ip") or "").strip(),
        "port": record.get("web_ui_port"),
        "is_vm": bool(record.get("is_vm")),
        "refresh_error": str(record.get("_refresh_error") or ""),
    }


class NpmPlusDestinationAdapter:
    """Plan and reconcile proxy hosts while keeping NPMPlus details destination-local."""

    key = "npmplus"
    label = "NPMPlus"

    def __init__(self, client_factory, reconcile_func):
        self.client_factory = client_factory
        self.reconcile = reconcile_func

    def plan(self, guests, config):
        result = plan_hosts(
            guests,
            config["vlan_map"],
            config["public_domain"],
            parse_exclusions(config["excluded_guest_hostnames"]),
            config.get("aliases"),
            instance_aliases=config.get("entry_aliases"),
        )
        for host in result["plan"]:
            override = (config.get("entry_port_overrides") or {}).get(
                f"{host['instance']}:{host['guest_hostname']}"
            )
            if override is not None:
                host["forward_port"] = int(override)
        return result

    def apply(self, plan, config, state, apply_changes=False):
        allow_delete = bool(config.get("delete_removed_hosts", False))
        planned_instances = {host["instance"] for host in plan["plan"]}
        needed = set(planned_instances)
        instances = config.get("instances") or {}
        if allow_delete:
            # An instance may need visiting only to delete hosts for removed guests.
            needed |= {
                key.partition(":")[0] for key in state["managed"]
            } & set(instances)

        clients = {}
        for instance in sorted(needed):
            entry = instances.get(instance) or {}
            if not entry.get("url"):
                if instance in planned_instances:
                    raise ValueError(
                        f"Set the URL for NPMPlus instance '{instance}' before syncing"
                    )
                continue
            clients[instance] = self.client_factory(instance, entry)

        return self.reconcile(
            plan["plan"],
            clients,
            state,
            apply_changes=apply_changes,
            present_domains=plan["present"],
            allow_delete=allow_delete,
            max_deletes=config["max_deletes_per_run"] or 0,
            planned_names=plan["names_by_domain"],
        )
