import unittest
from unittest import mock

from reverse_proxy_automation.clients import ApiError
from reverse_proxy_automation.integrations import (
    PslSourceAdapter,
    normalize_psl_record,
)
from reverse_proxy_automation.plugins.npmplus.adapter import NpmPlusDestination


class FakePslClient:
    def __init__(self, records):
        self.records = records
        self.arguments = None

    def installed_scripts(self, **kwargs):
        self.arguments = kwargs
        return self.records


class SourceAdapterTests(unittest.TestCase):
    def test_psl_adapter_reads_records_and_returns_source_neutral_guests(self):
        record = {
            "id": 5,
            "script_name": "photos.sh",
            "container_id": 101,
            "lxc_config": {"hostname": "gallery", "net_vlan": 20},
            "web_ui_ip": "192.0.2.4",
            "web_ui_port": 8081,
            "is_vm": False,
            "_refresh_error": "refresh warning",
        }
        client = FakePslClient([record])
        progress = lambda *_: None
        adapter = PslSourceAdapter(lambda config: client)

        guests = adapter.read({}, refresh_lxc_config=True, progress_callback=progress)

        self.assertEqual(guests, [{
            "id": 5,
            "name": "photos.sh",
            "container_id": "101",
            "hostname": "gallery",
            "network_vlan": 20,
            "has_network_config": True,
            "address": "192.0.2.4",
            "port": 8081,
            "is_vm": False,
            "refresh_error": "refresh warning",
        }])
        self.assertEqual(client.arguments, {
            "refresh_lxc_config": True,
            "progress_callback": progress,
        })
        self.assertNotIn("lxc_config", guests[0])
        self.assertNotIn("web_ui_port", guests[0])

    def test_non_object_source_record_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not an object"):
            normalize_psl_record("not-a-record")


class DestinationAdapterTests(unittest.TestCase):
    def test_npm_adapter_owns_npm_planning_and_delegates_reconciliation(self):
        guest = normalize_psl_record({
            "id": 1,
            "script_name": "photos",
            "container_id": 101,
            "lxc_config": {"hostname": "photos", "net_vlan": 10},
            "web_ui_port": 8080,
        })
        config = {
            "vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}},
            "public_domain": "example.net",
            "excluded_guest_hostnames": "",
            "instances": {"lab": {"url": "https://npm.example.net:81"}},
            "entry_port_overrides": {"lab:photos": 9090},
            "delete_removed_hosts": False,
            "max_deletes_per_run": None,
        }
        client_calls = []
        reconcile_calls = []

        def create_client(instance, entry):
            client_calls.append((instance, entry))
            return "npm-client"

        def reconcile(*args, **kwargs):
            reconcile_calls.append((args, kwargs))
            return [("CREATE", "lab", "photos.example.net", "created")]

        adapter = NpmPlusDestination()
        with mock.patch.object(NpmPlusDestination, "client_factory", staticmethod(create_client)), \
                mock.patch("reverse_proxy_automation.plugins.npmplus.adapter.reconcile", reconcile):
            plan = adapter.plan([guest], config)
            state = {"managed": {}}
            rows = adapter.apply(plan, config, state, apply_changes=True)

        self.assertEqual(adapter.key, "npmplus")
        self.assertEqual(plan["plan"][0]["forward_port"], 9090)
        # The adapter builds the client itself from the saved instance entry...
        self.assertEqual(client_calls, [("lab", config["instances"]["lab"])])
        self.assertEqual(rows[0][0], "CREATE")
        # ...then hands the reconciler those clients, its own slice of the state
        # file, and the names the plan knows for each domain.
        self.assertEqual(reconcile_calls[0][0][1], {"lab": "npm-client"})
        self.assertEqual(reconcile_calls[0][0][2], state)
        self.assertEqual(reconcile_calls[0][1]["planned_names"], plan["names_by_domain"])
        self.assertTrue(reconcile_calls[0][1]["apply_changes"])

    def test_apply_refuses_a_planned_instance_with_no_url(self):
        guest = normalize_psl_record({
            "id": 1,
            "script_name": "photos",
            "container_id": 101,
            "lxc_config": {"hostname": "photos", "net_vlan": 10},
            "web_ui_port": 8080,
        })
        config = {
            "vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}},
            "public_domain": "example.net",
            "excluded_guest_hostnames": "",
            "instances": {},
            "entry_port_overrides": {},
            "delete_removed_hosts": False,
            "max_deletes_per_run": None,
        }
        adapter = NpmPlusDestination()
        plan = adapter.plan([guest], config)

        with self.assertRaisesRegex(ValueError, "Set the URL for NPMPlus instance 'lab' before syncing"):
            adapter.apply(plan, config, {"managed": {}}, apply_changes=True)

    def test_apply_handles_api_timeout_during_proxy_hosts_fetch(self):
        guest = normalize_psl_record({
            "id": 1,
            "script_name": "photos",
            "container_id": 101,
            "lxc_config": {"hostname": "photos", "net_vlan": 10},
            "web_ui_port": 8080,
        })
        config = {
            "vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}},
            "public_domain": "example.net",
            "excluded_guest_hostnames": "",
            "instances": {"lab": {"url": "https://npm.example.net:81"}},
            "entry_port_overrides": {},
            "delete_removed_hosts": False,
            "max_deletes_per_run": None,
        }
        adapter = NpmPlusDestination()
        plan = adapter.plan([guest], config)
        state = {"managed": {}}

        # Mock client that times out on proxy_hosts() call
        def create_client(instance, entry):
            client = mock.Mock()
            client.proxy_hosts.side_effect = ApiError("Request timeout", status=408)
            return client

        with mock.patch.object(NpmPlusDestination, "client_factory", staticmethod(create_client)):
            rows = adapter.apply(plan, config, state, apply_changes=True)
            
        # Should return an ERROR result
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "ERROR")
        self.assertEqual(rows[0][1], "lab")
        self.assertIn("Request timeout", rows[0][3])

    def test_apply_handles_malformed_json_response(self):
        guest = normalize_psl_record({
            "id": 1,
            "script_name": "photos",
            "container_id": 101,
            "lxc_config": {"hostname": "photos", "net_vlan": 10},
            "web_ui_port": 8080,
        })
        config = {
            "vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}},
            "public_domain": "example.net",
            "excluded_guest_hostnames": "",
            "instances": {"lab": {"url": "https://npm.example.net:81"}},
            "entry_port_overrides": {},
            "delete_removed_hosts": False,
            "max_deletes_per_run": None,
        }
        adapter = NpmPlusDestination()
        plan = adapter.plan([guest], config)
        state = {"managed": {}}

        # Mock client that returns malformed JSON (non-JSON response)
        def create_client(instance, entry):
            client = mock.Mock()
            # This will trigger _not_json error in JsonClient
            client.proxy_hosts.side_effect = ApiError("GET https://npm.example.net:81/api/nginx/proxy-hosts?expand=certificate did not return JSON (content-type text/html)")
            return client

        with mock.patch.object(NpmPlusDestination, "client_factory", staticmethod(create_client)):
            rows = adapter.apply(plan, config, state, apply_changes=True)
            
        # Should return an ERROR result
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "ERROR")
        self.assertEqual(rows[0][1], "lab")
        self.assertIn("did not return JSON", rows[0][3])

    def test_apply_handles_connection_error(self):
        guest = normalize_psl_record({
            "id": 1,
            "script_name": "photos",
            "container_id": 101,
            "lxc_config": {"hostname": "photos", "net_vlan": 10},
            "web_ui_port": 8080,
        })
        config = {
            "vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}},
            "public_domain": "example.net",
            "excluded_guest_hostnames": "",
            "instances": {"lab": {"url": "https://npm.example.net:81"}},
            "entry_port_overrides": {},
            "delete_removed_hosts": False,
            "max_deletes_per_run": None,
        }
        adapter = NpmPlusDestination()
        plan = adapter.plan([guest], config)
        state = {"managed": {}}

        # Mock client that raises connection error
        def create_client(instance, entry):
            client = mock.Mock()
            client.proxy_hosts.side_effect = ApiError("Could not reach https://npm.example.net:81: Failed to establish connection")
            return client

        with mock.patch.object(NpmPlusDestination, "client_factory", staticmethod(create_client)):
            rows = adapter.apply(plan, config, state, apply_changes=True)
            
        # Should return an ERROR result
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "ERROR")
        self.assertEqual(rows[0][1], "lab")
        self.assertIn("Could not reach", rows[0][3])
