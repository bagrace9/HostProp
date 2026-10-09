import unittest

from reverse_proxy_automation.sync import (
    adopt_host,
    adopted_host_ids,
    forget_host,
    ownership_of,
    reconcile,
)


class FakeNpmPlus:
    def __init__(self, hosts=None):
        self.hosts = hosts or []
        self.cert_list = [{"id": 7, "domain_names": ["*.example.net"]}]
        self.created = []
        self.updated = []
        self.deleted = []

    def proxy_hosts(self):
        return self.hosts

    def certificates(self):
        return self.cert_list

    def create_proxy_host(self, payload):
        self.created.append(payload)
        return {"id": 42}

    def update_proxy_host(self, host_id, payload):
        self.updated.append((host_id, payload))
        return {"id": host_id}

    def delete_proxy_host(self, host_id):
        self.deleted.append(str(host_id))
        self.hosts = [host for host in self.hosts if str(host.get("id")) != str(host_id)]


def planned_host():
    return {
        "source_id": "12",
        "guest_hostname": "photos",
        "instance": "lab",
        "domain_name": "photos.example.net",
        "forward_scheme": "http",
        "forward_host": "photos.internal.example.net",
        "forward_port": 8081,
    }


class ReconcileTests(unittest.TestCase):
    NPMPLUS_PROXY_HOST_FIELDS = {
        "domain_names",
        "forward_scheme",
        "forward_host",
        "forward_port",
        "certificate_id",
        "ssl_forced",
        "hsts_enabled",
        "hsts_subdomains",
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
        "advanced_config",
        "npmplus_location_config",
        "npmplus_directory",
        "npmplus_mtls_certificate_id",
        "npmplus_mtls_verify_client_optional",
        "npmplus_reuse_key",
        "npmplus_dns_challenge",
        "npmplus_dns_provider",
        "npmplus_dns_provider_credentials",
        "npmplus_propagation_seconds",
        "locations",
    }

    def test_preview_does_not_create_or_claim_host(self):
        client = FakeNpmPlus()
        state = {"managed": {}}
        result = reconcile([planned_host()], {"lab": client}, state)
        self.assertEqual(result[0][0], "DRY-CREATE")
        self.assertEqual(client.created, [])
        self.assertEqual(state["managed"], {})

    def test_create_payload_contains_only_fields_accepted_by_npmplus(self):
        client = FakeNpmPlus()
        reconcile([planned_host()], {"lab": client}, {"managed": {}}, apply_changes=True)
        self.assertTrue(client.created)
        self.assertLessEqual(set(client.created[0]), self.NPMPLUS_PROXY_HOST_FIELDS)

    def test_apply_creates_and_tracks_host_id(self):
        client = FakeNpmPlus()
        state = {"managed": {}}
        result = reconcile([planned_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "CREATE")
        self.assertEqual(state["managed"]["lab:photos.example.net"], "42")
        self.assertEqual(client.created[0]["certificate_id"], 7)

    def test_existing_manual_host_is_never_overwritten(self):
        client = FakeNpmPlus(hosts=[{
            "id": 21,
            "domain_names": ["photos.example.net"],
            "forward_host": "manually-configured.internal.example.net",
        }])
        state = {"managed": {}}
        result = reconcile([planned_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "CONFLICT")
        self.assertEqual(client.updated, [])
        self.assertEqual(client.created, [])

    def test_plan_for_an_instance_without_a_client_is_reported_not_raised(self):
        state = {"managed": {}}
        result = reconcile([planned_host()], {}, state, apply_changes=True)
        self.assertEqual(result[0][0], "ERROR")
        self.assertEqual(result[0][1], "lab")
        self.assertEqual(state["managed"], {})

    def test_managed_hosts_are_left_alone_when_their_instance_has_no_client(self):
        state = {"managed": {"dmz:gone.example.net": "5"}}
        result = reconcile(
            [],
            {"lab": FakeNpmPlus()},
            state,
            present_domains={"photos.example.net"},
            allow_delete=True,
            apply_changes=True,
        )
        self.assertEqual(result, [])
        self.assertEqual(state["managed"], {"dmz:gone.example.net": "5"})


def managed_host(domain="photos.example.net", certificate_id=7):
    """An NPMPlus host as the API returns it, already pointing where the plan wants."""
    return {
        "id": 42,
        "domain_names": [domain],
        "forward_scheme": "http",
        "forward_host": "photos.internal.example.net",
        "forward_port": 8081,
        "certificate_id": certificate_id,
        "ssl_forced": True,
    }


class AdoptedHostTests(unittest.TestCase):
    """A host the user adopted is updated like one this app created, but never deleted."""

    def test_an_adopted_host_is_updated_instead_of_conflicting(self):
        client = FakeNpmPlus(hosts=[{
            "id": 21,
            "domain_names": ["photos.example.net"],
            "forward_host": "manually-configured.internal.example.net",
        }])
        state = {"managed": {}, "adopted": {"lab:21": True}}
        result = reconcile([planned_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "UPDATE")
        self.assertEqual(client.updated[0][0], 21)
        self.assertEqual(client.updated[0][1]["forward_host"], "photos.internal.example.net")
        # Adoption hands the host over for updates; it does not make it deletable.
        self.assertEqual(state["managed"], {})

    def test_a_host_that_is_neither_managed_nor_adopted_is_not_overwritten(self):
        client = FakeNpmPlus(hosts=[{"id": 21, "domain_names": ["photos.example.net"]}])
        state = {"managed": {}, "adopted": {}}
        result = reconcile([planned_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "CONFLICT")
        self.assertIn("adopt it in Host management", result[0][3])
        self.assertEqual(client.updated, [])

    def test_an_adopted_host_is_never_deleted_by_a_run(self):
        client = FakeNpmPlus(hosts=[{"id": 21, "domain_names": ["photos.example.net"]}])
        state = {"managed": {}, "adopted": {"lab:21": True}}
        result = reconcile(
            [],
            {"lab": client},
            state,
            present_domains={"other.example.net"},
            allow_delete=True,
            apply_changes=True,
        )
        self.assertEqual(result, [])
        self.assertEqual(client.deleted, [])

    def test_ownership_is_reported_per_instance_and_host_id(self):
        state = {"managed": {"lab:photos.example.net": "42"}, "adopted": {"lab:21": True}}
        self.assertEqual(ownership_of(state, "lab", "42"), "managed")
        self.assertEqual(ownership_of(state, "lab", "21"), "adopted")
        self.assertEqual(ownership_of(state, "public", "21"), "unmanaged")
        self.assertEqual(ownership_of(state, "lab", 7), "unmanaged")
        self.assertEqual(adopted_host_ids(state, "lab"), {"21"})
        self.assertEqual(adopted_host_ids(state, "public"), set())

    def test_adopting_and_releasing_change_only_this_instance(self):
        state = {"managed": {"lab:photos.example.net": "42"}, "adopted": {}}
        adopt_host(state, "public", 3)
        self.assertEqual(state["adopted"], {"public:3": True})
        self.assertEqual(forget_host(state, "lab", "42"), 1)
        self.assertEqual(state["managed"], {})
        self.assertEqual(state["adopted"], {"public:3": True})
        self.assertEqual(forget_host(state, "public", "3"), 1)
        self.assertEqual(state["adopted"], {})


class AliasReconcileTests(unittest.TestCase):
    """A guest's aliases are written as extra names on its own host."""

    @staticmethod
    def alias_host():
        host = planned_host()
        host["domain_names"] = ["photos.example.net", "pictures.example.net"]
        return host

    def test_a_new_host_is_created_with_every_name(self):
        client = FakeNpmPlus()
        state = {"managed": {}}
        result = reconcile([self.alias_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "CREATE")
        self.assertEqual(client.created[0]["domain_names"], ["photos.example.net", "pictures.example.net"])
        self.assertEqual(client.created[0]["certificate_id"], 7)

    def test_adding_an_alias_to_a_recorded_host_updates_it(self):
        existing = managed_host()
        existing.update({
            "npmplus_access_list_ids": [5],
            "npmplus_access_list_type": "private",
            "npmplus_http3_support": True,
            "advanced_config": "proxy_read_timeout 60s;",
            "locations": [{"path": "/health", "forward_scheme": "http", "forward_host": "health", "forward_port": 80}],
            # These fields are present on legacy/expanded responses but are not
            # accepted by NPMplus's strict proxy-host update schema.
            "access_list_id": 5,
            "block_exploits": True,
            "meta": {},
        })
        client = FakeNpmPlus(hosts=[existing])
        state = {"managed": {"lab:photos.example.net": "42"}}
        result = reconcile([self.alias_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "UPDATE")
        payload = client.updated[0][1]
        self.assertEqual(payload["domain_names"], ["photos.example.net", "pictures.example.net"])
        self.assertEqual(payload["npmplus_access_list_ids"], [5])
        self.assertEqual(payload["npmplus_access_list_type"], "private")
        self.assertTrue(payload["npmplus_http3_support"])
        self.assertEqual(payload["advanced_config"], "proxy_read_timeout 60s;")
        self.assertEqual(payload["locations"], existing["locations"])
        self.assertLessEqual(set(payload), ReconcileTests.NPMPLUS_PROXY_HOST_FIELDS)
        self.assertNotIn("access_list_id", payload)
        self.assertNotIn("block_exploits", payload)
        self.assertNotIn("meta", payload)

    def test_a_recorded_host_that_already_matches_is_left_alone(self):
        client = FakeNpmPlus(hosts=[managed_host()])
        state = {"managed": {"lab:photos.example.net": "42"}}
        result = reconcile([planned_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "OK")
        self.assertEqual(client.updated, [])

    def test_a_host_needs_one_certificate_covering_every_name(self):
        client = FakeNpmPlus()
        client.cert_list = [{"id": 7, "domain_names": ["photos.example.net"]}]
        state = {"managed": {}}
        result = reconcile([self.alias_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "SKIP")
        self.assertIn("photos.example.net, pictures.example.net", result[0][3])
        self.assertEqual(client.created, [])

    def test_an_alias_another_entry_already_serves_is_refused(self):
        client = FakeNpmPlus(hosts=[
            {"id": 21, "domain_names": ["pictures.example.net"]},
            managed_host(),
        ])
        state = {"managed": {"lab:photos.example.net": "42"}}
        result = reconcile([self.alias_host()], {"lab": client}, state, apply_changes=True)
        self.assertEqual(result[0][0], "CONFLICT")
        self.assertIn("alias pictures.example.net is already served by proxy host 21", result[0][3])
        self.assertEqual(client.updated, [])


class PruneGuardTests(unittest.TestCase):
    """A managed host is deleted only while it serves the names this app knows for it."""

    def prune(self, client, planned_names=None):
        state = {"managed": {"lab:photos.example.net": "42"}}
        return reconcile(
            [],
            {"lab": client},
            state,
            present_domains={"other.example.net"},
            allow_delete=True,
            apply_changes=True,
            max_deletes=10,
            planned_names=planned_names,
        )

    def test_a_managed_host_whose_guest_is_gone_is_deleted(self):
        client = FakeNpmPlus(hosts=[managed_host()])
        self.assertEqual(self.prune(client)[0][0], "DELETE")
        self.assertEqual(client.deleted, ["42"])

    def test_cleanup_without_a_limit_is_refused(self):
        client = FakeNpmPlus(hosts=[managed_host()])
        result = reconcile(
            [],
            {"lab": client},
            {"managed": {"lab:photos.example.net": "42"}},
            present_domains={"other.example.net"},
            allow_delete=True,
            apply_changes=True,
        )
        self.assertEqual(result[0][0], "ERROR")
        self.assertIn("Set a deletion limit", result[0][3])
        self.assertEqual(client.deleted, [])

    def test_a_host_serving_a_name_this_app_does_not_know_is_never_deleted(self):
        client = FakeNpmPlus(hosts=[managed_host()])
        client.hosts[0]["domain_names"] = ["photos.example.net", "manual-alias.example.net"]
        result = self.prune(client)
        self.assertEqual(result[0][0], "CONFLICT")
        self.assertIn("delete it in Host management", result[0][3])
        self.assertEqual(client.deleted, [])

    def test_a_configured_alias_is_part_of_the_names_that_may_be_deleted(self):
        client = FakeNpmPlus(hosts=[managed_host()])
        client.hosts[0]["domain_names"] = ["photos.example.net", "pictures.example.net"]
        result = self.prune(client, planned_names={
            "photos.example.net": ["photos.example.net", "pictures.example.net"],
        })
        self.assertEqual(result[0][0], "DELETE")

    def test_a_host_serving_another_domain_instead_is_not_deleted(self):
        client = FakeNpmPlus(hosts=[managed_host()])
        client.hosts[0]["domain_names"] = ["renamed.example.net"]
        result = self.prune(client)
        self.assertEqual(result[0][0], "CONFLICT")
        self.assertIn("no longer serves this domain", result[0][3])
        self.assertEqual(client.deleted, [])


if __name__ == "__main__":
    unittest.main()