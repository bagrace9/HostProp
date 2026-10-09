from pathlib import Path
import unittest

from reverse_proxy_automation.webapp import WEB_DIR


class DashboardAssetTests(unittest.TestCase):
    def test_index_html_ships_in_the_web_directory(self):
        asset = WEB_DIR / "index.html"
        self.assertTrue(asset.is_file(), f"Dashboard asset is missing: {asset}")
        page = asset.read_text(encoding="utf-8")
        # Guard against the dashboard being moved without updating WEB_DIR.
        for element_id in ("pve-url", "pve-insecure", "vlan-groups", "instance-rows", "add-instance", "generated", "status-message", "apply-now", "entries", "plan-entries", "refresh-entries", "panel-hosts"):
            self.assertIn(f'id="{element_id}"', page)

    def test_the_dashboard_uses_the_entries_endpoints(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # Managing entries is what the page is for now: listing them, changing
        # ownership and deleting one all have to reach the service.
        for path in ('"/api/entries"', '"/api/entries/ownership"', '"/api/entries/aliases"', '"/api/entries/port"', '"/api/entries/delete"'):
            self.assertIn(path, page)

    def test_instance_choices_are_not_hardcoded_in_the_dashboard(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # VLAN rows must be filled from the configured instances at runtime, so no
        # instance-specific field ids or option values may be baked into the page.
        for stale in ("npm-lab-url", "npm-public-url", "npm_lab_secret", "npm_public_secret", '<option value="lab"', 'data-test="lab"'):
            self.assertNotIn(stale, page)

    def test_new_configuration_fields_start_without_preset_values(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        for preset in ('value="bagrace.net"', 'value="npmplus-lab,npmplus-public"', 'value="300"', 'value="10"'):
            self.assertNotIn(preset, page)
        self.assertIn('config.poll_interval_seconds===""?null:Number(config.poll_interval_seconds)', page)
        self.assertIn('config.max_deletes_per_run===""?null:Number(config.max_deletes_per_run)', page)
        self.assertIn('$("delete-removed").checked=Boolean(config.delete_removed_hosts)', page)

    def test_self_signed_switches_are_off_when_tls_verification_is_on(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("verifyInput.checked=verifyTls===false", page)
        self.assertIn('verify_tls:!row.querySelector(".instance-verify").checked', page)
        self.assertIn('$("pve-insecure").checked=config.pve_local_verify_tls===false', page)
        self.assertIn('config.pve_local_verify_tls=!$("pve-insecure").checked', page)
    def test_the_dashboard_reports_failures_honestly(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # A toast carries a kind so a failed call cannot look like a success, and an
        # error stays on screen until it is dismissed or replaced.
        self.assertIn('node.classList.toggle("error",kind==="error")', page)
        self.assertIn('if(kind==="error")return', page)
        self.assertIn('toast(error.message,"error")', page)
        # Preview and Apply save the page first, so the service never runs a stale
        # settings file, and the toast repeats what the returned status said.
        self.assertIn("await saveConfig();toast(`${verb} started;", page)
        self.assertIn("const status=await api(path,{})", page)
        self.assertIn('else if(status.state==="error")toast(`${verb} failed: ', page)
        self.assertIn('if(status.state==="busy")toast(status.message,"error")', page)

    def test_api_errors_explain_file_urls_and_non_json_responses(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('if(location.protocol==="file:")', page)
        self.assertIn("opening index.html directly cannot reach the dashboard API", page)
        self.assertIn("const raw=await response.text()", page)
        self.assertIn("Dashboard API returned a non-JSON response (HTTP ${response.status})", page)

    def test_the_entries_table_groups_and_labels_each_row(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # Each instance gets a header row with its own counts, and a row carries the
        # ownership tag plus whatever the last check said about that hostname.
        self.assertIn("function summarizeEntries(hosts)", page)
        self.assertIn("instance-head-row", page)
        self.assertIn("paintEntryNames(names,entry)", page)
        self.assertIn("function entryStatusBadge(entry)", page)
        self.assertIn('chip.className="alias-chip"', page)
        self.assertIn("<th>Actions</th>", page)
        # Delete is a quiet button: the destructive action no longer shouts.
        self.assertIn('drop.className="button ghost"', page)
        for state in ("managed", "adopted", "unmanaged"):
            self.assertIn(f'<span class="tag {state}">', page)

    def test_a_skipped_plan_item_is_still_labelled_in_the_entries_table(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # A pre-plan skip row is only {name, reason}: no status and no instance, so the
        # entries table has to derive the status the same way the reconciliation table
        # does, or such an entry would show no badge and no tint at all.
        self.assertIn("item.status||plannerSkipStatus(item.reason)", page)
        self.assertIn("!item.instance||item.instance===entry.instance", page)

    def test_host_management_displays_the_latest_plan_and_skipped_records(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="plan-entries"', page)
        self.assertIn("function renderPlan(status)", page)
        self.assertIn("status.planned||[]", page)
        self.assertIn("status.skipped||[]", page)
        self.assertIn("renderPlan(status)", page)
        self.assertIn("It does not create NPMPlus entries; use Apply to write changes.", page)

    def test_preview_and_apply_request_fresh_lxc_configuration(self):
        from reverse_proxy_automation import webapp

        source = Path(webapp.__file__).read_text(encoding="utf-8")
        self.assertIn('path == "/api/preview"', source)
        self.assertIn('path == "/api/apply"', source)
        self.assertEqual(source.count("refresh_lxc_config=True"), 2)
        service_source = Path(webapp.__file__).with_name("service.py").read_text(encoding="utf-8")
        self.assertIn("refresh_lxc_config=True,", service_source)
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("Refreshing PSL records…", page)
        self.assertIn('status.state==="busy"', page)

    def test_releasing_a_managed_entry_is_confirmed(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # Releasing a managed entry cannot be undone in one click, so the page has to
        # say so: the next run reports a conflict for that name until it is adopted.
        self.assertIn('action==="release"&&entry.ownership==="managed"', page)
        self.assertIn("reports a conflict for this name", page)

    def test_the_page_tracks_unsaved_settings_and_stale_entries(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # The header says when the form holds settings this machine does not have.
        self.assertIn('id="unsaved"', page)
        self.assertIn("function formState()", page)
        # A finished check reloads the entries, so a row's buttons are never stale.
        self.assertIn("checkKey!==lastCheckKey", page)


    def test_the_dashboard_sections_are_grouped_into_four_tabs(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        # PSL, NPMPlus instances, VLAN routing, and per-host management each have a
        # dedicated tab. A tab names the panel it opens and a panel names its tab.
        self.assertIn('role="tablist"', page)
        for name in ("psl", "npm", "vlan", "hosts"):
            self.assertIn(f'id="tab-{name}"', page)
            self.assertIn(f'id="panel-{name}"', page)
            self.assertIn(f'data-tab="{name}"', page)
            self.assertIn(f'aria-controls="panel-{name}"', page)
            self.assertIn(f'aria-labelledby="tab-{name}"', page)
        self.assertIn('id="panel-psl" role="tabpanel" aria-labelledby="tab-psl">', page)
        self.assertIn('id="panel-npm" role="tabpanel" aria-labelledby="tab-npm" hidden>', page)
        self.assertIn('id="panel-vlan" role="tabpanel" aria-labelledby="tab-vlan" hidden>', page)
        self.assertIn('id="panel-hosts" role="tabpanel" aria-labelledby="tab-hosts" hidden>', page)
        # Each setting sits inside the panel that owns it: the PSL endpoint, then the
        # instance rows, then the VLAN cards, each after its own panel has opened, and
        # the result tables stay outside the settings card altogether.
        self.assertLess(page.index('id="panel-psl"'), page.index('id="pve-url"'))
        self.assertLess(page.index('id="pve-url"'), page.index('id="panel-npm"'))
        self.assertLess(page.index('id="panel-npm"'), page.index('id="instance-rows"'))
        self.assertLess(page.index('id="instance-rows"'), page.index('id="panel-vlan"'))
        self.assertLess(page.index('id="panel-vlan"'), page.index('id="vlan-groups"'))
        self.assertLess(page.index('id="panel-hosts"'), page.index('id="entries"'))
        self.assertNotIn("Latest reconciliation", page)
        self.assertNotIn('id="results"', page)
        # All three tabs save through the one settings file, so each carries a button.
        for button in ("save-config", "save-instances", "save-vlans"):
            self.assertIn(f'id="{button}"', page)
        # Tabs switch by click and by arrow key, with one tab reachable by Tab at a time.
        self.assertIn("function tabButtons()", page)
        self.assertIn("function showTab(name,focus)", page)
        self.assertIn("tab.onkeydown=event=>{", page)
        self.assertIn('"ArrowRight"?1:event.key==="ArrowLeft"?-1:0', page)
        self.assertIn('tab.tabIndex=selected?0:-1', page)
        # A closed panel must not hide an unsaved edit: the tab holding it is marked.
        self.assertIn("function paintTabs(dirty)", page)
        self.assertIn("panelSnapshots=snapshotPanels()", page)
        self.assertIn(".tab.dirty::after", page)
        self.assertIn('data-tab="hosts"', page)
        self.assertIn("function saveEntryAliases(entry,list,button)", page)
        self.assertIn("Aliases for this guest", page)
        self.assertIn("function saveEntryPort(entry,input,button)", page)
        self.assertIn("Port override for", page)
        self.assertIn("Leave the port override blank to use PSL detection", page)

    def test_guest_aliases_are_only_edited_in_host_management(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="aliases"', page)
        self.assertNotIn('aliases:"aliases"', page)
        self.assertIn('textInput("entry-aliases"', page)
        self.assertIn('function addAliasChip(list,value)', page)
        self.assertIn('aliasInput.value.split(",")', page)
        self.assertIn('aliasInput.onkeydown=event=>{if(event.key==="Enter")', page)
        self.assertIn('aliasSave.textContent="Save aliases"', page)

    def test_reconciliation_table_is_removed_but_host_status_badges_remain(self):
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Latest reconciliation", page)
        self.assertNotIn('id="refresh"', page)
        self.assertIn("function entryStatusBadge(entry)", page)
        self.assertIn("function renderStatus(status)", page)


if __name__ == "__main__":
    unittest.main()
