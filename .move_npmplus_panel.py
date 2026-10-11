"""One-off: move the NPMPlus panel out of the dashboard shell into its plugin.

The shell's ``web/index.html`` still held NPMPlus's markup and behaviour: its
three tabs, its settings panels, its side card and every function that talks
about an instance or a proxy host. This script lifts them verbatim into
``plugins/npmplus/web/panel.js``, leaves three mounting hosts behind and
replaces the NPMPlus-specific wiring in the shell with a registry-driven mount.
It is deleted once it has run.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "reverse_proxy_automation" / "web" / "index.html"
PANEL = ROOT / "reverse_proxy_automation" / "plugins" / "npmplus" / "web" / "panel.js"

lines = INDEX.read_text(encoding="utf-8").split("\n")


def cut(start, end, head, tail):
    """Remove a 1-based inclusive line range, checking it is the range meant."""
    chunk = lines[start - 1:end]
    text = "\n".join(chunk)
    assert head in text, f"lines {start}-{end} do not hold {head!r}: {text[:70]!r}"
    assert tail in text, f"lines {start}-{end} do not reach {tail!r}: {text[-70:]!r}"
    del lines[start - 1:end]
    return text


# --- the destination's behaviour, cut from the bottom up --------------------
entries_helpers = cut(517, 527, "function plannerSkipStatus(", "function setEntriesBusy(")
entry_table = cut(482, 492, "function entryCellActions(", "function renderGenerated(")
settings_helpers = cut(472, 480, "function renameVlanGroup(", "function getVlanMap(")
instance_helpers = cut(465, 467, "function instanceName(", "function instanceDisplay(")
side_card = cut(444, 444, 'id="generated"', 'id="generated"')
panels = cut(403, 430, 'id="panel-npm"', 'id="plan-entries"')
tab_buttons = cut(385, 387, 'id="tab-npm"', 'id="tab-hosts"')

shell = "\n".join(lines)


def patch(old, new, count=1):
    """Replace one known piece of the shell, so a surprise edit stops the run."""
    global shell
    found = shell.count(old)
    assert found == count, f"expected {count} of {old[:70]!r}, found {found}"
    shell = shell.replace(old, new)


def rewire(chunk, old, new, count=1):
    """Point one panel call at this destination's own action route."""
    found = chunk.count(old)
    assert found == count, f"expected {count} of {old[:70]!r}, found {found}"
    return chunk.replace(old, new)


# --- the shell keeps the source settings and gains three mounting hosts ----
patch(
    'aria-selected="true">PSL</button>\n        </div>',
    'aria-selected="true">PSL</button>\n'
    '          <div data-plugin-host="tabs" style="display:contents"></div>\n'
    '        </div>',
)
patch(
    'data-test="pve">Test PSL</button></div>\n        </div>',
    'data-test="pve">Test PSL</button></div>\n        </div>\n'
    '        <div data-plugin-host="panels" style="display:contents"></div>',
)
patch(
    '</section>\n    </aside>',
    '</section>\n      <div data-plugin-host="side" style="display:contents"></div>\n    </aside>',
)
# The tab strip holds as many tabs as the mounted destinations bring.
patch(
    "      grid-template-columns:repeat(4,minmax(0,1fr));",
    "      grid-template-columns:repeat(var(--tab-count,4),minmax(0,1fr));",
)

# --- the shell's settings vocabulary is the source and the run switches -----
patch(
    'const fields = {pve_local_url:"pve-url",pve_local_username:"pve-user",'
    'public_domain:"public-domain",poll_interval_seconds:"poll-interval",'
    'excluded_guest_hostnames:"excluded-hosts",max_deletes_per_run:"max-deletes"};',
    'const fields = {pve_local_url:"pve-url",pve_local_username:"pve-user",'
    'poll_interval_seconds:"poll-interval",excluded_guest_hostnames:"excluded-hosts"};',
)
patch(
    "let toastTimer;let latestStatus=null,lastCheckKey=null,entriesBusy=false,"
    "entriesPending=false,formSnapshot=null,panelSnapshots=null;",
    "let toastTimer;let formSnapshot=null,panelSnapshots=null;",
)
patch(
    's \u00b7 NPMPlus ${timings.npmplus_seconds.toFixed(2)}s`:"";',
    's`:"";',
)

# --- the shell reports the status it is given and hands it on ---------------
patch(
    ";latestStatus=status;renderPlan(status);const checkKey=status.last_check||null;"
    "if(checkKey&&checkKey!==lastCheckKey){lastCheckKey=checkKey;loadEntries()}}",
    ";for(const destination of destinations){if(destination.status)destination.status(status)}}",
)

# --- the shell restores and collects only what it owns ---------------------
patch('$("delete-removed").checked=Boolean(config.delete_removed_hosts);', "")
assert shell.count('$("instance-rows").replaceChildren();') == 1
start = shell.index('$("instance-rows").replaceChildren();')
end = shell.index("renderGenerated(config);") + len("renderGenerated(config);")
moved_load = shell[start:end]
assert "paintPlaintextWarning(config);takeSnapshot()" in shell[end:end + 80]
shell = (
    shell[:start]
    + "for(const destination of destinations){if(destination.load)destination.load(config)}"
    + shell[end:]
)
patch(
    "config.instances=getInstances();config.vlan_map=getVlanMap();",
    "for(const destination of destinations)Object.assign(config,"
    "destination.collect?destination.collect():{});",
)
patch('config.delete_removed_hosts=$("delete-removed").checked;', "")

# --- the shell drives a run and the tabs of whatever it mounted ------------
patch(
    "const status=await api(path,{});renderStatus(status);",
    "const status=await api(path,{});renderStatus(status);await refreshDestinations();",
)
# The tab strip is wired on the container, because a destination's tabs are
# mounted after this script has run.
patch(
    '    tabButtons().forEach(tab=>{tab.onclick=()=>showTab(tab.dataset.tab);'
    'tab.onkeydown=event=>{const tabs=tabButtons();'
    'const step=event.key==="ArrowRight"?1:event.key==="ArrowLeft"?-1:0;'
    'const target=step?tabs[(tabs.indexOf(tab)+step+tabs.length)%tabs.length]:'
    'event.key==="Home"?tabs[0]:event.key==="End"?tabs[tabs.length-1]:null;'
    'if(!target)return;event.preventDefault();showTab(target.dataset.tab,true)}});\n',
    '    const tabList=document.querySelector(".tabs");\n'
    '    tabList.addEventListener("click",event=>{const tab=event.target.closest(".tab");'
    'if(tab)showTab(tab.dataset.tab)});\n'
    '    tabList.addEventListener("keydown",event=>{if(!event.target.closest(".tab"))return;'
    'const tabs=tabButtons();const step=event.key==="ArrowRight"?1:event.key==="ArrowLeft"?-1:0;'
    'const target=step?tabs[(tabs.indexOf(event.target)+step+tabs.length)%tabs.length]:'
    'event.key==="Home"?tabs[0]:event.key==="End"?tabs[tabs.length-1]:null;'
    'if(!target)return;event.preventDefault();showTab(target.dataset.tab,true)});\n',
)
# Every "test this endpoint" button is wired once the panels are mounted, so a
# destination's own test button works too.
patch(
    '    document.querySelectorAll("[data-test]").forEach(button=>button.onclick='
    'async()=>{button.disabled=true;try{await saveConfig();'
    'const result=await api("/api/test",{target:button.dataset.test});toast(result.message)}'
    'catch(error){toast(error.message,"error")}finally{button.disabled=false}});\n',
    "",
)

# --- the shell's own button wiring loses the destination's buttons ---------
patch('$("add-instance").onclick=()=>{instanceRow();refreshVlanGroups()};', "")
patch(
    '$("save-instances").onclick=()=>saveConfig().catch(error=>toast(error.message,"error"));',
    "",
)
patch(
    '$("save-vlans").onclick=()=>saveConfig().catch(error=>toast(error.message,"error"));',
    "",
)
patch('$("refresh-entries").onclick=()=>loadEntries();', "")
patch(
    '$("delete-removed").onchange=()=>saveConfig().catch(error=>toast(error.message,"error"));',
    "",
)
patch(
    'async function initialize(){await loadConfig();const status=await api("/api/status");'
    "lastCheckKey=status.last_check||null;renderStatus(status);await loadEntries()}",
    'async function initialize(){await mountDestinations();await loadConfig();'
    'const status=await api("/api/status");renderStatus(status);await refreshDestinations()}',
)

# --- the shell mounts whatever the registry hands it -----------------------
DESTINATIONS = '''    // A destination is mounted, not written into this page: the shell asks the
    // core which destinations exist, loads the panel file each one names,
    // mounts the markup it registered and then asks it for the settings the
    // page has to send back and the fields it has to restore. Nothing below
    // knows that NPMPlus has instances, VLAN routes or proxy host entries.
    const destinations=[];
    const registered={};
    function pluginHost(name){return document.querySelector(`[data-plugin-host="${name}"]`)}
    function registerDestination(descriptor){
      if(!descriptor||!descriptor.key)throw new Error("A destination panel must register a key");
      registered[descriptor.key]=descriptor;
      return descriptor
    }
    function mountPanel(descriptor){
      const template=document.createElement("template");
      template.innerHTML=String(descriptor.html||"").trim();
      for(const chunk of [...template.content.children]){
        const host=pluginHost(chunk.dataset.host||"");
        if(!host){console.warn(`Destination ${descriptor.key} declares an unknown panel section: ${chunk.dataset.host}`);continue}
        host.appendChild(chunk)
      }
      if(descriptor.styles){const style=document.createElement("style");style.dataset.destination=descriptor.key;style.textContent=descriptor.styles;document.head.appendChild(style)}
      destinations.push(descriptor);
      if(descriptor.start)descriptor.start()
    }
    function mountScript(descriptor){
      return new Promise((resolve,reject)=>{
        const script=document.createElement("script");
        script.src=`/plugins/${encodeURIComponent(descriptor.key)}/${encodeURIComponent(descriptor.panel)}`;
        script.onload=()=>resolve();
        script.onerror=()=>reject(new Error(`${descriptor.label||descriptor.key} panel could not be loaded from ${script.src}`));
        document.head.appendChild(script)
      })
    }
    function wireTestButtons(){
      document.querySelectorAll("[data-test]").forEach(button=>button.onclick=async()=>{button.disabled=true;try{await saveConfig();const result=await api("/api/test",{target:button.dataset.test});toast(result.message)}catch(error){toast(error.message,"error")}finally{button.disabled=false}})
    }
    async function mountDestinations(){
      const listed=await api("/api/destinations");
      for(const described of listed.destinations||[]){
        if(!described.panel)continue;
        await mountScript(described);
        const mounted=registered[described.key];
        if(!mounted){console.warn(`Destination ${described.key} registered no panel for ${described.panel}`);continue}
        mounted.descriptor=described;
        if(!mounted.label)mounted.label=described.label;
        mountPanel(mounted)
      }
      wireTestButtons();
      document.documentElement.style.setProperty("--tab-count",String(tabButtons().length));
      const first=tabButtons()[0];
      if(first)showTab(first.dataset.tab,false)
    }
    async function refreshDestinations(){for(const destination of destinations){if(destination.refresh)await destination.refresh()}}
    window.RPA={$:$,api:api,toast:toast,textInput:textInput,secretField:secretField,saveConfig:saveConfig,paintDirty:paintDirty,showTab:showTab,register:registerDestination};
'''
patch(
    '    document.addEventListener("input",paintDirty);document.addEventListener("change",paintDirty);',
    DESTINATIONS
    + '    document.addEventListener("input",paintDirty);document.addEventListener("change",paintDirty);',
)

# --- the panel the shell mounts -------------------------------------------
for chunk, name in ((tab_buttons, "tabs"), (panels, "panels"), (side_card, "side")):
    assert "`" not in chunk and "${" not in chunk, f"{name} markup is not template-literal safe"

entry_table = rewire(entry_table, 'await api("/api/entries/aliases",', 'await pluginAction("aliases",')
entry_table = rewire(entry_table, 'await api("/api/entries/port",', 'await pluginAction("port",')
entry_table = rewire(entry_table, 'await api("/api/entries/ownership",', 'await pluginAction("ownership",')
entry_table = rewire(entry_table, 'await api("/api/entries/delete",', 'await pluginAction("delete",')
entry_table = rewire(entry_table, 'renderEntries(await api("/api/entries"))', 'renderEntries(await pluginAction("list_entries"))')
for action in ("list_entries", "aliases", "port", "ownership", "delete"):
    assert f'pluginAction("{action}"' in entry_table, f"{action} was not rewired"
assert "/api/entries" not in entry_table

collect_fn = '''    // The settings this destination owns, sent back with every save.
    function collect(){
      const config={};
      config.public_domain=$("public-domain").value;
      config.max_deletes_per_run=$("max-deletes").value;
      config.max_deletes_per_run=config.max_deletes_per_run===""?null:Number(config.max_deletes_per_run);
      config.delete_removed_hosts=$("delete-removed").checked;
      config.instances=getInstances();
      config.vlan_map=getVlanMap();
      return config;
    }'''

load_fn = '''    // The fields this destination owns, restored from the saved settings.
    function load(config){
      $("public-domain").value=config.public_domain??"";
      $("max-deletes").value=config.max_deletes_per_run??"";
      $("delete-removed").checked=Boolean(config.delete_removed_hosts);
      ''' + moved_load + "}"

status_fn = '''    // The shell renders the status it was given and then offers it here. The
    // first status only plants the check key: the shell already asked for the
    // entries when the page opened. A finished check reloads them, so a row
    // never shows a button that the last run has already made stale.
    function status(status){
      latestStatus=status;
      renderPlan(status);
      const checkKey=status.last_check||null;
      const changed=checkKey!==lastCheckKey;
      lastCheckKey=checkKey;
      if(changed&&checkKey&&seenStatus)loadEntries();
      seenStatus=true;
    }'''

refresh_fn = '''    // The shell calls this after a run, so the entries the run changed are
    // re-read from the instance.
    function refresh(){return loadEntries()}'''

start_fn = '''    // The panel wires its own buttons: the shell mounted this markup, so these
    // elements exist by the time this runs.
    function start(){
      $("add-instance").onclick=()=>{instanceRow();refreshVlanGroups()};
      $("save-instances").onclick=()=>saveConfig().catch(error=>toast(error.message,"error"));
      $("save-vlans").onclick=()=>saveConfig().catch(error=>toast(error.message,"error"));
      $("refresh-entries").onclick=()=>loadEntries();
      $("delete-removed").onchange=()=>saveConfig().catch(error=>toast(error.message,"error"));
    }'''

header = '''/* NPMPlus's own dashboard panel.
 *
 * The core serves this file without reading it and the shell mounts the markup
 * it registers, so nothing in the shell knows that NPMPlus has instances, VLAN
 * routes or proxy host entries. The panel reaches its own actions through
 * /api/destinations/npmplus and is handed the settings to restore and collect.
 */
(function () {
  "use strict";
  const RPA = window.RPA;
  if (!RPA) throw new Error("The NPMPlus panel was loaded before the dashboard shell was ready");
  const { $, api, toast, textInput, secretField, saveConfig, register } = RPA;
  const KEY = "npmplus";

  // The shell mounts each chunk of this markup where its data-host says, so
  // the panel decides what the page shows without the shell naming it.
  const HTML = `
<div data-host="tabs" style="display:contents">
'''

markup = tab_buttons + '''
</div>
<div data-host="panels" style="display:contents">
''' + panels + '''
</div>
<div data-host="side" style="display:contents">
''' + side_card + '''
</div>
`;

  let latestStatus=null,lastCheckKey=null,seenStatus=false,entriesBusy=false,entriesPending=false;

  // This destination's own action route. The core forwards the action name and
  // the payload opaquely, so it never learns what an NPMPlus entry is.
  function pluginAction(action,payload){return api(`/api/destinations/${KEY}`,Object.assign({action:action},payload||{}))}
'''

body = "\n\n".join([instance_helpers, settings_helpers, entry_table, entries_helpers,
                    collect_fn, load_fn, status_fn, refresh_fn, start_fn])

footer = '''
  register({
    key: KEY,
    label: "NPMPlus",
    html: HTML,
    start: start,
    load: load,
    collect: collect,
    status: status,
    refresh: refresh
  });
})();
'''

panel_text = header + markup + body + footer
PANEL.parent.mkdir(parents=True, exist_ok=True)
PANEL.write_text(panel_text, encoding="utf-8")
INDEX.write_text(shell, encoding="utf-8")

print(f"wrote {PANEL.name}: {len(panel_text.splitlines())} lines")
print(f"rewrote {INDEX.name}: {len(shell.splitlines())} lines")
for token in ("instanceRow", "getInstances", "getVlanMap", "vlanGroup", "loadEntries",
              "renderPlan", "renderGenerated", "entryRow", "renderEntries", "entriesBusy",
              "latestStatus", "NPMPlus", "public_domain", "max_deletes", "delete_removed"):
    print(f"  shell still mentions {token!r}: {shell.count(token)}")




