import unittest

from reverse_proxy_automation.plugins.npmplus.client import select_certificate, select_shared_certificate
from reverse_proxy_automation.integrations import normalize_psl_record
from reverse_proxy_automation.planner import (
    parse_aliases,
    parse_exclusions,
    plan_hosts,
    vlan_from_address,
)


VLAN_MAP = {
    "10": {"instance": "lab", "zone": "internal.example.net"},
    "20": {"instance": "public", "zone": "public.example.net"},
}


def guest(hostname, vlan="10", port=8080, **overrides):
    script = {
        "id": 1,
        "script_name": hostname,
        "container_id": "101",
        "status": "success",
        "web_ui_port": port,
        "is_vm": False,
        "lxc_config": {"hostname": hostname, "net_vlan": vlan},
    }
    script.update(overrides)
    return normalize_psl_record(script)


class PlannerTests(unittest.TestCase):
    def test_routes_by_vlan_and_uses_guest_dns_zone_and_port(self):
        result = plan_hosts([guest("photos", "20", 8081)], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [{
            "source_id": "1",
            "container_id": "101",
            "guest_hostname": "photos",
            "instance": "public",
            "domain_name": "photos.example.net",
            "domain_names": ["photos.example.net"],
            "forward_scheme": "http",
            "forward_host": "photos.public.example.net",
            "forward_port": 8081,
        }])

    def test_uses_cached_guest_hostname_over_script_label(self):
        result = plan_hosts([guest("display-name", lxc_config={"hostname": "manual-name", "net_vlan": "10"})], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"][0]["domain_name"], "manual-name.example.net")

    def test_script_name_ending_in_sh_yields_the_word_before_the_period(self):
        result = plan_hosts([guest("photos.sh", lxc_config={"net_vlan": "10"})], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [{
            "source_id": "1",
            "container_id": "101",
            "guest_hostname": "photos",
            "instance": "lab",
            "domain_name": "photos.example.net",
            "domain_names": ["photos.example.net"],
            "forward_scheme": "http",
            "forward_host": "photos.internal.example.net",
            "forward_port": 8080,
        }])

    def test_cached_hostname_ending_in_sh_is_trimmed_too(self):
        result = plan_hosts([guest("display", lxc_config={"hostname": "photos.sh", "net_vlan": "10"})], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"][0]["guest_hostname"], "photos")
        self.assertEqual(result["plan"][0]["forward_host"], "photos.internal.example.net")

    def test_sh_suffix_matches_exclusions_and_ignores_case(self):
        result = plan_hosts(
            [guest("npmplus-one.sh"), guest("Photos.SH", "20", container_id="102")],
            VLAN_MAP,
            "example.net",
            exclusions=parse_exclusions("npmplus-one"),
        )
        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["photos"])
        self.assertEqual(result["skipped"], [("npmplus-one", "excluded proxy guest")])

    def test_empty_exclusion_list_allows_previously_excluded_guests(self):
        result = plan_hosts(
            [guest("npmplus-one")],
            VLAN_MAP,
            "example.net",
            exclusions=parse_exclusions(""),
        )
        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["npmplus-one"])
        self.assertEqual(result["skipped"], [])

    def test_configured_exclusions_do_not_implicitly_add_defaults(self):
        self.assertEqual(parse_exclusions("photos, wiki"), {"photos", "wiki"})

    def test_only_sh_names_gain_a_hostname(self):
        result = plan_hosts([guest("photos.bak"), guest(".sh", container_id="102")], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertEqual(
            result["skipped"],
            [(".sh", "missing or invalid guest hostname"), ("photos.bak", "missing or invalid guest hostname")],
        )

    def test_an_sh_guest_counts_as_present(self):
        result = plan_hosts([guest("photos.sh", port=None)], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["present"], {"photos.example.net"})

    def test_skips_proxy_guests_and_unknown_vlan_and_missing_port(self):
        result = plan_hosts([
            guest("npmplus-one"),
            guest("unmapped", "999", container_id="102"),
            guest("no-port", port=None, container_id="103"),
        ], VLAN_MAP, "example.net", exclusions={"npmplus-one"})
        self.assertEqual(result["plan"], [])
        self.assertEqual(len(result["skipped"]), 3)

    def test_skips_vms_without_exposed_vlan_config(self):
        vm = guest("vm-app", "10", is_vm=True)
        result = plan_hosts([vm], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertIn("VM VLAN config", result["skipped"][0][1])

    def test_failed_psl_record_is_planned_when_its_routing_data_is_valid(self):
        script = guest("new-container", status="failed")

        result = plan_hosts([script], VLAN_MAP, "example.net")

        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["new-container"])
        self.assertEqual(result["skipped"], [])

    def test_psl_record_without_container_id_is_reported_as_skipped(self):
        script = guest("new-container", container_id=None)

        result = plan_hosts([script], VLAN_MAP, "example.net")

        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [
            ("new-container", "PSL record has no container ID"),
        ])

    def test_duplicate_hostnames_fail_closed(self):
        # Two containers that want one name: neither is planned, whichever record
        # arrived last.
        result = plan_hosts([guest("same"), guest("same", "20", container_id="102")], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertEqual(len(result["skipped"]), 2)

    def test_any_configured_instance_name_can_own_a_vlan(self):
        vlan_map = {"10": {"instance": "lab", "zone": "internal.example.net"}, "40": {"instance": "dmz", "zone": "dmz.example.net"}}
        result = plan_hosts([guest("jump", "40", 443)], vlan_map, "example.net")
        self.assertEqual(result["plan"][0]["instance"], "dmz")
        self.assertEqual(result["plan"][0]["forward_host"], "jump.dmz.example.net")

    def test_vlan_without_a_mapping_is_skipped_not_routed(self):
        result = plan_hosts([guest("orphan", "999")], {}, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertIn("no NPMPlus instance mapped", result["skipped"][0][1])

    def test_deployment_without_lxc_config_routes_by_the_vlan_in_the_ip(self):
        # Some PSL deployments omit lxc_config and report only the script name
        # and the guest's web UI address.
        result = plan_hosts([guest("photos.sh", lxc_config=None, web_ui_ip="127.10.10.107")], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [{
            "source_id": "1",
            "container_id": "101",
            "guest_hostname": "photos",
            "instance": "lab",
            "domain_name": "photos.example.net",
            "domain_names": ["photos.example.net"],
            "forward_scheme": "http",
            "forward_host": "photos.internal.example.net",
            "forward_port": 8080,
        }])

    def test_unmapped_vlan_from_an_ip_names_the_address_it_was_read_from(self):
        result = plan_hosts([guest("photos", lxc_config=None, web_ui_ip="127.10.10.107")], {}, "example.net")
        self.assertEqual(result["skipped"], [
            ("photos", "no NPMPlus instance mapped for VLAN 10 (from web_ui_ip 127.10.10.107)"),
        ])

    def test_ip_that_does_not_repeat_its_tag_is_left_untagged(self):
        script = guest("photos", lxc_config=None, web_ui_ip="192.0.2.107")
        result = plan_hosts([script], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [
            ("photos", "no NPMPlus instance mapped for VLAN untagged (no lxc_config, and web_ui_ip 192.0.2.107 carries no VLAN)"),
        ])

    def test_cached_lxc_config_without_a_tag_stays_untagged(self):
        script = guest("photos", lxc_config={"hostname": "photos"}, web_ui_ip="127.10.10.107")
        result = plan_hosts([script], VLAN_MAP, "example.net")
        self.assertEqual(result["skipped"], [("photos", "no NPMPlus instance mapped for VLAN untagged")])

    def test_a_guest_with_no_config_and_no_address_says_so(self):
        result = plan_hosts([guest("photos", lxc_config=None)], VLAN_MAP, "example.net")
        self.assertEqual(result["skipped"], [
            ("photos", "no NPMPlus instance mapped for VLAN untagged (no lxc_config, and no web_ui_ip to read a VLAN from)"),
        ])

    def test_only_a_repeated_octet_is_read_as_a_vlan_tag(self):
        readings = {
            "127.2.2.107": "2",
            "127.02.02.107": "2",
            "127.5.5.20": "5",
            "127.2.5.20": "",
            "127.0.0.5": "",
            "203.0.113.10": "",
            "10.2.2": "",
            "127.2.2.2.2": "",
            "127.300.300.5": "",
            "127.2.2.1000": "",
            "photos": "",
            "": "",
            None: "",
        }
        for address, expected in readings.items():
            self.assertEqual(vlan_from_address(address), expected, address)

    def test_rejects_invalid_public_domain(self):
        with self.assertRaises(ValueError):
            plan_hosts([], VLAN_MAP, "bad domain")


class ContainerIdentityTests(unittest.TestCase):
    """PSL keeps a record per install attempt, so one container can be described twice."""

    @staticmethod
    def record(script_name, vlan, port, record_id, container_id, status="success", address=None):
        return guest(
            script_name,
            vlan,
            port,
            id=record_id,
            container_id=container_id,
            status=status,
            lxc_config=None if address else {"hostname": script_name, "net_vlan": vlan},
            web_ui_ip=address,
        )

    def test_two_records_of_one_container_plan_one_guest(self):
        # An install attempt failed and was retried under a new script name.
        # PSL kept both records, but one container should produce one proxy host.
        stale = self.record("previous-app.sh", "10", 5000, 1, "100", status="failed", address="127.10.10.156")
        fresh = self.record("current-app.sh", "10", 5000, 2, "100", address="127.10.10.156")
        result = plan_hosts([stale, fresh], VLAN_MAP, "example.net")
        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["current-app"])
        self.assertEqual(result["plan"][0]["container_id"], "100")
        self.assertEqual(result["plan"][0]["source_id"], "2")
        self.assertEqual(result["containers"], 1)

    def test_the_record_that_states_the_vlan_is_the_one_that_routes(self):
        # The older record states no address, so the newer record's address is what
        # puts the container on VLAN 10.
        older = self.record("previous-app.sh", "10", 5000, 1, "100", status="failed")
        newer = self.record("current-app.sh", "10", 5000, 2, "100", address="127.10.10.156")
        result = plan_hosts([older, newer], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"][0]["forward_host"], "current-app.internal.example.net")

    def test_the_order_psl_lists_the_records_in_does_not_decide(self):
        stale = self.record("previous-app.sh", "10", 5000, 1, "100", status="failed", address="127.10.10.156")
        fresh = self.record("current-app.sh", "10", 5000, 2, "100", address="127.10.10.156")
        for records in ([stale, fresh], [fresh, stale]):
            result = plan_hosts(records, VLAN_MAP, "example.net")
            self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["current-app"], records)

    def test_superseded_install_records_are_not_reported_as_skipped(self):
        older = self.record("previous-app.sh", "10", 5000, 1, "100", status="failed", address="127.10.10.156")
        newer = self.record("current-app.sh", "10", 5000, 2, "100", address="127.10.10.156")
        result = plan_hosts([older, newer], VLAN_MAP, "example.net")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["present"], {"current-app.example.net"})

    def test_newest_successful_record_beats_a_more_complete_older_record(self):
        working = self.record("photos.sh", "10", 8080, 120, "101")
        current = self.record("photos-new.sh", "20", 0, 121, "101")
        result = plan_hosts([working, current], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [("photos-new", "web_ui_port is missing or invalid")])

    def test_a_record_that_cannot_be_refreshed_is_skipped_instead_of_routed_stale(self):
        current = self.record("photos.sh", "10", 8080, 120, "101")
        current["refresh_error"] = "Could not refresh container 101: SSH connection failed"

        result = plan_hosts([current], VLAN_MAP, "example.net")

        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [
            ("photos", "Could not refresh container 101: SSH connection failed"),
        ])
        self.assertEqual(result["present"], {"photos.example.net"})
        self.assertEqual(result["containers"], 1)

    def test_refresh_failure_uses_a_saved_ip_and_port_without_an_error_row(self):
        current = self.record("photos.sh", "10", 8080, 120, "101", address="127.10.10.5")
        current["refresh_error"] = "Could not refresh container 101: SSH connection failed"

        result = plan_hosts([current], VLAN_MAP, "example.net")

        self.assertEqual(len(result["plan"]), 1)
        self.assertEqual(result["plan"][0]["forward_port"], 8080)
        self.assertEqual(result["skipped"], [])

    def test_refresh_failure_without_a_saved_port_still_skips_the_record(self):
        current = self.record("photos.sh", "10", None, 120, "101", address="127.10.10.5")
        current["refresh_error"] = "Could not refresh container 101: SSH connection failed"

        result = plan_hosts([current], VLAN_MAP, "example.net")

        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [
            ("photos", "Could not refresh container 101: SSH connection failed"),
        ])

    def test_newest_record_is_selected_regardless_of_psl_status(self):
        working = self.record("photos.sh", "10", 8080, 120, "101")
        retry = self.record("photos2.sh", "20", 8080, 121, "101", status="failed")
        result = plan_hosts([working, retry], VLAN_MAP, "example.net")
        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["photos2"])
        self.assertEqual(result["plan"][0]["forward_host"], "photos2.public.example.net")
        self.assertEqual(result["skipped"], [])

    def test_refresh_errors_on_superseded_records_are_not_reported(self):
        older = self.record("oldphotos.sh", "10", 8080, 120, "101", status="failed")
        older["refresh_error"] = "Could not refresh old PSL record"
        current = self.record("photos.sh", "10", 8080, 121, "101")

        result = plan_hosts([older, current], VLAN_MAP, "example.net")

        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["photos"])
        self.assertEqual(result["skipped"], [])

    def test_the_newest_record_names_the_container_when_both_can_route(self):
        older = self.record("oldname.sh", "10", 8080, 40, "101")
        newer = self.record("newname.sh", "10", 8080, 41, "101")
        result = plan_hosts([older, newer], VLAN_MAP, "example.net")
        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["newname"])
        # The name the container no longer uses is not a guest of its own, so a host
        # recorded for it is no longer kept alive by this run.
        self.assertEqual(result["present"], {"newname.example.net"})

    def test_records_without_a_container_id_are_never_merged(self):
        # Nothing ties these records to a container, so they stay two guests: they
        # are counted twice, and neither of them routes.
        result = plan_hosts(
            [guest("same", container_id=None), guest("same", "20", container_id=None)],
            VLAN_MAP,
            "example.net",
        )
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [
            ("same", "PSL record has no container ID"),
            ("same", "PSL record has no container ID"),
        ])
        self.assertEqual(result["containers"], 2)
        self.assertEqual(result["present"], {"same.example.net"})

    def test_a_record_without_a_container_id_is_not_routed(self):
        result = plan_hosts([guest("solo", container_id=None)], VLAN_MAP, "example.net")
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["present"], {"solo.example.net"})

    def test_each_container_is_counted_once(self):
        result = plan_hosts([
            guest("a", container_id="1"),
            guest("b", container_id="2"),
            guest("a", container_id="1"),
        ], VLAN_MAP, "example.net")
        self.assertEqual(result["containers"], 2)
        self.assertEqual([row["guest_hostname"] for row in result["plan"]], ["a", "b"])


class AliasTests(unittest.TestCase):
    """Aliases give one guest's host a second, third or further name."""

    def test_a_bare_alias_gets_the_public_domain_and_a_dotted_one_is_kept(self):
        result = plan_hosts([guest("media-service")], VLAN_MAP, "example.net", aliases="media-service = photos")
        self.assertEqual(result["plan"][0]["domain_names"], ["media-service.example.net", "photos.example.net"])
        self.assertEqual(result["plan"][0]["domain_name"], "media-service.example.net")
        parsed = parse_aliases("media-service = photos, pictures.example.org\n# comment\n\n", "example.net")
        self.assertEqual(parsed, {"media-service": ["photos.example.net", "pictures.example.org"]})

    def test_several_aliases_keep_their_order_and_drop_repeats(self):
        result = plan_hosts(
            [guest("media-service")],
            VLAN_MAP,
            "example.net",
            aliases={"media-service": ["photos", "photos", "media-service.example.net", "pictures"]},
        )
        self.assertEqual(result["plan"][0]["domain_names"], ["media-service.example.net", "photos.example.net", "pictures.example.net"])

    def test_an_alias_counts_as_the_guest_still_being_here(self):
        result = plan_hosts([guest("media-service")], VLAN_MAP, "example.net", aliases="media-service = photos")
        self.assertEqual(result["present"], {"media-service.example.net", "photos.example.net"})
        self.assertEqual(result["names_by_domain"]["media-service.example.net"], ["media-service.example.net", "photos.example.net"])

    def test_entry_aliases_are_scoped_to_the_instance_and_guest(self):
        result = plan_hosts(
            [guest("photos", "10"), guest("wiki", "20", container_id="102")],
            VLAN_MAP,
            "example.net",
            instance_aliases={
                "lab:photos": ["gallery"],
                "public:wiki": ["docs.example.org"],
            },
        )
        by_guest = {row["guest_hostname"]: row for row in result["plan"]}
        self.assertEqual(
            by_guest["photos"]["domain_names"],
            ["photos.example.net", "gallery.example.net"],
        )
        self.assertEqual(
            by_guest["wiki"]["domain_names"],
            ["wiki.example.net", "docs.example.org"],
        )
        self.assertIn("gallery.example.net", result["present"])

    def test_entry_aliases_replace_legacy_aliases_for_that_route(self):
        result = plan_hosts(
            [guest("photos", "10")],
            VLAN_MAP,
            "example.net",
            aliases="photos = old-name",
            instance_aliases={"lab:photos": ["new-name"]},
        )
        self.assertEqual(
            result["plan"][0]["domain_names"],
            ["photos.example.net", "new-name.example.net"],
        )

    def test_a_routable_guest_keeps_a_name_another_guest_claims_as_its_own(self):
        # photos is a guest of its own here, so media-service may not also answer to it.
        result = plan_hosts(
            [guest("media-service"), guest("photos", "20", container_id="102")],
            VLAN_MAP,
            "example.net",
            aliases="media-service = photos",
        )
        by_name = {row["guest_hostname"]: row["domain_names"] for row in result["plan"]}
        self.assertEqual(by_name["media-service"], ["media-service.example.net"])
        self.assertEqual(by_name["photos"], ["photos.example.net"])
        self.assertEqual(result["skipped"], [
            ("media-service", "alias photos.example.net is already served by guest photos"),
        ])

    def test_two_guests_may_not_claim_one_alias(self):
        result = plan_hosts(
            [guest("media-service"), guest("video-service", "20", container_id="102")],
            VLAN_MAP,
            "example.net",
            aliases="media-service = media\nvideo-service = media",
        )
        by_name = {row["guest_hostname"]: row["domain_names"] for row in result["plan"]}
        self.assertEqual(by_name["media-service"], ["media-service.example.net", "media.example.net"])
        self.assertEqual(by_name["video-service"], ["video-service.example.net"])
        self.assertEqual(result["skipped"], [
            ("video-service", "alias media.example.net is already served by guest media-service"),
        ])

    def test_an_unroutable_guest_still_declares_the_names_its_host_must_serve(self):
        # The guest has no usable port, so it is not routed, but a host already
        # recorded for it may only be deleted while it serves these names.
        result = plan_hosts([guest("media-service", port=None)], VLAN_MAP, "example.net", aliases="media-service = photos")
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["names_by_domain"], {"media-service.example.net": ["media-service.example.net", "photos.example.net"]})

    def test_aliases_do_not_route_an_excluded_or_unknown_guest(self):
        result = plan_hosts(
            [guest("npmplus-one")],
            VLAN_MAP,
            "example.net",
            exclusions={"npmplus-one"},
            aliases="npmplus-one = admin",
        )
        self.assertEqual(result["plan"], [])
        self.assertEqual(result["skipped"], [("npmplus-one", "excluded proxy guest")])

    def test_a_malformed_alias_line_is_reported_not_ignored(self):
        readings = {
            "media-service photos": "must read 'guest = name, name'",
            "media-service =": "must read 'guest = name, name'",
            "= photos": "must read 'guest = name, name'",
            "bad name = photos": "which is not a guest hostname",
            "media-service = bad name": "which is not a hostname",
            "media-service = media-service": "repeats the guest's own hostname media-service.example.net",
            "media-service = ,,": "lists no alias name",
            "media-service = photos\nmedia-service = pictures": "a second time",
        }
        for text, expected in readings.items():
            with self.assertRaises(ValueError, msg=text) as caught:
                parse_aliases(text, "example.net")
            self.assertIn(expected, str(caught.exception), text)



    def test_prefers_certificate_covering_domain_wildcard(self):
        certificates = [
            {"id": 8, "domain_names": ["other.example.net"]},
            {"id": 9, "domain_names": ["*.example.net"]},
        ]
        self.assertEqual(select_certificate(certificates, "photos.example.net")["id"], 9)

    def test_no_matching_certificate(self):
        self.assertIsNone(select_certificate([], "photos.example.net"))

    def test_a_shared_certificate_has_to_cover_every_name_a_host_serves(self):
        certificates = [
            {"id": 8, "domain_names": ["media-service.example.net"]},
            {"id": 9, "domain_names": ["*.example.net"]},
        ]
        names = ["media-service.example.net", "photos.example.net"]
        self.assertEqual(select_shared_certificate(certificates, names)["id"], 9)
        # A name outside the wildcard is not covered by that certificate at all.
        self.assertIsNone(select_shared_certificate(
            [certificates[1]], ["media-service.example.net", "photos.other.net"]
        ))


if __name__ == "__main__":
    unittest.main()