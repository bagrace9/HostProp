import json
import logging
import tempfile
import unittest
from pathlib import Path

from reverse_proxy_automation.plugins.base import destination_state
from reverse_proxy_automation.service import (
    ConfigStore,
    SyncService,
    destination_applies,
    destination_enabled,
    normalize_config,
    public_config,
    unresolved_secret_references,
    validate_config,
)


class Source:
    def read(self, config, refresh_lxc_config=False, progress_callback=None):
        return [{"id": 1, "name": "photos", "container_id": "101"}]


class Destination:
    def __init__(self, key, plan_error=None, apply_error=None):
        self.key = key
        self.label = key.title()
        self.plan_error = plan_error
        self.apply_error = apply_error
        self.calls = []

    def plan(self, guests, config):
        if self.plan_error:
            raise RuntimeError(self.plan_error)
        return {"plan": [], "skipped": [], "containers": 1}

    def apply(self, plan, config, state, apply_changes=False):
        self.calls.append(apply_changes)
        destination_state(state, self.key)["touched"] = True
        if self.apply_error:
            raise RuntimeError(self.apply_error)
        return [("CREATE", f"{self.key}-i", f"{self.key}.example.net", "made")]


def run(destinations, apply_changes=True, **settings):
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        store = ConfigStore(base / "config.json")
        store.save({"pve_local_url": "https://psl.example.net", **settings})
        service = SyncService(store, base / "state.json", source_adapter=Source(), destination_adapters=destinations)
        try:
            status = service.run_once(apply_changes=apply_changes)
        finally:
            service.stop()
        state_file = base / "state.json"
        saved = json.loads(state_file.read_text(encoding="utf-8"))["destinations"] if state_file.exists() else None
        return status, saved


def auto_run(destinations, **settings):
    """The watcher's own path: read the source once, then write what opted in."""
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        store = ConfigStore(base / "config.json")
        store.save({"pve_local_url": "https://psl.example.net", **settings})
        service = SyncService(store, base / "state.json", source_adapter=Source(), destination_adapters=destinations)
        try:
            status = service.auto_apply()
        finally:
            service.stop()
        state_file = base / "state.json"
        saved = json.loads(state_file.read_text(encoding="utf-8"))["destinations"] if state_file.exists() else None
        return status, saved


class IsolationTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_failing_apply_keeps_the_rows_and_state_of_the_destinations_that_worked(self):
        status, saved = run([Destination("good"), Destination("bad", apply_error="bad is down")])
        self.assertEqual(status["state"], "error")
        self.assertIn("Bad: bad is down", status["message"])
        self.assertEqual([row["destination"] for row in status["planned"]], ["Good"])
        self.assertEqual(saved["good"], {"touched": True})

    def test_a_failing_plan_does_not_stop_the_other_destinations(self):
        good = Destination("good")
        status, saved = run([Destination("broken", plan_error="plan blew up"), good])
        self.assertEqual(status["state"], "error")
        self.assertIn("Broken: plan blew up", status["message"])
        self.assertEqual([row["destination"] for row in status["planned"]], ["Good"])
        self.assertEqual(good.calls, [True])
        self.assertNotIn("broken", saved)

    def test_state_is_saved_for_a_destination_that_failed_part_way(self):
        status, saved = run([Destination("bad", apply_error="boom"), Destination("good")])
        self.assertEqual(saved["bad"], {"touched": True})
        self.assertEqual(saved["good"], {"touched": True})

    def test_a_single_failing_destination_reports_its_own_error_as_before(self):
        status, _ = run([Destination("only", apply_error="only is down")])
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["message"], "only is down")
        self.assertEqual(status["planned"], [])

    def test_a_preview_saves_nothing(self):
        _, saved = run([Destination("one")], apply_changes=False)
        self.assertIsNone(saved)


class EnableFlagTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_destinations_default_to_enabled(self):
        self.assertTrue(destination_enabled({}, "npmplus"))
        self.assertTrue(destination_enabled(normalize_config({}), "npmplus"))
        self.assertTrue(destination_enabled({"destinations_enabled": {"other": False}}, "npmplus"))

    def test_a_disabled_destination_does_not_run(self):
        one, two = Destination("one"), Destination("two")
        status, saved = run([one, two], destinations_enabled={"one": False})
        self.assertEqual(one.calls, [])
        self.assertEqual(two.calls, [True])
        self.assertEqual([row["destination"] for row in status["planned"]], ["Two"])
        self.assertNotIn("one", saved)

    def test_with_every_destination_off_a_run_only_reads_psl(self):
        status, _ = run([Destination("one")], destinations_enabled={"one": False})
        self.assertEqual(status["state"], "ok", status["message"])
        self.assertIn("no destination is enabled", status["message"])
        self.assertEqual(status["planned"], [])

    def test_the_flags_round_trip_and_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save({"destinations_enabled": {"npmplus": False}})
            self.assertEqual(store.load()["destinations_enabled"], {"npmplus": False})
            store.save({"destinations_enabled": {"checkmk": "yes"}})
            self.assertEqual(store.load()["destinations_enabled"], {"npmplus": False, "checkmk": True})
            self.assertEqual(public_config(store.load())["destinations_enabled"], {"npmplus": False, "checkmk": True})

    def test_a_malformed_value_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ConfigStore(Path(directory) / "config.json").save({"destinations_enabled": ["npmplus"]})
        config = normalize_config({})
        config["destinations_enabled"] = {"npmplus": "yes"}
        with self.assertRaises(ValueError):
            validate_config(config, allow_incomplete=True)

    def test_a_switched_off_npmplus_is_not_validated_and_needs_no_secrets(self):
        dangling = {"20": {"instance": "gone", "zone": "internal.example.net"}}
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            with self.assertRaises(ValueError):
                store.save({"vlan_map": dangling})
            store.save({"destinations_enabled": {"npmplus": False}, "vlan_map": dangling})
            validate_config(store.load(), allow_incomplete=True)
        config = normalize_config({"instances": {"lab": {"url": "https://x", "secret": "env:NPM_LAB_SECRET"}}})
        self.assertEqual(len(unresolved_secret_references(config, {})), 1)
        config["destinations_enabled"] = {"npmplus": False}
        self.assertEqual(unresolved_secret_references(config, {}), [])


class AutoApplyFlagTests(unittest.TestCase):
    """The watcher's writes need the master switch and the plugin's own entry."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_both_switches_have_to_agree(self):
        self.assertTrue(destination_applies({"auto_apply": True}, "npmplus"))
        self.assertTrue(
            destination_applies({"auto_apply": True, "destinations_auto_apply": {"npmplus": True}}, "npmplus")
        )
        # The master switch off stops every destination at once.
        self.assertFalse(destination_applies({"auto_apply": False}, "npmplus"))
        # The plugin's own entry refines it, for that plugin only.
        self.assertFalse(
            destination_applies({"auto_apply": True, "destinations_auto_apply": {"npmplus": False}}, "npmplus")
        )
        self.assertTrue(
            destination_applies(
                {"auto_apply": True, "destinations_auto_apply": {"npmplus": False}}, "checkmk"
            )
        )
        # A plugin switched off is skipped by the watcher, not only by a run.
        self.assertFalse(
            destination_applies({"auto_apply": True, "destinations_enabled": {"npmplus": False}}, "npmplus")
        )

    def test_the_watcher_writes_only_the_plugins_left_to_apply_on_their_own(self):
        manual, automatic = Destination("manual"), Destination("automatic")
        status, saved = auto_run(
            [manual, automatic],
            auto_apply=True,
            destinations_auto_apply={"manual": False},
        )
        self.assertEqual(manual.calls, [])
        self.assertEqual(automatic.calls, [True])
        self.assertEqual(saved, {"automatic": {"touched": True}})
        # A plugin left for manual apply is not planned by the watcher either; its own
        # page reads when it is asked to, so nothing is spent on it on every interval.
        self.assertEqual(list(status["destinations"]), ["automatic"])
        self.assertEqual(status["state"], "ok")

    def test_the_master_switch_off_writes_nothing_at_all(self):
        one, two = Destination("one"), Destination("two")
        status, saved = auto_run([one, two], auto_apply=False)
        self.assertEqual(one.calls, [])
        self.assertEqual(two.calls, [])
        self.assertIsNone(saved)
        # Nothing opted in, so the watcher wrote and planned nothing at all; the
        # status is still the idle one it started with.
        self.assertEqual(status["planned"], [])
        self.assertEqual(status["state"], "idle")


if __name__ == "__main__":
    unittest.main()
