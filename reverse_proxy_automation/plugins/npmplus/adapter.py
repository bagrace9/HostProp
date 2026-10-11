"""NPMPlus as a destination: plan guests into proxy hosts, then reconcile them.

This is the plan/apply half of the plugin. It used to be
``integrations.NpmPlusDestinationAdapter``, which the service built by handing it a
client factory and the core's reconciler. It now builds its own clients and writes
its own records, and nothing outside this package builds a NPMPlus client.
"""

from ...planner import parse_exclusions, plan_hosts
from ...settings import as_bool, resolve_secret
from .client import NpmPlusClient
from .config import instance_label
from .reconcile import reconcile
from .state import records


class NpmPlusDestination:
    """Plan and reconcile proxy hosts while keeping NPMPlus details destination-local."""

    key = "npmplus"
    label = "NPMPlus"

    @staticmethod
    def client_factory(instance, entry):
        """The client for one configured instance, built from its saved settings."""
        return NpmPlusClient(
            instance_label(instance, entry),
            entry.get("url", ""),
            entry.get("identity", ""),
            # A stored "env:VAR" reference is swapped for its value only here, so
            # neither the settings file nor the browser ever holds the password.
            resolve_secret(entry.get("secret", "")),
            # Only this instance stops verifying, and only when its switch is off.
            verify_tls=as_bool(entry.get("verify_tls"), True),
        )

    def plan(self, guests, config):
        """Proxy hosts for the guests PSL reports, mapped through the VLAN table."""
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
        """Apply or preview the plan, keeping this app's records in its own slice."""
        slice_ = records(state)
        allow_delete = bool(config.get("delete_removed_hosts", False))
        planned_instances = {host["instance"] for host in plan["plan"]}
        needed = set(planned_instances)
        instances = config.get("instances") or {}
        if allow_delete:
            # An instance may need visiting only to delete hosts for removed guests.
            needed |= {key.partition(":")[0] for key in slice_["managed"]} & set(instances)

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

        return reconcile(
            plan["plan"],
            clients,
            slice_,
            apply_changes=apply_changes,
            present_domains=plan["present"],
            allow_delete=allow_delete,
            max_deletes=config["max_deletes_per_run"] or 0,
            planned_names=plan["names_by_domain"],
        )
