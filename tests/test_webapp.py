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
        # The seam is visible, not just real: the card names what the core
        # reported, the panel file and tabs each destination brought, and it
        # says plainly what an install with nothing registered can do.
        self.assertIn('id="destination-card"', page)
        self.assertIn('id="destination-list"', page)
        self.assertIn('id="destination-badge"', page)
        self.assertIn("function destinationRow(described)", page)
        self.assertIn("function renderDestinations(listed)", page)
        for marker in (
            "chunk.dataset.destination=descriptor.key",
            "tab.dataset.destination=descriptor.key",
            "descriptor.tabs.push(tab.dataset.tab)",
            "renderDestinations(listed.destinations||[])",
            "paintDestinationBadge(listed.length)",
            'const list=$("destination-list")',
        ):
            self.assertIn(marker, page)
        # A destination's first tab is one click away from the card, and a run
        # with nothing registered says so instead of listing nothing.
        self.assertIn("showTab(tabs[0],true)", page)
        self.assertIn("No destination is installed, so a run has nothing to write to.", page)

    def test_the_strip_says_which_tabs_a_plugin_brought(self):
        page = shell_page()
        # The strip holds the shell's own tab beside the tabs a plugin mounted, so
        # the legend under it names the owner of each entry. A reader can then see
        # that most of the page is an installed plugin rather than the app itself.
        self.assertIn('id="tab-sources"', page)
        self.assertIn("function renderTabSources(listed)", page)
        self.assertIn("renderTabSources(listed)", page)
        self.assertIn('legendItem("core-tag","built in"', page)
        self.assertIn('legendItem("plugin-tag","plugin"', page)
        # A mounted tab carries its owner in the strip itself, so the boundary stays
        # visible while one of a plugin's own tabs is open, and the tabs it brought
        # are listed as chips on its card.
        self.assertIn(".tab[data-destination]{box-shadow", page)
        self.assertIn("tab.title=`From the ${descriptor.label||descriptor.key} destination plugin`;", page)
        self.assertIn("descriptor.tabLabels.push(tab.textContent.trim()||tab.dataset.tab)", page)
        self.assertIn('chips.className="plugin-chip-row"', page)

    def test_a_plugins_own_section_is_captioned_with_its_plugin(self):
        page = shell_page()
        # A section a plugin mounted is drawn by the plugin, so the shell captions
        # it with the plugin that brought it and the app's own panel is the only
        # one left uncaptioned. The words come off the element, so the rule names no
        # destination and a second plugin needs no change here.
        self.assertIn('content:"Plugin: " attr(data-plugin-label)', page)
        self.assertIn("node.dataset.pluginLabel=descriptor.label||descriptor.key", page)
        self.assertIn('node.classList.add("plugin-owned")', page)
        self.assertIn('if(chunk.dataset.host!=="tabs"){', page)
        self.assertIn('const carrier=chunk.style.display==="contents"', page)
        self.assertIn("markPluginSurface(surface,descriptor)", page)
        # The shell's own panel says in words that it shipped with the app, so the
        # contrast with a caption is explicit rather than implied.
        self.assertIn('<span class="tag core-tag">built in</span>', page)
        self.assertIn("<h2>Destination plugins</h2>", page)
        self.assertIn('"No destination plugins"', page)

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
        self.assertIn("await saveConfig();toast(`${verb} started;", page)
        self.assertIn("const status=await api(path,{})", page)
        self.assertIn('else if(status.state==="error")toast(`${verb} failed: ', page)
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
        self.assertIn('path == "/api/preview"', source)
        self.assertIn('path == "/api/apply"', source)
        self.assertEqual(source.count("refresh_lxc_config=True"), 2)
        service_source = Path(webapp.__file__).with_name("service.py").read_text(encoding="utf-8")
        self.assertIn("refresh_lxc_config=True,", service_source)
        page = shell_page()
        self.assertIn("Refreshing PSL records…", page)
        self.assertIn('status.state==="busy"', page)

    def test_the_shell_tracks_unsaved_settings_across_panels(self):
        page = shell_page()
        # The header says when the form holds settings this machine does not have,
        # including one a mounted panel is holding.
        self.assertIn('id="unsaved"', page)
        self.assertIn("function formState()", page)
        self.assertIn("panelSnapshots=snapshotPanels()", page)

    def test_the_shell_owns_the_tab_strip_and_its_own_panel(self):
        page = shell_page()
        # The shell opens with its own PSL tab; every destination adds one more, so
        # the strip is drawn from what was mounted rather than from a fixed count.
        self.assertIn('role="tablist"', page)
        self.assertIn('id="panel-psl" role="tabpanel" aria-labelledby="tab-psl">', page)
        self.assertIn('aria-controls="panel-psl"', page)
        self.assertLess(page.index('id="panel-psl"'), page.index('id="pve-url"'))
        self.assertIn("function tabButtons()", page)
        self.assertIn("function showTab(name,focus)", page)
        # Click and arrow keys are handled on the strip itself, so a tab a
        # destination mounted works without the shell ever naming it.
        self.assertIn('tabList.addEventListener("click",event=>{', page)
        self.assertIn('tabList.addEventListener("keydown",event=>{', page)
        self.assertIn('"ArrowRight"?1:event.key==="ArrowLeft"?-1:0', page)
        self.assertIn("tab.tabIndex=selected?0:-1", page)
        # A closed panel must not hide an unsaved edit: the tab holding it is marked,
        # and a mounted panel's tab is marked from the same snapshot the header uses.
        self.assertIn("function paintTabs(dirty)", page)
        self.assertIn("panelSnapshots=snapshotPanels()", page)
        self.assertIn(".tab.dirty::after", page)

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
