import unittest

from reverse_proxy_automation.integrations import (
    NpmPlusDestinationAdapter,
    PslSourceAdapter,
    normalize_psl_record,
)


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

        adapter = NpmPlusDestinationAdapter(create_client, reconcile)
        plan = adapter.plan([guest], config)
        state = {"managed": {}}

        rows = adapter.apply(plan, config, state, apply_changes=True)

        self.assertEqual(adapter.key, "npmplus")
        self.assertEqual(plan["plan"][0]["forward_port"], 9090)
        self.assertEqual(client_calls, [("lab", config["instances"]["lab"])])
        self.assertEqual(rows[0][0], "CREATE")
        self.assertEqual(reconcile_calls[0][0][1], {"lab": "npm-client"})
        self.assertTrue(reconcile_calls[0][1]["apply_changes"])
