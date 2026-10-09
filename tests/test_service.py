import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from reverse_proxy_automation.service import ConfigStore, SyncService
from reverse_proxy_automation.sync import load_state


PSL_SCRIPTS = []
PSL_REFRESH_REQUESTS = []


def guest(hostname, vlan, port, script_id=1, container_id="101"):
    return {
        "id": script_id,
        "script_name": hostname,
        "container_id": container_id,
        "status": "success",
        "web_ui_port": port,
        "is_vm": False,
        "lxc_config": {"hostname": hostname, "net_vlan": vlan},
    }


class FakePsl:
    def __init__(self, url, username="", password="", verify_tls=True):
        self.url = url

    def installed_scripts(self, refresh_lxc_config=False, progress_callback=None):
        PSL_REFRESH_REQUESTS.append(refresh_lxc_config)
        if refresh_lxc_config and progress_callback is not None and PSL_SCRIPTS:
            script = PSL_SCRIPTS[0]
            progress_callback(1, len(PSL_SCRIPTS), script["id"], script["container_id"])
        return list(PSL_SCRIPTS)


class FakeNpmPlus:
    def __init__(self, name, url, identity="", secret=""):
        self.name = name
        self.url = url
        self.hosts = {}
        self.deleted = []

    def proxy_hosts(self, expand_certificate=True):
        return [
            {
                "id": host_id,
                "domain_names": list(payload["domain_names"]),
                "forward_scheme": payload.get("forward_scheme", "http"),
                "forward_host": payload.get("forward_host", ""),
                "forward_port": payload.get("forward_port"),
                "enabled": True,
            }
            for host_id, payload in self.hosts.items()
        ]

    def certificates(self):
        return [{"id": 7, "domain_names": ["*.example.net"]}]

    def create_proxy_host(self, payload):
        host_id = str(len(self.hosts) + 1)
        self.hosts[host_id] = payload
        return {"id": host_id}

    def update_proxy_host(self, host_id, payload):
        self.hosts[str(host_id)] = payload
        return {"id": host_id}

    def delete_proxy_host(self, host_id):
        self.deleted.append(str(host_id))
        self.hosts.pop(str(host_id), None)


def service_for(directory, instances, vlan_map):
    store = ConfigStore(Path(directory) / "app-config.json")
    store.save({
        "pve_local_url": "https://psl.example.net",
        "public_domain": "example.net",
        "instances": instances,
        "vlan_map": vlan_map,
    })
    return SyncService(store, Path(directory) / "sync-state.json"), store


class MultiInstanceRunTests(unittest.TestCase):
    def test_watcher_runs_immediately_on_startup_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "app-config.json")
            store.save({
                "pve_local_url": "https://psl.example.net",
                "public_domain": "example.net",
                "instances": {"lab": {"url": "https://lab.example.net:81"}},
                "vlan_map": {"20": {"instance": "lab", "zone": "internal.example.net"}},
                "watch_enabled": True,
                "auto_apply": True,
                "poll_interval_seconds": 300,
            })
            called = threading.Event()
            arguments = []

            def record_run(service, apply_changes=False, refresh_lxc_config=False):
                arguments.append((apply_changes, refresh_lxc_config))
                called.set()
                return {"state": "ok"}

            with mock.patch.object(SyncService, "run_once", record_run):
                service = SyncService(store, Path(directory) / "sync-state.json")
                try:
                    self.assertTrue(called.wait(2), "watcher waited for its polling interval on startup")
                    self.assertEqual(arguments, [(True, True)])
                finally:
                    service.stop()

    def test_watcher_refreshes_endpoints_before_applying(self):
        with tempfile.TemporaryDirectory() as directory:
            service, store = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"20": {"instance": "lab", "zone": "internal.example.net"}},
            )
            called = threading.Event()
            arguments = []

            def record_run(apply_changes=False, refresh_lxc_config=False):
                arguments.append((apply_changes, refresh_lxc_config))
                called.set()
                return {"state": "ok"}

            service.run_once = record_run
            store.save({
                "watch_enabled": True,
                "auto_apply": True,
                "poll_interval_seconds": 300,
            })
            service.wake_loop()
            try:
                self.assertTrue(called.wait(2), "watcher did not start a scheduled run")
                self.assertEqual(arguments, [(True, True)])
            finally:
                service.stop()

    def test_watcher_survives_configuration_failure_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "app-config.json")
            config = {
                "watch_enabled": True,
                "auto_apply": False,
                "poll_interval_seconds": 300,
            }
            store.load = mock.Mock(side_effect=[OSError("temporary read failure"), config])
            called = threading.Event()

            def record_run(service, apply_changes=False, refresh_lxc_config=False):
                called.set()
                return {"state": "ok"}

            with mock.patch.object(SyncService, "run_once", record_run), \
                    mock.patch("reverse_proxy_automation.service.logging.exception"):
                service = SyncService(store, Path(directory) / "sync-state.json")
                try:
                    deadline = threading.Event()
                    for _ in range(100):
                        if service.get_status()["state"] == "error":
                            break
                        deadline.wait(0.01)
                    self.assertEqual(service.get_status()["state"], "error")
                    self.assertIn("temporary read failure", service.get_status()["message"])
                    self.assertTrue(service.thread.is_alive())
                    service.wake_loop()
                    self.assertTrue(called.wait(2), "watcher did not recover after configuration became readable")
                finally:
                    service.stop()

    def test_refresh_failure_is_a_plan_skip_and_does_not_fail_the_run(self):
        script = guest("photos", "20", 8081)
        script["_refresh_error"] = "Could not refresh LXC config for container 101: SSH connection failed"
        PSL_SCRIPTS[:] = [script]

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl):
            service, _ = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"20": {"instance": "lab", "zone": "internal.example.net"}},
            )
            try:
                status = service.run_once(refresh_lxc_config=True)
            finally:
                service.stop()

        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(status["planned"], [])
        self.assertEqual(len(status["skipped"]), 1)
        self.assertEqual(status["skipped"][0]["name"], "photos")
        self.assertIn("SSH connection failed", status["skipped"][0]["reason"])

    def test_refresh_failure_with_saved_ip_and_port_does_not_add_plan_error(self):
        registry = {}

        def factory(name, url, identity="", secret="", verify_tls=True):
            return registry.setdefault(name, FakeNpmPlus(name, url))

        script = guest("photos", "20", 8081)
        script["web_ui_ip"] = "192.0.2.8"
        script["_refresh_error"] = "Could not refresh Web UI IP: SSH connection failed"
        PSL_SCRIPTS[:] = [script]

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"20": {"instance": "lab", "zone": "internal.example.net"}},
            )
            try:
                status = service.run_once(refresh_lxc_config=True)
            finally:
                service.stop()

        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(len(status["planned"]), 1)
        self.assertEqual(status["planned"][0]["domain"], "photos.example.net")
        self.assertNotIn("SSH connection failed", status["planned"][0]["details"])
        self.assertEqual(status["skipped"], [])

    def test_manual_refresh_waits_for_an_existing_watcher_run(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"20": {"instance": "lab", "zone": "internal.example.net"}},
            )
            entered = threading.Event()
            result = []

            def manual_run():
                entered.set()
                result.append(service.run_once(refresh_lxc_config=True))

            original_run = service._run_once
            service._run_once = lambda apply, refresh=False: {"state": "ok", "did_refresh": refresh}
            service.run_lock.acquire()
            thread = threading.Thread(target=manual_run)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(thread.is_alive())
            finally:
                service.run_lock.release()
            thread.join(timeout=2)
            service._run_once = original_run
            service.stop()

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [{"state": "ok", "did_refresh": True}])

    def test_failed_forced_refresh_clears_the_previous_plan(self):
        class FailingPsl(FakePsl):
            def installed_scripts(self, refresh_lxc_config=False, progress_callback=None):
                raise RuntimeError("Cannot refresh Web UI IP")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FailingPsl):
            service, _ = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"20": {"instance": "lab", "zone": "internal.example.net"}},
            )
            service.status.update({
                "planned": [{"domain": "old.example.net"}],
                "skipped": [{"name": "old-guest", "reason": "stale IP"}],
                "guest_count": 1,
            })
            try:
                status = service.run_once(refresh_lxc_config=True)
            finally:
                service.stop()

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["planned"], [])
        self.assertEqual(status["skipped"], [])
        self.assertEqual(status["guest_count"], 0)

    def test_cleared_exclusion_setting_allows_guest_without_a_configured_exclusion(self):
        registry = {}
        PSL_SCRIPTS[:] = [guest("npmplus-one", "20", 81)]

        def factory(name, url, identity, secret, verify_tls=True):
            return registry.setdefault(name, FakeNpmPlus(name, url))

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, store = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"20": {"instance": "lab", "zone": "internal.example.net"}},
            )
            store.save({"excluded_guest_hostnames": ""})
            PSL_REFRESH_REQUESTS.clear()
            try:
                status = service.run_once(apply_changes=False, refresh_lxc_config=True)
            finally:
                service.stop()

        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(PSL_REFRESH_REQUESTS, [True])
        self.assertEqual(status["planned"][0]["status"], "DRY-CREATE")
        self.assertEqual(status["planned"][0]["domain"], "npmplus-one.example.net")
        self.assertEqual(status["skipped"], [])

    def test_every_vlan_is_reconciled_on_the_instance_it_maps_to(self):
        registry = {}

        def factory(name, url, identity, secret, verify_tls=True):
            return registry.setdefault(name, FakeNpmPlus(name, url))

        PSL_SCRIPTS[:] = [guest("photos", "20", 8081, 1), guest("wiki", "30", 8080, 2, "102")]
        instances = {
            "lab": {"url": "https://lab.example.net:81"},
            "dmz": {"url": "https://dmz.example.net:81"},
        }
        vlan_map = {
            "20": {"instance": "lab", "zone": "internal.example.net"},
            "30": {"instance": "dmz", "zone": "dmz.example.net"},
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, store = service_for(directory, instances, vlan_map)
            store.save({"delete_removed_hosts": True, "max_deletes_per_run": 10})
            state_path = Path(directory) / "sync-state.json"
            try:
                status = service.run_once(apply_changes=True)
                self.assertEqual(status["state"], "ok", status["message"])
                self.assertEqual(status["timings"]["source_records"], 2)
                self.assertGreaterEqual(status["timings"]["psl_read_seconds"], 0)
                self.assertGreaterEqual(status["timings"]["planning_seconds"], 0)
                self.assertGreaterEqual(status["timings"]["npmplus_seconds"], 0)
                self.assertEqual(sorted(registry), ["dmz NPMPlus", "lab NPMPlus"])
                self.assertEqual(registry["lab NPMPlus"].hosts["1"]["forward_host"], "photos.internal.example.net")
                self.assertEqual(registry["lab NPMPlus"].hosts["1"]["forward_port"], 8081)
                self.assertEqual(registry["dmz NPMPlus"].hosts["1"]["forward_host"], "wiki.dmz.example.net")
                self.assertEqual(
                    load_state(state_path)["managed"],
                    {"lab:photos.example.net": "1", "dmz:wiki.example.net": "1"},
                )

                # The lab guest disappears from PSL: only its own instance is touched.
                PSL_SCRIPTS[:] = [guest("wiki", "30", 8080, 2, "102")]
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
            self.assertEqual(status["state"], "ok", status["message"])
            self.assertEqual(registry["lab NPMPlus"].deleted, ["1"])
            self.assertEqual(registry["dmz NPMPlus"].deleted, [])
            self.assertEqual(list(load_state(state_path)["managed"]), ["dmz:wiki.example.net"])

    def test_vlan_mapped_to_an_instance_without_a_url_fails_before_any_client_is_built(self):
        def factory(name, url, identity, secret, verify_tls=True):
            raise AssertionError(f"no client may be built for {name}")

        PSL_SCRIPTS[:] = [guest("wiki", "30", 8080)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(directory, {"dmz": {"url": ""}}, {"30": {"instance": "dmz", "zone": "dmz.example.net"}})
            try:
                status = service.run_once(apply_changes=False)
            finally:
                service.stop()
            self.assertEqual(status["state"], "error")
            self.assertIn("Set the URL for NPMPlus instance 'dmz'", status["message"])


class DuplicateContainerRecordTests(unittest.TestCase):
    def test_one_container_gets_one_host_when_psl_holds_two_records(self):
        registry = {}

        def factory(name, url, identity, secret, verify_tls=True):
            return registry.setdefault(name, FakeNpmPlus(name, url))

        # One container, two records: the failed first attempt and the retry.
        stale = guest("previous-app.sh", "20", 5000, 1, "100")
        stale["status"] = "failed"
        fresh = guest("current-app.sh", "20", 5000, 2, "100")
        PSL_SCRIPTS[:] = [stale, fresh]
        instances = {"lab": {"url": "https://lab.example.net:81"}}
        vlan_map = {"20": {"instance": "lab", "zone": "internal.example.net"}}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(directory, instances, vlan_map)
            try:
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(list(registry["lab NPMPlus"].hosts), ["1"])
        self.assertEqual(registry["lab NPMPlus"].hosts["1"]["domain_names"], ["current-app.example.net"])
        # One container is one guest; historical PSL attempts are not surfaced as
        # skipped hosts.
        self.assertEqual(status["guest_count"], 1)
        self.assertEqual(status["skipped"], [])


class SecretReferenceRunTests(unittest.TestCase):
    def test_a_run_uses_the_environment_value_for_an_instance_secret(self):
        seen = {}

        def factory(name, url, identity, secret, verify_tls=True):
            seen[name] = secret
            return FakeNpmPlus(name, url)

        PSL_SCRIPTS[:] = [guest("wiki", "30", 8080)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"NPM_DMZ_SECRET": "from-env"}), \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(
                directory,
                {"dmz": {"url": "https://dmz.example.net:81", "secret": "env:NPM_DMZ_SECRET"}},
                {"30": {"instance": "dmz", "zone": "dmz.example.net"}},
            )
            try:
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(seen["dmz NPMPlus"], "from-env")

    def test_a_run_names_an_unset_variable_before_any_client_is_built(self):
        def refuse(*args, **kwargs):
            raise AssertionError("no client may be built while a secret reference is unresolved")

        PSL_SCRIPTS[:] = [guest("wiki", "30", 8080)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", refuse), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", refuse):
            service, _ = service_for(
                directory,
                {"dmz": {"url": "https://dmz.example.net:81", "secret": "env:NPM_DMZ_SECRET"}},
                {"30": {"instance": "dmz", "zone": "dmz.example.net"}},
            )
            try:
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
        self.assertEqual(status["state"], "error")
        self.assertIn("NPM_DMZ_SECRET", status["message"])
        self.assertIn("not set", status["message"])

    def test_a_run_resolves_the_psl_password_reference_before_building_the_client(self):
        seen = {}

        def psl_factory(url, username="", password="", verify_tls=True):
            seen["password"] = password
            return FakePsl(url, username, password)

        PSL_SCRIPTS[:] = []
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"PVE_LOCAL_PASSWORD": "psl-from-env"}), \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", psl_factory), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", FakeNpmPlus):
            service, store = service_for(directory, {}, {})
            store.save({"pve_local_password": "env:PVE_LOCAL_PASSWORD"})
            try:
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(seen["password"], "psl-from-env")

    def test_testing_an_instance_uses_the_environment_value(self):
        seen = {}

        def factory(name, url, identity, secret, verify_tls=True):
            seen["secret"] = secret
            return FakeNpmPlus(name, url)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"NPM_DMZ_SECRET": "from-env"}), \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(
                directory,
                {"dmz": {"url": "https://dmz.example.net:81", "secret": "env:NPM_DMZ_SECRET"}},
                {},
            )
            try:
                result = service.test_connection("dmz")
            finally:
                service.stop()
        self.assertTrue(result["ok"])
        self.assertEqual(seen["secret"], "from-env")

    def test_testing_an_instance_names_its_own_unset_variable(self):
        def factory(name, url, identity, secret, verify_tls=True):
            raise AssertionError("no client may be built while a secret reference is unresolved")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(
                directory,
                {"dmz": {"url": "https://dmz.example.net:81", "secret": "env:NPM_DMZ_SECRET"}},
                {},
            )
            try:
                with self.assertRaises(ValueError) as caught:
                    service.test_connection("dmz")
            finally:
                service.stop()
        self.assertIn("NPM_DMZ_SECRET", str(caught.exception))


class SelfSignedRunTests(unittest.TestCase):
    def test_a_run_relaxes_verification_only_for_the_switched_endpoint(self):
        seen = {}

        def psl_factory(url, username="", password="", verify_tls=True):
            seen["pve"] = verify_tls
            return FakePsl(url, username, password)

        def factory(name, url, identity, secret, verify_tls=True):
            seen[name] = verify_tls
            return FakeNpmPlus(name, url)

        PSL_SCRIPTS[:] = [guest("photos", "20", 8081, 1), guest("wiki", "30", 8080, 2, "102")]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", psl_factory), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            store = ConfigStore(Path(directory) / "app-config.json")
            store.save({
                "pve_local_url": "https://psl.example.net",
                "public_domain": "example.net",
                "pve_local_verify_tls": False,
                "instances": {
                    "lab": {"url": "https://lab.example.net:81", "verify_tls": False},
                    "dmz": {"url": "https://dmz.example.net:81"},
                },
                "vlan_map": {
                    "20": {"instance": "lab", "zone": "internal.example.net"},
                    "30": {"instance": "dmz", "zone": "dmz.example.net"},
                },
            })
            service = SyncService(store, Path(directory) / "sync-state.json")
            try:
                status = service.run_once(apply_changes=False)
            finally:
                service.stop()
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertFalse(seen["pve"])
        self.assertFalse(seen["lab NPMPlus"])
        # An endpoint whose switch is off still verifies its certificate.
        self.assertTrue(seen["dmz NPMPlus"])

    def test_testing_an_instance_forwards_its_switch(self):
        seen = {}

        def factory(name, url, identity, secret, verify_tls=True):
            seen["verify_tls"] = verify_tls
            return FakeNpmPlus(name, url)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = service_for(
                directory,
                {"dmz": {"url": "https://dmz.example.net:81", "verify_tls": False}},
                {},
            )
            try:
                result = service.test_connection("dmz")
            finally:
                service.stop()
        self.assertTrue(result["ok"])
        self.assertFalse(seen["verify_tls"])


class EntryManagementTests(unittest.TestCase):
    """The host management tab lists every host and can manage one."""

    @staticmethod
    def build(directory, instances, managed=None, adopted=None):
        store = ConfigStore(Path(directory) / "app-config.json")
        store.save({
            "pve_local_url": "https://psl.example.net",
            "public_domain": "example.net",
            "instances": instances,
        })
        state_path = Path(directory) / "sync-state.json"
        state_path.write_text(
            json.dumps({"managed": managed or {}, "adopted": adopted or {}}), encoding="utf-8"
        )
        return SyncService(store, state_path), state_path

    @staticmethod
    def registry_factory(registry):
        def factory(name, url, identity="", secret="", verify_tls=True):
            return registry.setdefault(name, FakeNpmPlus(name, url))

        return factory

    def test_entries_show_how_each_host_is_owned(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        client = registry["lab NPMPlus"]
        client.hosts["9"] = {
            "domain_names": ["feed-service.example.net"], "forward_scheme": "http",
            "forward_host": "feed-service.internal.example.net", "forward_port": 8082,
        }
        client.hosts["21"] = {
            "domain_names": ["media-service.example.net", "photos.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.public.example.net", "forward_port": 8081,
        }
        client.hosts["30"] = {
            "domain_names": ["video-service.example.net"], "forward_scheme": "http",
            "forward_host": "video-service.public.example.net", "forward_port": 8090,
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, store = self.build(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                managed={"lab:feed-service.example.net": "9"},
                adopted={"lab:21": True},
            )
            try:
                service.config_store.save({"aliases": "media-service = gallery"})
                listing = service.list_entries()
            finally:
                service.stop()
        lab = listing["instances"][0]
        self.assertEqual(lab["label"], "lab NPMPlus")
        self.assertEqual(lab["error"], "")
        self.assertEqual([host["id"] for host in lab["hosts"]], ["9", "21", "30"])
        self.assertEqual(
            [host["ownership"] for host in lab["hosts"]], ["managed", "adopted", "unmanaged"]
        )
        self.assertEqual(lab["hosts"][1]["domain_names"], ["media-service.example.net", "photos.example.net"])
        self.assertEqual(lab["hosts"][1]["aliases"], ["gallery.example.net"])
        self.assertEqual(lab["hosts"][0]["forward_host"], "feed-service.internal.example.net")
        self.assertEqual(lab["hosts"][0]["forward_port"], 8082)
        self.assertIsNone(lab["hosts"][0]["port_override"])

    def test_multiple_instances_are_loaded_concurrently(self):
        barrier = threading.Barrier(2)
        registry = {}

        class ConcurrentNpmPlus(FakeNpmPlus):
            def proxy_hosts(self, expand_certificate=True):
                barrier.wait(timeout=1)
                return []

        def factory(name, url, identity="", secret="", verify_tls=True):
            return registry.setdefault(name, ConcurrentNpmPlus(name, url))

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, _ = self.build(
                directory,
                {
                    "lab": {"url": "https://lab.example.net:81"},
                    "dmz": {"url": "https://dmz.example.net:81"},
                },
            )
            try:
                result = service.list_entries()
            finally:
                service.stop()

        self.assertEqual([instance["error"] for instance in result["instances"]], ["", ""])
        self.assertEqual(len(result["instances"]), 2)

    def test_an_instance_without_a_url_is_reported_in_its_own_row(self):
        registry = {}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, _ = self.build(directory, {"dmz": {"url": ""}})
            try:
                listing = service.list_entries()
            finally:
                service.stop()
        self.assertEqual(listing["instances"][0]["hosts"], [])
        self.assertIn("No URL is saved", listing["instances"][0]["error"])


    def test_adopting_and_releasing_an_entry_writes_and_clears_its_record(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        registry["lab NPMPlus"].hosts["21"] = {
            "domain_names": ["media-service.example.net", "photos.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.public.example.net", "forward_port": 8081,
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, state_path = self.build(directory, {"lab": {"url": "https://lab.example.net:81"}})
            try:
                adopted = service.set_ownership("lab", "21", "adopt")
                self.assertEqual(adopted["ownership"], "adopted")
                self.assertIn("never deleted on its own", adopted["message"])
                self.assertEqual(load_state(state_path)["adopted"], {"lab:21": True})
                released = service.set_ownership("lab", "21", "release")
                self.assertEqual(released["ownership"], "unmanaged")
                self.assertIn("left alone", released["message"])
                self.assertEqual(load_state(state_path)["adopted"], {})
            finally:
                service.stop()

    def test_aliases_are_saved_for_the_selected_guest_route(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        registry["lab NPMPlus"].hosts["21"] = {
            "domain_names": ["media-service.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.public.example.net", "forward_port": 8081,
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, _ = self.build(directory, {"lab": {"url": "https://lab.example.net:81"}})
            try:
                result = service.set_entry_aliases(
                    "lab", "21", ["pictures", "gallery.example.org"]
                )
                self.assertEqual(result["guest_hostname"], "media-service")
                self.assertEqual(
                    result["aliases"],
                    ["pictures.example.net", "gallery.example.org"],
                )
                self.assertEqual(
                    service.config_store.load()["entry_aliases"],
                    {"lab:media-service": ["pictures.example.net", "gallery.example.org"]},
                )
                listing = service.list_entries()
                self.assertEqual(listing["instances"][0]["hosts"][0]["aliases"], result["aliases"])
                with self.assertRaises(ValueError):
                    service.set_entry_aliases("lab", "21", ["media-service"])
            finally:
                service.stop()

    def test_deleting_an_entry_removes_it_and_its_recorded_ownership(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        client_builds = []
        registry["lab NPMPlus"].hosts["21"] = {
            "domain_names": ["media-service.example.net", "photos.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.public.example.net", "forward_port": 8081,
        }

        def factory(name, url, identity="", secret="", verify_tls=True):
            client_builds.append(name)
            return registry[name]

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", factory):
            service, state_path = self.build(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                managed={"lab:media-service.example.net": "21"},
            )
            try:
                result = service.delete_entry("lab", "21")
            finally:
                service.stop()
        self.assertEqual(registry["lab NPMPlus"].deleted, ["21"])
        self.assertEqual(client_builds, ["lab NPMPlus"])
        self.assertIn("media-service.example.net, photos.example.net", result["message"])
        self.assertTrue(result["message"].startswith("Deleted"), result["message"])
        self.assertEqual(load_state(state_path)["managed"], {})

    def test_an_unknown_action_or_a_missing_entry_is_refused(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, _ = self.build(directory, {"lab": {"url": "https://lab.example.net:81"}})
            try:
                with self.assertRaises(ValueError) as caught:
                    service.set_ownership("lab", "21", "promote")
                self.assertIn("must be 'adopt' or 'release'", str(caught.exception))
                with self.assertRaises(ValueError) as caught:
                    service.delete_entry("lab", "21")
                self.assertIn("reload the entries list", str(caught.exception))
                with self.assertRaises(ValueError) as caught:
                    service.delete_entry("dmz", "1")
                self.assertIn("not configured", str(caught.exception))
            finally:
                service.stop()


    def test_aliases_from_the_settings_reach_the_created_host(self):
        registry = {}
        PSL_SCRIPTS[:] = [guest("media-service", "20", 8081, 1)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, _ = service_for(
                directory,
                {"public": {"url": "https://public.example.net:81"}},
                {"20": {"instance": "public", "zone": "public.example.net"}},
            )
            service.config_store.save({"aliases": "media-service = photos"})
            try:
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(
            registry["public NPMPlus"].hosts["1"]["domain_names"],
            ["media-service.example.net", "photos.example.net"],
        )

    def test_instance_guest_aliases_are_applied_to_their_route(self):
        registry = {}
        PSL_SCRIPTS[:] = [guest("media-service", "10", 8081)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, _ = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"10": {"instance": "lab", "zone": "internal.example.net"}},
            )
            service.config_store.save({
                "entry_aliases": {
                    "lab:media-service": ["photos.example.net", "pictures.example.net"]
                }
            })
            try:
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(
            registry["lab NPMPlus"].hosts["1"]["domain_names"],
            ["media-service.example.net", "photos.example.net", "pictures.example.net"],
        )

    def test_host_management_port_override_is_persisted_and_applied_to_the_route(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        registry["lab NPMPlus"].hosts["21"] = {
            "domain_names": ["media-service.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.internal.example.net", "forward_port": 8081,
        }
        PSL_SCRIPTS[:] = [guest("media-service", "10", 80)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, store = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"10": {"instance": "lab", "zone": "internal.example.net"}},
            )
            try:
                service.set_ownership("lab", "21", "adopt")
                result = service.set_entry_port("lab", "21", 8090)
                self.assertEqual(result["port_override"], 8090)
                self.assertEqual(
                    store.load()["entry_port_overrides"],
                    {"lab:media-service": 8090},
                )
                listing = service.list_entries()
                self.assertEqual(listing["instances"][0]["hosts"][0]["port_override"], 8090)
                preview = service.run_once(apply_changes=False)
                self.assertEqual(preview["state"], "ok", preview["message"])
                self.assertIn(":8090", preview["planned"][0]["details"])
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()

        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(registry["lab NPMPlus"].hosts["21"]["forward_port"], 8090)

    def test_clearing_a_host_port_override_restores_detected_port_routing(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        registry["lab NPMPlus"].hosts["21"] = {
            "domain_names": ["media-service.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.internal.example.net", "forward_port": 8090,
        }
        PSL_SCRIPTS[:] = [guest("media-service", "10", 8081)]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.PveScriptsLocalClient", FakePsl), \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, store = service_for(
                directory,
                {"lab": {"url": "https://lab.example.net:81"}},
                {"10": {"instance": "lab", "zone": "internal.example.net"}},
            )
            store.save({"entry_port_overrides": {"lab:media-service": 8090}})
            try:
                service.set_ownership("lab", "21", "adopt")
                result = service.set_entry_port("lab", "21", "")
                self.assertIsNone(result["port_override"])
                self.assertEqual(store.load()["entry_port_overrides"], {})
                status = service.run_once(apply_changes=True)
            finally:
                service.stop()

        self.assertEqual(status["state"], "ok", status["message"])
        self.assertEqual(registry["lab NPMPlus"].hosts["21"]["forward_port"], 8081)

    def test_host_port_override_rejects_out_of_range_and_fractional_values(self):
        registry = {"lab NPMPlus": FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
        registry["lab NPMPlus"].hosts["21"] = {
            "domain_names": ["media-service.example.net"], "forward_scheme": "http",
            "forward_host": "media-service.internal.example.net", "forward_port": 8081,
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("reverse_proxy_automation.service.NpmPlusClient", self.registry_factory(registry)):
            service, _ = self.build(directory, {"lab": {"url": "https://lab.example.net:81"}})
            try:
                for port in (0, 65536, 1.5, True, "not-a-port"):
                    with self.subTest(port=port), self.assertRaises(ValueError):
                        service.set_entry_port("lab", "21", port)
            finally:
                service.stop()

    def test_a_malformed_alias_line_is_refused_when_the_settings_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "app-config.json")
            store.save({"pve_local_url": "https://psl.example.net", "public_domain": "example.net"})
            with self.assertRaises(ValueError) as caught:
                store.save({"aliases": "media-service photos"})
        self.assertIn("Alias line 1", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
