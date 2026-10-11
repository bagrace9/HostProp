"""Reconciling a plan against the proxy hosts an NPMPlus instance already holds.

This module was the core's sync.py. It is NPMPlus's own logic - it speaks about
proxy hosts, certificates and NPMPlus ownership keys - so it lives with the
destination that uses it. ``state`` here is the plugin's slice of the state
file, not the whole file; see ``state.py``.
"""

from concurrent.futures import ThreadPoolExecutor

from ...clients import ApiError
from .client import select_shared_certificate
from .state import adopted_host_ids


CHANGED_OK = {"OK", "CREATE", "UPDATE", "DRY-CREATE", "DRY-UPDATE"}

PRESERVED_FIELDS = (
    "locations",
    "advanced_config",
    "npmplus_http3_support",
    "npmplus_access_list_ids",
    "npmplus_access_list_type",
    "npmplus_noindex",
    "npmplus_crowdsec_appsec",
    "npmplus_proxy_request_buffering",
    "npmplus_proxy_response_buffering",
    "npmplus_upstream_compression",
    "npmplus_fancyindex",
    "npmplus_x_frame_options",
    "npmplus_auth_request",
    "npmplus_auth_request_upstream",
    "npmplus_location_config",
    "npmplus_directory",
    "npmplus_mtls_certificate_id",
    "npmplus_mtls_verify_client_optional",
    "npmplus_reuse_key",
    "npmplus_dns_challenge",
    "npmplus_dns_provider",
    "npmplus_dns_provider_credentials",
    "npmplus_propagation_seconds",
)


def proxy_host_payload(host, certificate_id):
    return {
        # One host may answer to several names: the guest's hostname first, then any
        # alias configured for it.
        "domain_names": [
            str(name).lower() for name in (host.get("domain_names") or [host["domain_name"]])
        ],
        "forward_scheme": host["forward_scheme"],
        "forward_host": host["forward_host"],
        "forward_port": host["forward_port"],
        "certificate_id": certificate_id,
        "ssl_forced": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
    }


def _domain_map(proxy_hosts):
    by_domain = {}
    for existing in proxy_hosts:
        for domain in existing.get("domain_names", []):
            by_domain.setdefault(str(domain).lower(), []).append(existing)
    return by_domain


def reconcile(
    plan,
    clients,
    state,
    apply_changes=False,
    present_domains=None,
    allow_delete=False,
    max_deletes=None,
    planned_names=None,
):
    """Create/update planned hosts and, optionally, delete hosts this app created.

    Only hosts recorded in state["managed"] are ever deleted, and only when they
    serve no name beyond the ones planned_names lists for their guest. A host the
    user adopted is updated like one this app created, but is never deleted by
    itself. present_domains is the set of public domains for every guest PSL still
    reports; deletion is disabled when it is None.
    """
    managed = state.setdefault("managed", {})
    results = []
    snapshots = {}
    plans_by_instance = {name: [] for name in clients}
    for host in plan:
        plans_by_instance.setdefault(host["instance"], []).append(host)
    prune_enabled = allow_delete and present_domains is not None
    managed_by_instance = {}
    for key in managed:
        managed_by_instance.setdefault(key.partition(":")[0], True)

    active_instances = [
        (instance, instance_hosts)
        for instance, instance_hosts in plans_by_instance.items()
        if instance_hosts or (prune_enabled and instance in managed_by_instance)
    ]
    clients_to_read = [
        (instance, instance_hosts, clients[instance])
        for instance, instance_hosts in active_instances
        if clients.get(instance) is not None
    ]

    def read_instance(instance, instance_hosts, client):
        try:
            current_hosts = client.proxy_hosts()
            certificates = client.certificates() if instance_hosts else []
            return instance, current_hosts, certificates, None
        except ApiError as error:
            return instance, [], [], error

    if len(clients_to_read) > 1:
        with ThreadPoolExecutor(max_workers=min(len(clients_to_read), 8)) as executor:
            snapshots_read = list(executor.map(
                lambda row: read_instance(*row),
                clients_to_read,
            ))
    else:
        snapshots_read = [read_instance(*row) for row in clients_to_read]
    instance_snapshots = {
        instance: (current_hosts, certificates, error)
        for instance, current_hosts, certificates, error in snapshots_read
    }

    for instance, instance_hosts in active_instances:
        if not instance_hosts and not (prune_enabled and instance in managed_by_instance):
            continue
        client = clients.get(instance)
        if client is None:
            # The plan named an instance the caller handed in no client for. Report it
            # rather than raising, and leave those hosts and their state untouched.
            for host in instance_hosts:
                results.append(("ERROR", instance, host["domain_name"], "no NPMPlus client configured for this instance"))
            continue
        current_hosts, certificates, error = instance_snapshots[instance]
        if error is not None:
            results.append(("ERROR", instance, f"({instance} NPMPlus)", str(error)))
            continue
        snapshots[instance] = current_hosts
        domains = _domain_map(current_hosts)
        adopted_ids = adopted_host_ids(state, instance)

        for host in instance_hosts:
            domain_name = host["domain_name"].lower()
            # The guest's own name first, then its aliases, all lowercase.
            names = [domain_name]
            for name in host.get("domain_names") or []:
                name = str(name).lower()
                if name not in names:
                    names.append(name)
            ownership_key = f"{instance}:{domain_name}"
            owned_id = managed.get(ownership_key)
            matches = domains.get(domain_name, [])

            if len(matches) > 1:
                results.append(("CONFLICT", instance, domain_name, "multiple existing proxy hosts use this domain"))
                continue

            existing = matches[0] if matches else None
            existing_id = str(existing.get("id")) if existing else ""
            if existing and existing_id != str(owned_id) and existing_id not in adopted_ids:
                results.append((
                    "CONFLICT",
                    instance,
                    domain_name,
                    f"existing host {existing_id} is not recorded as automation-managed; adopt it in Host "
                    "management to let this run update it",
                ))
                continue

            # A host answers to the guest's name and its aliases, and every other
            # name has to be free or already on this very host: taking one over from
            # another entry would leave two hosts answering the same name.
            blocked = None
            for name in names[1:]:
                for occupant in domains.get(name, []):
                    if existing is not None and str(occupant.get("id")) == existing_id:
                        continue
                    blocked = (name, str(occupant.get("id")))
                    break
                if blocked:
                    break
            if blocked:
                results.append((
                    "CONFLICT",
                    instance,
                    domain_name,
                    f"alias {blocked[0]} is already served by proxy host {blocked[1]}; release or delete that "
                    "entry in Host management to give this host the name",
                ))
                continue

            certificate = select_shared_certificate(certificates, names)
            if not certificate:
                results.append((
                    "SKIP",
                    instance,
                    domain_name,
                    f"no matching unexpired certificate on this NPMPlus covers {', '.join(names)}",
                ))
                continue

            payload = proxy_host_payload(host, certificate["id"])
            if existing:
                # Preserve settings the automation does not manage (access lists,
                # custom locations, advanced config) so PUT does not reset them.
                for key in PRESERVED_FIELDS:
                    if key in existing:
                        payload[key] = existing[key]
                served = {str(name).lower() for name in existing.get("domain_names", [])}
                unchanged = set(names) == served and all(
                    existing.get(field) == payload[field]
                    for field in ("forward_scheme", "forward_host", "forward_port", "certificate_id", "ssl_forced")
                )
                if unchanged:
                    results.append(("OK", instance, domain_name, f"already points to {host['forward_host']}:{host['forward_port']}"))
                    continue
                action = "UPDATE"
                host_id = existing["id"]
            else:
                action = "CREATE"
                host_id = None

            details = f"{host['forward_scheme']}://{host['forward_host']}:{host['forward_port']}"
            if apply_changes:
                try:
                    saved = client.update_proxy_host(host_id, payload) if host_id else client.create_proxy_host(payload)
                except ApiError as error:
                    results.append(("ERROR", instance, domain_name, str(error)))
                    continue
                saved_id = saved.get("id") if isinstance(saved, dict) else None
                if saved_id is None:
                    results.append(("ERROR", instance, domain_name, "NPMPlus response did not include a host ID"))
                    continue
                # An adopted host stays adopted: recording it as created here would
                # let a later run delete it once its guest is gone.
                if str(saved_id) not in adopted_ids:
                    managed[ownership_key] = str(saved_id)
                results.append((action, instance, domain_name, details))
            else:
                results.append((f"DRY-{action}", instance, domain_name, details))

    if prune_enabled:
        results.extend(
            _prune(
                plan,
                clients,
                state,
                snapshots,
                results,
                present_domains,
                apply_changes,
                max_deletes,
                planned_names,
            )
        )
    return results


def _prune(
    plan,
    clients,
    state,
    snapshots,
    results,
    present_domains,
    apply_changes,
    max_deletes,
    planned_names=None,
):
    """Delete managed proxy hosts whose guest is gone (or moved to the other instance)."""
    managed = state["managed"]
    if not managed:
        return []
    if not present_domains:
        return [("ERROR", "", "(cleanup)", "PSL reported no usable guests; refusing to delete managed hosts")]

    desired_keys = {f"{host['instance']}:{host['domain_name'].lower()}" for host in plan}
    ready_instances_by_domain = {}
    for row in results:
        if row[0] in CHANGED_OK:
            ready_instances_by_domain.setdefault(row[2], set()).add(row[1])

    candidates = []
    for key, host_id in sorted(managed.items()):
        instance, _, domain = key.partition(":")
        if key in desired_keys or instance not in snapshots:
            continue
        moved = any(other != instance for other in ready_instances_by_domain.get(domain, ()))
        if domain in present_domains and not moved:
            continue  # guest still exists but is not routable right now; leave the host alone
        candidates.append((key, instance, domain, host_id, "moved to the other NPMPlus" if moved else "guest no longer in PSL"))

    if not candidates:
        return []
    if max_deletes is None:
        return [("ERROR", "", "(cleanup)", "Set a deletion limit before enabling cleanup")]
    if len(candidates) > max_deletes:
        return [(
            "ERROR", "", "(cleanup)",
            f"{len(candidates)} deletions exceed the limit of {max_deletes}; nothing deleted. Raise the limit if this is intended.",
        )]

    rows = []
    host_by_id = {
        instance: {
            str(host.get("id")): host
            for host in hosts
        }
        for instance, hosts in snapshots.items()
    }
    for key, instance, domain, host_id, reason in candidates:
        existing = host_by_id[instance].get(str(host_id))
        if existing is None:
            if apply_changes:
                managed.pop(key, None)
            rows.append(("OK", instance, domain, "host already removed from NPMPlus; state entry cleared"))
            continue
        names = {str(name).lower() for name in existing.get("domain_names", [])}
        if domain not in names:
            rows.append(("CONFLICT", instance, domain, "recorded host ID no longer serves this domain; not deleting"))
            continue
        expected = {domain}
        for name in (planned_names or {}).get(domain, []):
            expected.add(str(name).lower())
        if names - expected:
            rows.append((
                "CONFLICT",
                instance,
                domain,
                "managed host serves a name this app does not know for that guest; not deleting it - "
                "delete it in Host management if it should go",
            ))
            continue
        if not apply_changes:
            rows.append(("DRY-DELETE", instance, domain, reason))
            continue
        try:
            clients[instance].delete_proxy_host(host_id)
        except ApiError as error:
            rows.append(("ERROR", instance, domain, str(error)))
            continue
        managed.pop(key, None)
        rows.append(("DELETE", instance, domain, reason))
    return rows
