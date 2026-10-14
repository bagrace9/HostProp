from pathlib import Path
import base64
import http.client
import json
import re
import socket
import threading
import tempfile
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from reverse_proxy_automation import webapp
from reverse_proxy_automation.plugins import default_registry
from reverse_proxy_automation.plugins.npmplus import PLUGIN as NPM_PLUGIN
from reverse_proxy_automation.service import ConfigStore, SyncService
from reverse_proxy_automation.webapp import DashboardServer, Handler, WEB_DIR


def shell_page():
    """The dashboard shell: the page that mounts every destination's panel.

    It keeps the shared look and the settings the core itself owns, and names no
    destination, so it is read on its own.
    """
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def panel_page():
    """NPMPlus's own panel, found from its package the way the server finds it."""
    directory = webapp.plugin_web_directory(NPM_PLUGIN)
    if directory is None:
        raise AssertionError("the NPMPlus package ships no web directory")
    return (directory / NPM_PLUGIN.panel).read_text(encoding="utf-8")


class DashboardShellTests(unittest.TestCase):
    """The shell mounts destinations; it knows nothing about any of them."""

    def test_the_shell_ships_in_the_web_directory(self):
        asset = WEB_DIR / "index.html"
        self.assertTrue(asset.is_file(), f"Dashboard asset is missing: {asset}")
        page = shell_page()
        # Guard against the dashboard being moved without updating WEB_DIR: the
        # shell's own controls, and the hosts a destination's panel mounts into.
        for element_id in ("pve-url", "pve-insecure", "excluded-hosts", "save-config", "panel-psl", "unsaved"):
            self.assertIn(f'id="{element_id}"', page)
        for host in ("tabs", "panels", "side"):
            self.assertIn(f'data-plugin-host="{host}"', page)

    def test_the_shell_names_no_destination_of_its_own(self):
        page = shell_page()
        # Every NPMPlus word lives in its panel now. A shell that still knew an
        # instance row, a VLAN card or a proxy host table would have to change
        # whenever a destination did, which is what this seam exists to stop.
        for stale in (
            "instance-rows", "add-instance", "save-instances", "instance-verify",
            "vlan-groups", "save-vlans", "vlan-", "public-domain", "max-deletes",
            "delete-removed", "delete_removed_hosts", "plan-entries", "panel-hosts",
            'id="entries"', 'id="generated"', 'id="aliases"', 'id="panel-npm"',
            'id="panel-vlan"', 'id="tab-npm"', "refresh-entries", "entry-aliases",
            "host_id", "domain_names", "forward_host", "ownership", "aliases",
            "public_domain", "max_deletes", "vlan_map", "/api/entries",
            "npm-lab-url", "npm-public-url", "npm_lab_secret", "npm_public_secret",
        ):
            self.assertNotIn(stale, page)
        # The seam is about names as well as ids: the shell's own copy - its
        # subtitle, its badge and its Apply confirmation - cannot name a
        # destination either, because a second destination makes each of them
        # wrong. Whatever a destination is called belongs in its panel.
        for named in ("NPMPlus", "npmplus"):
            self.assertNotIn(named, page)

    def test_the_shell_shows_the_destinations_it_mounted(self):
        page = shell_page()
        # Results are grouped by the plugin that produced them, so the page shows
        # which plugin planned or failed what, and an install with nothing
        # registered says so plainly.
        self.assertIn('id="destination-card"', page)
        self.assertIn('id="destination-list"', page)
        self.assertIn('id="destination-badge"', page)
        self.assertIn('id="results-by-destination"', page)
        self.assertIn("function renderDestinations(listed)", page)
        self.assertIn("function renderResults(status)", page)
        self.assertIn("function paintDestinationState(status)", page)
        for marker in (
            "chunk.dataset.destination=descriptor.key",
            "tab.dataset.destination=descriptor.key",
            "descriptor.tabs.push(tab.dataset.tab)",
            "renderDestinations(listed.destinations||[])",
            "paintDestinationBadge(listed.length)",
            'const list=$("destination-list")',
        ):
            self.assertIn(marker, page)
        self.assertIn("No destination is installed, so a run has nothing to write to.", page)
        self.assertIn('"No destination plugins"', page)

    def test_psl_is_the_primary_source_and_plugins_hang_off_it(self):
        page = shell_page()
        # The page is not one strip of equal tabs. The rail separates the required,
        # built-in source from the optional destination plugins, and the overview
        # draws the flow from the one to the others.
        self.assertIn('<nav class="rail"', page)
        self.assertLess(page.index('data-nav="psl"'), page.index('id="destination-list"'))
        self.assertIn('Source <small class="req">required</small>', page)
        self.assertIn("Destination plugins <small>optional</small>", page)
        self.assertIn('class="nav-item source-item"', page)
        self.assertLess(page.index('class="flow-source"'), page.index('class="flow-targets"'))
        self.assertIn('id="flow-plugins"', page)
        self.assertIn("function renderFlow(listed)", page)
        self.assertIn("function paintFlow(status)", page)
        # PSL is a page of its own, outside the container the plugin pages go into.
        self.assertIn('id="page-psl" data-page="psl"', page)
        self.assertLess(page.index('id="page-psl"'), page.index('id="destination-pages"'))
        self.assertIn('<span class="kicker">Source</span>', page)
        self.assertIn("Destination plugin", page)
        # Plugin chunks are staged out of sight, then moved onto that plugin's page.
        self.assertIn('id="plugin-staging" hidden', page)
        for host in ("tabs", "panels", "side"):
            self.assertIn(f'data-plugin-host="{host}"', page)

    def test_a_plugin_can_be_switched_off_from_its_own_page(self):
        page = shell_page()
        # Every plugin page carries an enable switch, so a plugin can be taken out of
        # runs without touching PSL or any other plugin; the page says so while off.
        self.assertIn("toggle.dataset.destinationSwitch=described.key", page)
        self.assertIn("function collectEnabled()", page)
        self.assertIn("config.destinations_enabled=collectEnabled()", page)
        self.assertIn("function paintEnabled(config)", page)
        self.assertIn("paintEnabled(config);paintPlaintextWarning(config)", page)
        self.assertIn("is switched off. Runs skip it and its settings are not checked", page)
        self.assertIn(".dest-page.off .dest-body", page)

    def test_a_plugin_can_be_left_for_manual_apply_from_its_own_page(self):
        page = shell_page()
        # The watcher's writes are gated twice: the master switch on the Run card and
        # each plugin's own entry. A plugin left for manual apply is left out of the
        # watcher's run, so its page has to say so.
        self.assertIn("autoToggle.dataset.destinationAutoApply=described.key", page)
        self.assertIn("function collectAutoApply()", page)
        self.assertIn("config.destinations_auto_apply=collectAutoApply()", page)
        self.assertIn("function paintAutoApply(config)", page)
        self.assertIn("paintPlaintextWarning(config);paintAutoApply(config)", page)
        # The switch sits in the plugin page's head, above the panels it brought, with
        # a line stating what the two switches together mean for that plugin.
        self.assertLess(
            page.index("autoToggle.dataset.destinationAutoApply=described.key"),
            page.index('const body=document.createElement("div");body.className="dest-body";'),
        )
        self.assertIn("controls.append(control,auto,autoHint,runBox);head.append(text,controls);", page)
        self.assertIn("autoToggle.onchange=()=>saveConfig()", page)
        self.assertIn("function paintAutoApplyHints()", page)
        self.assertIn('hint.dataset.state=written?"":"blocked"', page)
        self.assertIn("Left for manual apply: the watcher reads PSL and writes nothing here.", page)
        self.assertIn("The master Automatically apply switch is off, so the watcher only reads PSL and writes nothing.", page)
        self.assertIn("paintAutoApplyHints();saveConfig()", page)

    def test_the_shell_reads_only_elements_it_owns(self):
        page = shell_page()
        # An id the shell reads but does not declare is a nameless dependency on
        # markup it does not own, which is how the header badge came to be
        # written by a panel. The shell reaches its own controls and nothing else.
        declared = set(re.findall(r'id="([^"]+)"', page))
        read = set(re.findall(r'\$\("([^"]+)"\)', page))
        self.assertLessEqual(
            read,
            declared,
            f"the shell reads elements it does not own: {sorted(read - declared)}",
        )

    def test_the_shell_mounts_whatever_the_registry_hands_it(self):
        page = shell_page()
        # The shell asks the core which destinations exist, loads each one's own
        # panel file, injects the look it registers and then lets it start.
        self.assertIn('await api("/api/destinations")', page)
        self.assertIn("function registerDestination(descriptor)", page)
        self.assertIn("function mountPanel(descriptor)", page)
        self.assertIn("function mountScript(descriptor)", page)
        self.assertIn('pluginHost(chunk.dataset.host||"")', page)
        self.assertIn('if(descriptor.styles){const style=document.createElement("style");style.dataset.destination=descriptor.key;', page)
        self.assertIn("script.src=`/plugins/${encodeURIComponent(descriptor.key)}/${encodeURIComponent(descriptor.panel)}`;", page)

    def test_the_shell_hands_a_panel_the_api_it_may_use(self):
        page = shell_page()
        # A panel is mounted code, not a module: one published object is the whole
        # contract, and a panel reaches the shell through nothing else.
        self.assertIn(
            "window.RPA={$:$,api:api,toast:toast,textInput:textInput,"
            "secretField:secretField,saveConfig:saveConfig,paintDirty:paintDirty,"
            "showTab:showTab,register:registerDestination}",
            page,
        )

    def test_the_shell_reads_its_own_settings_off_the_form(self):
        page = shell_page()
        # A blank number field means "not set" rather than 0, and the TLS switch is
        # inverted once, exactly as a panel does for its own settings.
        self.assertIn('config.poll_interval_seconds===""?null:Number(config.poll_interval_seconds)', page)
        self.assertIn('$("pve-insecure").checked=config.pve_local_verify_tls===false', page)
        self.assertIn('config.pve_local_verify_tls=!$("pve-insecure").checked', page)
        # No preset is baked into a field the shell owns.
        for preset in ('value="example.net"', 'value="npmplus-one,npmplus-two"', 'value="300"', 'value="10"'):
            self.assertNotIn(preset, page)

    def test_the_dashboard_reports_failures_honestly(self):
        page = shell_page()
        # A toast carries a kind so a failed call cannot look like a success, and an
        # error stays on screen until it is dismissed or replaced.
        self.assertIn('node.classList.toggle("error",kind==="error")', page)
        self.assertIn('if(kind==="error")return', page)
        self.assertIn('toast(error.message,"error")', page)
        # Preview and Apply save the page first, so the service never runs a stale
        # settings file, and the toast repeats what the returned status said.
        self.assertIn("await saveConfig();toast(`${verb} ${label} started;", page)
        self.assertIn("const status=await api(path,{})", page)
        self.assertIn('else if(status.state==="error")toast(`${verb} ${label} failed: ', page)
        self.assertIn('if(status.state==="busy")toast(status.message,"error")', page)

    def test_api_errors_explain_file_urls_and_non_json_responses(self):
        page = shell_page()
        self.assertIn('if(location.protocol==="file:")', page)
        self.assertIn("opening index.html directly cannot reach the dashboard API", page)
        self.assertIn("const raw=await response.text()", page)
        self.assertIn("Dashboard API returned a non-JSON response (HTTP ${response.status})", page)

    def test_preview_and_apply_request_fresh_lxc_configuration(self):
        from reverse_proxy_automation import webapp

        source = Path(webapp.__file__).read_text(encoding="utf-8")
        # Preview and Apply are asked for at the one destination's own door, so a run
        # reads PSL fresh and touches the plugin whose page asked and no other. There
        # is no whole-install run left to ask for.
        self.assertIn('verb in ("preview", "apply")', source)
        self.assertIn('apply_changes=verb == "apply"', source)
        self.assertIn("refresh_lxc_config=True", source)
        self.assertNotIn('path == "/api/preview"', source)
        self.assertNotIn('path == "/api/apply"', source)
        service_source = Path(webapp.__file__).with_name("service.py").read_text(encoding="utf-8")
        # The watcher's own interval does the same two things a manual run does: one
        # forced read of PSL, then a per-destination decision about writing, which is
        # why the read and the apply are separate calls rather than one whole run.
        self.assertIn("self.refresh_source(refresh_lxc_config=True)", service_source)
        self.assertIn("self.auto_apply(config)", service_source)
        page = shell_page()
        self.assertIn("Refreshing PSL records…", page)
        self.assertIn('status.state==="busy"', page)

    def test_each_plugin_runs_its_own_preview_and_apply(self):
        page = shell_page()
        # A plugin's Preview and Apply sit on that plugin's own page and name the
        # plugin they belong to, so a run reviews and writes that destination alone,
        # and the shell no longer carries a run button that spoke for every one.
        self.assertNotIn('id="preview-now"', page)
        self.assertNotIn('id="apply-now"', page)
        self.assertNotIn("/api/preview", page)
        self.assertNotIn("/api/apply", page)
        self.assertIn("async function runDestination(key,kind,button)", page)
        self.assertIn("const path=`/api/destinations/${encodeURIComponent(key)}/${kind}`;", page)
        self.assertIn('preview.textContent=`Preview ${label}`', page)
        self.assertIn('apply.textContent=`Apply ${label}`', page)
        self.assertIn('runDestination(described.key,"preview",preview)', page)
        self.assertIn('runDestination(described.key,"apply",apply)', page)
        self.assertIn("runBox.append(preview,apply);", page)

    def test_the_shell_tracks_unsaved_settings_across_panels(self):
        page = shell_page()
        # The header says when the form holds settings this machine does not have,
        # including one a mounted panel is holding.
        self.assertIn('id="unsaved"', page)
        self.assertIn("function formState()", page)
        self.assertIn("panelSnapshots=snapshotPanels()", page)

    def test_every_plugin_gets_its_own_pages_and_tab_strip(self):
        page = shell_page()
        # PSL is a page of its own with no tab strip; each plugin's tabs live in a
        # strip on that plugin's own page, so no tab can be mistaken for another
        # owner's. Tabs and panels are looked up by id, so the page has to be in the
        # document before its first tab is selected.
        self.assertIn("function destinationPage(described,index)", page)
        self.assertIn('strip.setAttribute("role","tablist")', page)
        self.assertIn("function wireTabStrip(strip)", page)
        self.assertIn("function selectTab(tab)", page)
        self.assertIn("function showPage(name,focus)", page)
        self.assertIn("function tabButtons()", page)
        self.assertIn("function showTab(name,focus)", page)
        self.assertIn('[data-host][data-destination="${described.key}"]', page)
        self.assertLess(page.index("pages.append(built.page)"), page.index("selectTab(mountedTabs[0])"))
        # Arrow keys and clicks are handled on each plugin's own strip.
        self.assertIn('strip.addEventListener("click",event=>{', page)
        self.assertIn('strip.addEventListener("keydown",event=>{', page)
        self.assertIn('"ArrowRight"?1:event.key==="ArrowLeft"?-1:0', page)
        self.assertIn("other.tabIndex=selected?0:-1", page)
        # A closed page must not hide an unsaved edit: the tab holding it and the
        # rail entry of the page it is on are both marked from the same snapshot.
        self.assertIn("function paintTabs(dirty)", page)
        self.assertIn("function paintNav(dirty)", page)
        self.assertIn("panelSnapshots=snapshotPanels()", page)
        self.assertIn(".tab.dirty::after", page)
        self.assertIn("location.hash", page)

    def test_the_shell_renders_the_run_status(self):
        page = shell_page()
        # The status line is the shell's: every destination's rows travel through it,
        # and the reconciliation table it replaced is gone for good.
        self.assertIn("function renderStatus(status)", page)
        self.assertNotIn("Latest reconciliation", page)
        self.assertNotIn('id="results"', page)

    def test_the_page_warns_when_a_secret_is_stored_as_plain_text(self):
        page = shell_page()
        # The settings file can hold literal credentials, so the page has to name
        # them and point at the env: alternative instead of staying silent.
        self.assertIn('id="plaintext-warning"', page)
        self.assertIn("class=\"note plaintext-warning\"", page)
        self.assertIn("function paintPlaintextWarning(config)", page)
        self.assertIn("config.plaintext_secrets", page)
        self.assertIn("paintPlaintextWarning(config)", page)


class NPMPlusPanelTests(unittest.TestCase):
    """The panel NPMPlus ships: its own look, its own markup, its own actions."""

    def test_the_registry_lists_the_panel_the_plugin_names(self):
        listed = default_registry().get(NPM_PLUGIN.key)
        self.assertIsNotNone(listed, "the core does not list the NPMPlus destination")
        self.assertEqual(listed.panel, NPM_PLUGIN.panel)
        self.assertEqual(listed.label, NPM_PLUGIN.label)
        # A panel is a file next to the plugin and the server refuses to serve
        # anything else, so a typo here is a missing panel, not a stray file.
        self.assertEqual(NPM_PLUGIN.panel, "panel.js")
        directory = webapp.plugin_web_directory(NPM_PLUGIN)
        self.assertIsNotNone(directory, "the NPMPlus package ships no web directory")
        self.assertTrue((directory / NPM_PLUGIN.panel).is_file())

    def test_the_panel_registers_its_look_its_markup_and_its_callbacks(self):
        page = panel_page()
        # The panel owns its styling and its markup: the shell injects what is
        # registered here and never has to learn a destination's names.
        self.assertIn(
            "const { $, api, toast, textInput, secretField, saveConfig, register } = RPA;",
            page,
        )
        self.assertIn("register({", page)
        self.assertIn("key: KEY", page)
        self.assertIn("html: HTML", page)
        self.assertIn("styles: STYLE", page)
        for callback in ("start: start", "load: load", "collect: collect", "status: status", "refresh: refresh"):
            self.assertIn(callback, page)

    def test_the_panel_brings_its_own_tabs_and_panels(self):
        page = panel_page()
        # The shell mounts each chunk where its data-host says, so the panel decides
        # what the page shows without the shell naming any of it.
        for host in ("tabs", "panels"):
            self.assertIn(f'data-host="{host}"', page)
        for element_id in ("tab-npm", "tab-vlan", "tab-hosts", "panel-npm", "panel-vlan", "panel-hosts"):
            self.assertIn(f'id="{element_id}"', page)
        for name in ("npm", "vlan", "hosts"):
            self.assertIn(f'data-tab="{name}"', page)
            self.assertIn(f'aria-controls="panel-{name}"', page)
            self.assertIn(f'aria-labelledby="tab-{name}"', page)

    def test_the_panel_writes_only_into_its_own_markup(self):
        page = panel_page()
        # The panel is mounted into a page the shell owns, so it reaches its own
        # markup by id and nothing else. Its domain summary used to escape into
        # the shell's header badge, which left the shell unable to rename or
        # reuse the one element every destination could write into.
        declared = set(re.findall(r'id="([^"]+)"', page))
        read = set(re.findall(r'\$\("([^"]+)"\)', page))
        self.assertLessEqual(
            read,
            declared,
            f"the panel reads elements it does not own: {sorted(read - declared)}",
        )
        self.assertNotIn("system-badge", page)
        self.assertIn('id="domain-summary"', page)
        self.assertIn('$("domain-summary").textContent=', page)
        self.assertIn("Names are created under", page)

    def test_the_panel_declares_no_id_the_shell_already_uses(self):
        # One id, one owner: a panel that reuses an id the shell renders would
        # hand the shell a control it can no longer find.
        shared = set(re.findall(r'id="([^"]+)"', panel_page())) & set(
            re.findall(r'id="([^"]+)"', shell_page())
        )
        self.assertEqual(set(), shared, f"the panel and the shell both declare: {sorted(shared)}")

    def test_the_panel_owns_the_settings_it_collects(self):
        page = panel_page()
        self.assertIn("function collect()", page)
        for element_id in ("public-domain", "max-deletes", "instance-rows", "vlan-groups",
                           "add-instance", "save-instances", "save-vlans"):
            self.assertIn(f'id="{element_id}"', page)
        # A blank number field means "not set" rather than 0, exactly as the shell
        # reads its own fields.
        self.assertIn('config.max_deletes_per_run===""?null:Number(config.max_deletes_per_run)', page)
        self.assertIn('$("delete-removed").checked=Boolean(config.delete_removed_hosts)', page)
        # A saved secret reaches its row as a placeholder through the shell's own
        # helper, never as a value, and the TLS switch is inverted once, at the one
        # place that reads it.
        self.assertIn('textInput("instance-secret"', page)
        self.assertIn("secretField(secretInput,secretEnv,secretConfigured)", page)
        self.assertIn('verifyInput.checked=verifyTls===false', page)
        self.assertIn('verify_tls:!row.querySelector(".instance-verify").checked', page)

    def test_the_panel_owns_the_host_management_tables(self):
        page = panel_page()
        for element_id in ("entries", "plan-entries", "refresh-entries"):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn("function renderEntries(payload)", page)
        self.assertIn("renderPlan(status)", page)
        self.assertIn("status.planned||[]", page)
        self.assertIn("status.skipped||[]", page)
        # A skipped plan item is labelled by the plan table, and the entries table
        # says out loud that it only reports.
        self.assertIn("It does not create NPMPlus entries; use Apply to write changes.", page)

    def test_the_panel_labels_every_row_the_way_the_last_run_saw_it(self):
        page = panel_page()
        # Each instance gets a header row with its own counts, and a row carries the
        # ownership tag plus whatever the last check said about that hostname.
        self.assertIn("function summarizeEntries(hosts)", page)
        self.assertIn("instance-head-row", page)
        self.assertIn("paintEntryNames(", page)
        self.assertIn("function entryStatusBadge(entry)", page)
        self.assertIn('chip.className="alias-chip"', page)
        self.assertIn("<th>Actions</th>", page)
        # Delete is a quiet button now: the destructive action no longer shouts.
        self.assertIn('drop.className="button ghost"', page)
        # A pre-plan skip row is only {name, reason}: no status and no instance, so
        # the panel derives the status the same way the plan table does.
        self.assertIn("item.status||plannerSkipStatus(item.reason)", page)
        self.assertIn("!item.instance||item.instance===entry.instance", page)

    def test_the_panel_reloads_the_entries_when_the_check_key_changes(self):
        page = panel_page()
        # A finished check reloads the entries, so a row's buttons are never stale.
        self.assertIn("checkKey!==lastCheckKey", page)

    def test_the_panel_confirms_releasing_a_managed_entry(self):
        page = panel_page()
        # Releasing a managed entry cannot be undone by that one click, so the panel
        # says what happens next instead of asking twice.
        self.assertIn('action==="release"&&entry.ownership==="managed"', page)
        self.assertIn("reports a conflict for this name", page)

    def test_the_panel_edits_aliases_and_a_port_override_from_the_row(self):
        page = panel_page()
        # Aliases and a port override are edited where the entry is listed, and the
        # separate alias field the old page had is gone.
        self.assertNotIn('id="aliases"', page)
        self.assertNotIn('aliases:"aliases"', page)
        self.assertIn('textInput("entry-aliases"', page)
        self.assertIn("function addAliasChip(list,value)", page)
        self.assertIn('aliasInput.value.split(",")', page)
        self.assertIn('aliasInput.onkeydown=event=>{if(event.key==="Enter")', page)
        self.assertIn('aliasSave.textContent="Save aliases"', page)
        self.assertIn("function saveEntryAliases(entry,list,button)", page)
        self.assertIn("Aliases for this guest", page)
        self.assertIn("function saveEntryPort(entry,input,button)", page)
        self.assertIn("Port override for", page)
        self.assertIn("Leave the port override blank to use PSL detection", page)

    def test_the_panel_asks_its_own_destination_for_every_action(self):
        page = panel_page()
        # The routes the old page owned are gone: every action travels to this
        # destination's own door, named by an action rather than by a route.
        self.assertIn("function pluginAction(action,payload)", page)
        self.assertIn('api(`/api/destinations/${KEY}`', page)
        for action in ("list_entries", "ownership", "aliases", "port", "delete"):
            self.assertIn(f'pluginAction("{action}"', page)
        for route in ("/api/entries", "/api/entries/ownership", "/api/entries/delete"):
            self.assertNotIn(route, page)


class DashboardRequestSecurityTests(unittest.TestCase):
    def setUp(self):
        class ConfigStub:
            def save(self, _config):
                raise AssertionError("invalid body must be rejected before configuration is saved")

        self.server = DashboardServer(
            ("127.0.0.1", 0),
            Handler,
            ConfigStub(),
            type("SyncStub", (), {"get_status": lambda self: {}})(),
            "pässword",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def _request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.addCleanup(connection.close)
        connection.request(method, path, headers=headers or {})
        return connection.getresponse()

    def _auth_header(self):
        credentials = base64.b64encode("user:pässword".encode("utf-8")).decode("ascii")
        return f"Basic {credentials}"

    def test_basic_auth_accepts_a_non_ascii_configured_password(self):
        response = self._request(
            "GET",
            "/api/status",
            {"Host": "localhost", "Authorization": self._auth_header()},
        )
        self.assertEqual(response.status, 200)

    def test_dns_rebinding_host_is_rejected_before_authentication(self):
        response = self._request("GET", "/api/status", {"Host": "attacker.example"})
        self.assertEqual(response.status, 421)
        self.assertIn("Untrusted Host header", response.read().decode("utf-8"))

    def test_configured_reverse_proxy_host_is_accepted(self):
        self.server.allowed_hosts.add("dashboard.example.net")
        response = self._request(
            "GET",
            "/api/status",
            {"Host": "dashboard.example.net:8443", "Authorization": self._auth_header()},
        )
        self.assertEqual(response.status, 200)

    def test_any_ip_literal_host_is_accepted_without_an_allowlist_entry(self):
        response = self._request(
            "GET",
            "/api/status",
            {"Host": "192.0.2.42:8088", "Authorization": self._auth_header()},
        )
        self.assertEqual(response.status, 200)

    def test_negative_content_length_is_rejected(self):
        request = (
            "POST /api/config HTTP/1.1\r\n"
            f"Host: localhost\r\nOrigin: http://localhost\r\nAuthorization: {self._auth_header()}\r\n"
            "Content-Type: application/json\r\nContent-Length: -1\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
            connection.sendall(request)
            response = connection.makefile("rb").read().decode("utf-8")
        self.assertIn(" 400 ", response)
        self.assertIn("Invalid request body length", response)

    def test_request_handlers_use_a_finite_socket_timeout(self):
        self.assertEqual(Handler.timeout, 30)


class DestinationRouteTests(unittest.TestCase):
    """The three things a page reaches for, answered without knowing a destination.

    The destination list, a panel file and one action: the shell needs the first
    two, a panel needs the third, and the core forwards all three blind.
    """

    def setUp(self):
        registry = default_registry()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        # The watcher must stay quiet: these tests are about the routes.
        (root / "config.json").write_text(json.dumps({"watch_enabled": False}), encoding="utf-8")
        self.config_store = ConfigStore(root / "config.json", registry=registry)
        self.service = SyncService(self.config_store, root / "state.json", registry=registry)
        self.addCleanup(self.service.stop)
        self.server = DashboardServer(
            ("127.0.0.1", 0),
            Handler,
            self.config_store,
            self.service,
            "",  # no password: these tests are about the routes, not the guard
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def _request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"Host": "localhost"}
        if body is not None:
            headers["Origin"] = "http://localhost"
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        return connection.getresponse()

    def test_the_page_learns_which_destinations_exist(self):
        response = self._request("GET", "/api/destinations")
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        listed = {destination["key"]: destination for destination in payload["destinations"]}
        self.assertIn(NPM_PLUGIN.key, listed)
        self.assertEqual(listed[NPM_PLUGIN.key]["panel"], NPM_PLUGIN.panel)
        self.assertEqual(listed[NPM_PLUGIN.key]["label"], NPM_PLUGIN.label)

    def test_a_panel_is_served_straight_from_its_own_package(self):
        response = self._request("GET", f"/plugins/{NPM_PLUGIN.key}/{NPM_PLUGIN.panel}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read().decode("utf-8"), panel_page())

    def test_a_panel_asset_cannot_walk_out_of_its_package(self):
        for path in (
            f"/plugins/{NPM_PLUGIN.key}/../plugin.py",
            f"/plugins/{NPM_PLUGIN.key}/web/panel.js",
            f"/plugins/{NPM_PLUGIN.key}/missing.js",
            "/plugins/no-such-destination/panel.js",
        ):
            with self.subTest(path=path):
                response = self._request("GET", path)
                self.assertEqual(response.status, 404)

    def test_a_panel_action_is_forwarded_to_the_destination_by_name(self):
        response = self._request(
            "POST",
            f"/api/destinations/{NPM_PLUGIN.key}",
            json.dumps({"action": "no_such_action"}),
        )
        self.assertEqual(response.status, 400)
        # The core forwards the action without reading it, so an action the
        # destination does not list is refused by the destination, not here.
        self.assertIn("Unknown action", response.read().decode("utf-8"))

    def test_an_action_for_an_unknown_destination_is_refused_by_name(self):
        response = self._request(
            "POST",
            "/api/destinations/no-such-destination",
            json.dumps({"action": "list_entries"}),
        )
        self.assertEqual(response.status, 400)
        self.assertIn("does not offer panel actions", response.read().decode("utf-8"))

    def test_preview_and_apply_are_asked_for_at_the_destination_thats_asked(self):
        # A plugin's own page asks for its own Preview or Apply, so the route names
        # the destination and the verb, and the run is told to read PSL again so
        # what it shows and writes is fresh rather than what another page last saw.
        with mock.patch.object(self.service, "run_destination", autospec=True) as run_destination:
            run_destination.return_value = self.service.get_status()
            for verb, applying in (("preview", False), ("apply", True)):
                with self.subTest(verb=verb):
                    response = self._request(
                        "POST",
                        f"/api/destinations/{NPM_PLUGIN.key}/{verb}",
                        json.dumps({}),
                    )
                    self.assertEqual(response.status, 200)
                    self.assertIn("state", json.loads(response.read().decode("utf-8")))
                    run_destination.assert_called_once_with(
                        NPM_PLUGIN.key,
                        apply_changes=applying,
                        refresh_lxc_config=True,
                    )
                    run_destination.reset_mock()

    def test_there_is_no_longer_a_run_of_the_whole_install_to_ask_for(self):
        # Every destination runs from its own page now, so the routes that ran them
        # all at once are gone rather than left reachable and unmentioned.
        for path in ("/api/preview", "/api/apply"):
            with self.subTest(path=path):
                response = self._request("POST", path, json.dumps({}))
                self.assertEqual(response.status, 404)

    def test_an_unlisted_request_for_a_destination_is_refused_by_name(self):
        response = self._request(
            "POST",
            f"/api/destinations/{NPM_PLUGIN.key}/reload",
            json.dumps({}),
        )
        self.assertEqual(response.status, 404)
        self.assertIn("Unknown destination request 'reload'", response.read().decode("utf-8"))


class DashboardLoggingTests(unittest.TestCase):
    def test_main_configures_info_logging_by_default(self):
        sync_service = mock.Mock()
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(webapp.logging, "basicConfig") as basic_config,
            mock.patch.object(webapp, "ConfigStore") as config_store_type,
            mock.patch.object(webapp, "SyncService", return_value=sync_service),
            mock.patch.object(webapp, "DashboardServer", return_value=server),
        ):
            config_store_type.return_value.load.return_value = {}
            self.assertEqual(webapp.main(["--host", "127.0.0.1"]), 0)

        basic_config.assert_called_once_with(
            level="INFO",
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        sync_service.stop.assert_called_once()
        server.server_close.assert_called_once()

    def test_main_uses_the_configured_log_level(self):
        sync_service = mock.Mock()
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt

        with (
            mock.patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}),
            mock.patch.object(webapp.logging, "basicConfig") as basic_config,
            mock.patch.object(webapp, "ConfigStore") as config_store_type,
            mock.patch.object(webapp, "SyncService", return_value=sync_service),
            mock.patch.object(webapp, "DashboardServer", return_value=server),
        ):
            config_store_type.return_value.load.return_value = {}
            self.assertEqual(webapp.main(["--host", "127.0.0.1"]), 0)

        basic_config.assert_called_once_with(
            level="DEBUG",
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    def test_main_rejects_an_invalid_log_level_without_traceback(self):
        with (
            mock.patch.dict("os.environ", {"LOG_LEVEL": "bogus"}),
            mock.patch.object(webapp.logging, "basicConfig") as basic_config,
            self.assertRaises(SystemExit) as caught,
            redirect_stdout(StringIO()),
        ):
            webapp.main(["--host", "127.0.0.1"])

        self.assertEqual(caught.exception.code, 2)
        basic_config.assert_not_called()

    def test_main_starts_with_default_ipv6_host_allowlist(self):
        started = threading.Event()

        def stop_after_start(_server, poll_interval):
            self.assertEqual(poll_interval, 0.5)
            started.set()
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(DashboardServer, "serve_forever", stop_after_start), \
                redirect_stdout(StringIO()) as output:
            result = webapp.main([
                "--host", "127.0.0.1",
                "--port", "0",
                "--config", str(Path(directory) / "config.json"),
                "--state", str(Path(directory) / "state.json"),
            ])

        self.assertEqual(result, 0)
        self.assertTrue(started.is_set())
        self.assertIn("Dashboard listening at http://127.0.0.1:0/", output.getvalue())

    def test_main_warns_when_a_secret_is_stored_as_plain_text(self):
        def stop_after_start(_server, poll_interval):
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(DashboardServer, "serve_forever", stop_after_start), \
                redirect_stdout(StringIO()) as output:
            config_path = Path(directory) / "config.json"
            webapp.ConfigStore(config_path).save({
                "instances": {
                    "lab": {"url": "https://internal.example.net:81", "secret": "api-secret"},
                },
            })
            result = webapp.main([
                "--host", "127.0.0.1",
                "--port", "0",
                "--config", str(config_path),
                "--state", str(Path(directory) / "state.json"),
            ])

        self.assertEqual(result, 0)
        # A literal credential is never silent: the launcher says where it lives
        # and how to move it out of the settings file.
        self.assertIn("NPMPlus instance 'lab' secret is stored in", output.getvalue())
        self.assertIn("in the clear", output.getvalue())


if __name__ == "__main__":
    unittest.main()
