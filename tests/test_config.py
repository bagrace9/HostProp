import json
import tempfile
import unittest
from pathlib import Path

from reverse_proxy_automation.plugins.npmplus.config import instance_label, merge_instances
from reverse_proxy_automation.service import (
    ConfigStore,
    check_secret_references,
    normalize_config,
    plaintext_secret_fields,
    public_config,
    resolve_secret,
    secret_env_name,
    unresolved_secret_references,
    validate_config,
)


def store_in(directory):
    return ConfigStore(Path(directory) / "app-config.json")


LEGACY_CONFIG = {
    "pve_local_url": "https://psl.internal.example.net:3000",
    "npm_lab_url": "https://npmplus-one.internal.example.net:81",
    "npm_lab_identity": "user@example.org",
    "npm_lab_secret": "lab-secret",
    "npm_public_url": "https://npmplus-two.public.example.net:81",
    "npm_public_identity": "public@example.org",
    "npm_public_secret": "public-secret",
    "vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}},
}


class LegacyMigrationTests(unittest.TestCase):
    def test_flat_legacy_keys_become_named_instances(self):
        config = normalize_config(LEGACY_CONFIG)
        self.assertEqual(sorted(config["instances"]), ["lab", "public"])
        self.assertEqual(config["instances"]["lab"]["url"], "https://npmplus-one.internal.example.net:81")
        self.assertEqual(config["instances"]["lab"]["secret"], "lab-secret")
        self.assertEqual(config["instances"]["lab"]["label"], "Lab NPMPlus")
        self.assertEqual(config["vlan_map"], {"10": {"instance": "lab", "zone": "internal.example.net"}})

    def test_saving_a_legacy_file_rewrites_it_without_the_flat_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.path.write_text(json.dumps(LEGACY_CONFIG), encoding="utf-8")
            saved = store.save({"public_domain": "example.net"})
            for stale in ("npm_lab_url", "npm_lab_identity", "npm_lab_secret", "npm_public_url"):
                self.assertNotIn(stale, saved)
            on_disk = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["instances"]["public"]["identity"], "public@example.org")
            self.assertEqual(on_disk["instances"]["public"]["secret"], "public-secret")


class NeutralInitialConfigTests(unittest.TestCase):
    def test_missing_settings_start_blank_or_in_safe_neutral_states(self):
        config = normalize_config({})
        self.assertEqual(config["pve_local_url"], "")
        self.assertEqual(config["public_domain"], "")
        self.assertEqual(config["excluded_guest_hostnames"], "")
        self.assertEqual(config["poll_interval_seconds"], None)
        self.assertEqual(config["max_deletes_per_run"], None)
        self.assertFalse(config["watch_enabled"])
        self.assertFalse(config["auto_apply"])
        self.assertFalse(config["delete_removed_hosts"])
        self.assertTrue(config["pve_local_verify_tls"])
        validate_config(config, allow_incomplete=True)

    def test_watching_requires_an_explicit_check_interval(self):
        config = normalize_config({"watch_enabled": True})
        with self.assertRaises(ValueError) as caught:
            validate_config(config, allow_incomplete=True)
        self.assertIn("Set the check interval", str(caught.exception))

    def test_automatic_deletion_requires_an_explicit_deletion_limit(self):
        config = normalize_config({"delete_removed_hosts": True})
        with self.assertRaises(ValueError) as caught:
            validate_config(config, allow_incomplete=True)
        self.assertIn("Set the deletion limit", str(caught.exception))

    def test_safety_switches_normalize_string_values_without_truthiness(self):
        config = normalize_config({
            "watch_enabled": "false",
            "auto_apply": "false",
            "delete_removed_hosts": "false",
        })
        self.assertEqual(
            (config["watch_enabled"], config["auto_apply"], config["delete_removed_hosts"]),
            (False, False, False),
        )
        enabled = normalize_config({
            "watch_enabled": "true",
            "auto_apply": "true",
            "delete_removed_hosts": "true",
        })
        self.assertEqual(
            (enabled["watch_enabled"], enabled["auto_apply"], enabled["delete_removed_hosts"]),
            (True, True, True),
        )

    def test_saving_a_non_boolean_safety_switch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            with self.assertRaisesRegex(ValueError, "watch_enabled.*true or false"):
                store.save({"watch_enabled": "false"})
            with self.assertRaisesRegex(ValueError, "auto_apply.*true or false"):
                store.save({"auto_apply": "false"})
            with self.assertRaisesRegex(ValueError, "delete_removed_hosts.*true or false"):
                store.save({"delete_removed_hosts": "false"})


class InstanceConfigTests(unittest.TestCase):
    def test_any_number_of_instances_can_be_mapped_by_any_number_of_vlans(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            config = store.save({
                "public_domain": "example.net",
                "instances": {
                    "lab": {"label": "Lab NPMPlus", "url": "https://internal.example.net:81", "identity": "a", "secret": "s1"},
                    "dmz": {"label": "", "url": "https://dmz.example.net:81", "identity": "b", "secret": "s2"},
                    "public": {"label": "Public", "url": "https://public.example.net:81", "identity": "c", "secret": "s3"},
                },
                "vlan_map": {
                    "10": {"instance": "lab", "zone": "internal.example.net"},
                    "30": {"instance": "DMZ", "zone": "dmz.example.net"},
                    "20": {"instance": "public", "zone": "public.example.net."},
                },
            })
            self.assertEqual(sorted(config["instances"]), ["dmz", "lab", "public"])
            # Instance names and zones are normalised on the way in.
            self.assertEqual(config["vlan_map"]["30"], {"instance": "dmz", "zone": "dmz.example.net"})
            self.assertEqual(config["vlan_map"]["20"]["zone"], "public.example.net")
            self.assertEqual(instance_label("dmz", config["instances"]["dmz"]), "dmz NPMPlus")

    def test_blank_secret_keeps_the_stored_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({"instances": {"lab": {"url": "https://internal.example.net:81", "identity": "a", "secret": "keep-me"}}})
            saved = store.save({
                "instances": {"lab": {"label": "Lab", "url": "https://internal.example.net:81", "identity": "a", "secret": ""}},
            })
            self.assertEqual(saved["instances"]["lab"]["secret"], "keep-me")
            self.assertEqual(saved["instances"]["lab"]["label"], "Lab")

    def test_unsubmitted_instance_keys_are_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({"instances": {"lab": {"url": "https://internal.example.net:81", "secret": "s1"}}})
            saved = store.save({"vlan_map": {"10": {"instance": "lab", "zone": "internal.example.net"}}})
            self.assertEqual(saved["instances"]["lab"]["url"], "https://internal.example.net:81")

    def test_removing_an_instance_drops_it_from_the_saved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({"instances": {"lab": {"url": "https://internal.example.net:81"}, "dmz": {"url": "https://dmz.example.net:81"}}})
            saved = store.save({"instances": {"lab": {"url": "https://internal.example.net:81"}}})
            self.assertEqual(sorted(saved["instances"]), ["lab"])

    def test_vlan_row_must_reference_a_configured_instance(self):
        config = normalize_config({
            "instances": {"lab": {"url": "https://internal.example.net:81"}},
            "vlan_map": {"10": {"instance": "dmz", "zone": "dmz.example.net"}},
        })
        with self.assertRaises(ValueError) as caught:
            validate_config(config, allow_incomplete=True)
        self.assertIn("not configured", str(caught.exception))

    def test_instance_name_must_be_a_slug(self):
        with self.assertRaises(ValueError) as caught:
            validate_config(normalize_config({"instances": {"lab one": {}}}), allow_incomplete=True)
        self.assertIn("Instance name", str(caught.exception))

    def test_every_vlan_row_needs_a_zone(self):
        with self.assertRaises(ValueError) as caught:
            validate_config(
                normalize_config({"instances": {"lab": {}}, "vlan_map": {"10": {"instance": "lab", "zone": " "}}}),
                allow_incomplete=True,
            )
        self.assertIn("upstream DNS zone", str(caught.exception))

    def test_merge_instances_rejects_non_objects(self):
        with self.assertRaises(ValueError):
            merge_instances({}, ["lab"])


class SelfSignedSwitchTests(unittest.TestCase):
    def test_verification_is_on_unless_the_switch_is_off(self):
        config = normalize_config({"instances": {"lab": {"url": "https://internal.example.net:81"}}})
        self.assertTrue(config["instances"]["lab"]["verify_tls"])
        self.assertTrue(config["pve_local_verify_tls"])

    def test_the_switch_round_trips_through_the_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({
                "pve_local_verify_tls": False,
                "instances": {"lab": {"url": "https://internal.example.net:81", "verify_tls": False}},
            })
            on_disk = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertFalse(on_disk["instances"]["lab"]["verify_tls"])
            self.assertFalse(on_disk["pve_local_verify_tls"])
            public = public_config(store.load())
        self.assertFalse(public["instances"]["lab"]["verify_tls"])
        self.assertFalse(public["pve_local_verify_tls"])

    def test_only_the_switched_instance_stops_verifying(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({
                "instances": {
                    "lab": {"url": "https://internal.example.net:81", "verify_tls": False},
                    "dmz": {"url": "https://dmz.example.net:81"},
                },
            })
            saved = store.load()
        self.assertFalse(saved["instances"]["lab"]["verify_tls"])
        self.assertTrue(saved["instances"]["dmz"]["verify_tls"])

    def test_a_hand_edited_spelling_is_accepted(self):
        config = normalize_config({
            "pve_local_verify_tls": "false",
            "instances": {
                "lab": {"verify_tls": 0},
                "dmz": {"verify_tls": "yes"},
                "public": {"verify_tls": "maybe"},
            },
        })
        self.assertFalse(config["pve_local_verify_tls"])
        self.assertFalse(config["instances"]["lab"]["verify_tls"])
        self.assertTrue(config["instances"]["dmz"]["verify_tls"])
        # An unrecognised value keeps the secure default rather than failing the file.
        self.assertTrue(config["instances"]["public"]["verify_tls"])

    def test_a_non_boolean_switch_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_config({"instances": {"lab": {"verify_tls": "maybe"}}}, allow_incomplete=True)
        self.assertIn("verify_tls", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            validate_config({"instances": {}, "pve_local_verify_tls": "maybe"}, allow_incomplete=True)
        self.assertIn("pve_local_verify_tls", str(caught.exception))


class PublicConfigTests(unittest.TestCase):
    def test_secrets_never_leave_the_server(self):
        config = normalize_config({
            "pve_local_password": "psl-password",
            "instances": {"lab": {"url": "https://internal.example.net:81", "identity": "a", "secret": "api-secret"}},
        })
        public = public_config(config)
        self.assertTrue(public["pve_local_password_configured"])
        self.assertNotIn("pve_local_password", public)
        self.assertEqual(
            public["plaintext_secrets"],
            ["PVE Scripts Local password", "NPMPlus instance 'lab' secret"],
        )
        self.assertEqual(public["instances"]["lab"], {
            "label": "",
            "url": "https://internal.example.net:81",
            "identity": "a",
            "secret_configured": True,
            "secret_env": "",
            "verify_tls": True,
        })
        self.assertNotIn("secret", public["instances"]["lab"])


class PlaintextSecretTests(unittest.TestCase):
    """A literal credential in the settings file is named, never valued."""

    def test_literal_secrets_are_listed_and_reference_free(self):
        config = normalize_config({
            "pve_local_password": "psl-password",
            "instances": {
                "lab": {"url": "https://internal.example.net:81", "secret": "api-secret"},
            },
        })
        self.assertEqual(
            plaintext_secret_fields(config),
            ["PVE Scripts Local password", "NPMPlus instance 'lab' secret"],
        )
        # Only the descriptions travel; the values never reach the page.
        self.assertNotIn("psl-password", json.dumps(public_config(config)))
        self.assertNotIn("api-secret", json.dumps(public_config(config)))

    def test_env_references_and_blank_fields_are_not_plaintext(self):
        config = normalize_config({
            "pve_local_password": "env:PVE_LOCAL_PASSWORD",
            "instances": {
                "lab": {"url": "https://a.example.net:81", "secret": "env:NPM_LAB_SECRET"},
                "dmz": {"url": "https://b.example.net:81"},
            },
        })
        self.assertEqual(plaintext_secret_fields(config), [])
        self.assertEqual(public_config(config)["plaintext_secrets"], [])


class SecretReferenceTests(unittest.TestCase):
    def test_env_reference_is_read_from_the_environment_at_use_time(self):
        environ = {"NPM_LAB_SECRET": "from-env"}
        self.assertEqual(resolve_secret("env:NPM_LAB_SECRET", environ), "from-env")
        self.assertEqual(resolve_secret("hunter2", environ), "hunter2")
        # An unset variable resolves to nothing so the caller can report it.
        self.assertEqual(resolve_secret("env:NPM_ABSENT", environ), "")

    def test_only_a_leading_env_prefix_marks_a_reference(self):
        self.assertEqual(secret_env_name("env:NPM_LAB_SECRET"), "NPM_LAB_SECRET")
        self.assertEqual(secret_env_name("ENV:FOO"), "FOO")
        self.assertEqual(secret_env_name("env: FOO "), "FOO")
        self.assertEqual(secret_env_name("env:"), "")
        for literal in ("hunter2", "envoy-pass", "environment", "", None):
            self.assertIsNone(secret_env_name(literal), literal)

    def test_a_malformed_reference_is_rejected_by_validation(self):
        config = normalize_config({"instances": {"lab": {"secret": "env:not a name"}}})
        with self.assertRaises(ValueError) as caught:
            validate_config(config, allow_incomplete=True)
        self.assertIn("NPMPlus instance 'lab' secret", str(caught.exception))
        self.assertIn("not a valid environment variable name", str(caught.exception))

    def test_a_legacy_custom_reference_loads_and_can_be_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.path.write_text(
                json.dumps({"pve_local_password": "env:LAB_SECRET"}),
                encoding="utf-8",
            )
            loaded = store.load()
            self.assertEqual(loaded["pve_local_password"], "env:LAB_SECRET")
            with self.assertRaisesRegex(ValueError, "variable assigned to this secret field"):
                validate_config(loaded, allow_incomplete=True)
            saved = store.save({"pve_local_password": "env:PVE_LOCAL_PASSWORD"})
            self.assertEqual(saved["pve_local_password"], "env:PVE_LOCAL_PASSWORD")

    def test_the_psl_password_reference_is_validated_too(self):
        with self.assertRaises(ValueError) as caught:
            validate_config(normalize_config({"pve_local_password": "env:1BAD"}), allow_incomplete=True)
        self.assertIn("PVE Scripts Local password", str(caught.exception))

    def test_secret_references_must_use_the_supported_variable_names(self):
        with self.assertRaises(ValueError) as caught:
            validate_config(
                normalize_config({"pve_local_password": "env:APP_PASSWORD"}),
                allow_incomplete=True,
            )
        self.assertIn("PVE_LOCAL_PASSWORD", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            validate_config(
                normalize_config({"instances": {"lab": {"secret": "env:PATH"}}}),
                allow_incomplete=True,
            )
        self.assertIn("NPM_<INSTANCE>_SECRET", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            validate_config(
                normalize_config({"instances": {"lab": {"secret": "env:NPM_PUBLIC_SECRET"}}}),
                allow_incomplete=True,
            )
        self.assertIn("variable assigned to this secret field", str(caught.exception))

    def test_a_well_formed_reference_passes_validation(self):
        validate_config(normalize_config({
            "pve_local_password": "env:PVE_LOCAL_PASSWORD",
            "instances": {"lab": {"secret": "env:NPM_LAB_SECRET"}},
        }), allow_incomplete=True)

    def test_every_unset_reference_is_reported_by_name(self):
        config = normalize_config({
            "pve_local_password": "env:PVE_LOCAL_PASSWORD",
            "instances": {"lab": {"secret": "env:NPM_LAB_SECRET"}, "dmz": {"secret": "literal"}},
        })
        self.assertEqual(unresolved_secret_references(config, {}), [
            ("PVE Scripts Local password", "PVE_LOCAL_PASSWORD"),
            ("NPMPlus instance 'lab' secret", "NPM_LAB_SECRET"),
        ])
        with self.assertRaises(ValueError) as caught:
            check_secret_references(config, {})
        self.assertIn("PVE_LOCAL_PASSWORD", str(caught.exception))
        self.assertIn("NPM_LAB_SECRET", str(caught.exception))

    def test_a_set_reference_passes_the_check(self):
        config = normalize_config({
            "pve_local_password": "env:PVE_LOCAL_PASSWORD",
            "instances": {"lab": {"secret": "env:NPM_LAB_SECRET"}},
        })
        check_secret_references(config, {"PVE_LOCAL_PASSWORD": "x", "NPM_LAB_SECRET": "y"})
        # Only the variable that is actually missing is reported.
        self.assertEqual(
            unresolved_secret_references(config, {"NPM_LAB_SECRET": "y"}),
            [("PVE Scripts Local password", "PVE_LOCAL_PASSWORD")],
        )

    def test_a_saved_reference_keeps_the_secret_out_of_the_file(self):
        environ = {"NPM_LAB_SECRET": "from-env"}
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({
                "instances": {"lab": {"url": "https://internal.example.net:81", "identity": "a", "secret": "env:NPM_LAB_SECRET"}},
            })
            on_disk = store.path.read_text(encoding="utf-8")
            self.assertIn("env:NPM_LAB_SECRET", on_disk)
            self.assertNotIn("from-env", on_disk)
            stored = store.load()
        public = public_config(stored, environ)
        self.assertTrue(public["instances"]["lab"]["secret_configured"])
        self.assertEqual(public["instances"]["lab"]["secret_env"], "NPM_LAB_SECRET")
        self.assertFalse(public_config(stored, {})["instances"]["lab"]["secret_configured"])
        self.assertNotIn("from-env", json.dumps(public))

    def test_a_blank_submit_keeps_a_stored_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            store.save({"instances": {"lab": {"url": "https://internal.example.net:81", "secret": "env:NPM_LAB_SECRET"}}})
            saved = store.save({"instances": {"lab": {"url": "https://internal.example.net:81", "identity": "b", "secret": ""}}})
            self.assertEqual(saved["instances"]["lab"]["secret"], "env:NPM_LAB_SECRET")

    def test_the_psl_password_reference_is_reported_by_name(self):
        config = normalize_config({"pve_local_password": "env:PVE_LOCAL_PASSWORD"})
        public = public_config(config, {})
        self.assertNotIn("pve_local_password", public)
        self.assertFalse(public["pve_local_password_configured"])
        self.assertEqual(public["pve_local_password_env"], "PVE_LOCAL_PASSWORD")
        self.assertTrue(public_config(config, {"PVE_LOCAL_PASSWORD": "x"})["pve_local_password_configured"])


if __name__ == "__main__":
    unittest.main()
