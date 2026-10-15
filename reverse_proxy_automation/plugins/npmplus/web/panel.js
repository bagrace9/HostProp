/* NPMPlus's own dashboard panel.
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
          <button class="tab" type="button" role="tab" id="tab-npm" data-tab="npm" aria-controls="panel-npm" aria-selected="false" tabindex="-1">NPMPlus</button>
          <button class="tab" type="button" role="tab" id="tab-vlan" data-tab="vlan" aria-controls="panel-vlan" aria-selected="false" tabindex="-1">VLAN routing</button>
          <button class="tab" type="button" role="tab" id="tab-hosts" data-tab="hosts" aria-controls="panel-hosts" aria-selected="false" tabindex="-1">Host management</button>
</div>
<div data-host="panels" style="display:contents">
        <div class="tab-panel" id="panel-npm" role="tabpanel" aria-labelledby="tab-npm" hidden>
          <div class="panel-head"><div><h2>NPMPlus instances</h2><p>Every NPMPlus this app may write to, and the public names it creates there.</p></div><button class="button secondary" id="add-instance">＋ Add instance</button></div>
          <div class="note">Saved API secrets are write-only in this page: leaving a secret blank keeps the value this machine already has, and it may instead be an environment reference such as <code>env:NPM_LAB_SECRET</code>, which is read from the dashboard's environment and never stored here. Turn on <strong>self-signed</strong> for an endpoint whose <code>https://</code> certificate this machine does not trust: the TLS certificate check is then skipped for that instance only, and every other instance keeps verifying.</div>
          <div class="field-grid">
            <div class="field"><label for="public-domain">Public hostname domain</label><input id="public-domain"></div>
            <div class="field"><label for="max-deletes">Max deletions per run</label><input id="max-deletes" type="number" min="1" max="1000"><small>Required when Delete removed guests is enabled; a run that would delete more deletes nothing.</small></div>
          </div>
          <div class="instance-head"><span>Name</span><span>Label</span><span>URL / self-signed</span><span>Identity</span><span>Secret or env:VAR</span><span></span></div>
          <div id="instance-rows"></div>
          <div class="actions"><button class="button" id="save-instances">Save NPMPlus settings</button></div>
        </div>
        <div class="tab-panel" id="panel-vlan" role="tabpanel" aria-labelledby="tab-vlan" hidden>
          <div class="panel-head"><div><h2>VLAN routing</h2><p>Each NPMPlus instance has a card here; add every Proxmox VLAN tag it serves and the DNS zone it resolves.</p></div></div>
          <div class="vlan-head"><span>VLAN tag</span><span>Guest DNS zone</span><span></span></div>
          <div id="vlan-groups"></div>
          <div class="actions"><button class="button" id="save-vlans">Save VLAN routing</button></div>
        </div>
        <div class="tab-panel" id="panel-hosts" role="tabpanel" aria-labelledby="tab-hosts" hidden>
          <div class="panel-head"><div><h2>Host management</h2><p>Manage each NPMPlus entry independently. Adopt lets a matching guest update a host; release leaves it alone. Alias and port overrides take effect on the next preview or apply. Leave the port override blank to use PSL detection.</p></div><button class="button secondary" id="refresh-entries">Reload entries</button></div>
          <div class="legend">
            <div class="legend-item"><span class="tag managed">managed</span>created here, kept in step</div>
            <div class="legend-item"><span class="tag adopted">adopted</span>matched to a guest, updated but never deleted</div>
            <div class="legend-item"><span class="tag unmanaged">unmanaged</span>left alone by this app</div>
          </div>
          <div class="table-wrap"><table class="results"><thead><tr><th>Ownership</th><th>Entry</th><th>Upstream / port override</th><th>Aliases for this guest</th><th>Actions</th></tr></thead><tbody id="entries"><tr><td colspan="5" class="empty">Loading entries…</td></tr></tbody></table></div>
          <div class="panel-head"><div><h2>Latest PSL plan</h2><p>Preview shows the records this app would create or update, plus records it skipped. It does not create NPMPlus entries; use Apply to write changes.</p></div></div>
          <div class="table-wrap"><table class="results"><thead><tr><th>Result</th><th>Guest / hostname</th><th>NPMPlus / details</th></tr></thead><tbody id="plan-entries"><tr><td colspan="3" class="empty">Run this page's Preview to see PSL records and skip reasons.</td></tr></tbody></table></div>
        </div>
</div>
<div data-host="side" style="display:contents">
      <section class="panel"><h2>Deletions</h2><div class="switch-row"><div><strong>Delete removed guests</strong><span>Remove only hosts this app created when the guest leaves PSL. Off until explicitly enabled.</span></div><input class="switch" type="checkbox" id="delete-removed" aria-label="Delete hosts this app created when their guest leaves PSL"></div><p class="status-meta">Max deletions per run, on the NPMPlus tab, guards this: a run that would delete more deletes nothing.</p></section>
      <section class="panel"><h2>What gets generated</h2><p class="status-meta" id="domain-summary"></p><div id="generated"></div><p class="status-meta">Shown for the saved settings. Your split-DNS records must send each VLAN to the NPMPlus instance you mapped it to, and a matching certificate has to be installed on that instance.</p></section>
</div>
`;

  // This panel's own look. The shell keeps the shared design system - panels,
  // fields, buttons, tabs, switches, tables - and injects whatever styles a
  // destination registers, so no rule below has to be known by the shell.
  const STYLE = `
    .vlan-head,.vlan-row{display:grid;grid-template-columns:minmax(70px,.7fr) minmax(120px,1.3fr) 34px;gap:8px;align-items:center}

    .vlan-head{font-size:10px;color:var(--muted);text-transform:uppercase;padding-bottom:6px}

    .vlan-row{margin-bottom:8px}

    .tag.managed{background:var(--green-soft);color:var(--green)}

    .tag.adopted{background:#e7eef8;color:#33517d}

    .tag.unmanaged{background:#edf1ee;color:#53635b}

    .entry-actions{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}

    .entry-actions .button{padding:6px 9px;font-size:12px}

    .entry-names{font-weight:600}

    .entry-meta{color:var(--muted);font-size:11px}

    .instance-head,.instance-row{display:grid;grid-template-columns:minmax(90px,.9fr) minmax(90px,1fr) minmax(140px,1.5fr) minmax(90px,1fr) minmax(90px,1fr) 34px 82px;gap:8px;align-items:center}

    .instance-head{font-size:10px;color:var(--muted);text-transform:uppercase;padding-bottom:6px}

    .instance-row{margin-bottom:8px}

    .instance-row input{width:100%;min-width:0}

    .url-cell{display:flex;gap:6px;align-items:center}

    .instance-row .switch{width:38px;min-width:38px}

    .row-actions{display:flex;gap:6px;align-items:center;justify-content:flex-end}

    .row-actions .button{padding:9px 10px;font-size:12px}

    .note.generated-note{white-space:pre-line}

    .vlan-group{border:1px solid var(--line);background:#fbfdfc;padding:12px 12px 4px;margin-bottom:12px}

    .vlan-group.missing{border-color:#e7b3a8;background:var(--coral-soft)}

    .vlan-group-head{display:flex;align-items:center;gap:9px;margin-bottom:9px}

    .vlan-group-head .button{margin-left:auto}

    .vlan-group-title{font-weight:600}

    .vlan-group-meta{color:var(--muted);font-size:11px;text-transform:uppercase}

    .vlan-group.missing .vlan-group-title,.vlan-group.missing .vlan-group-meta{color:var(--coral)}

    .entry-badge{margin-left:6px}

    .alias-chip{display:inline-block;margin-left:5px;padding:1px 5px;font-size:10px;font-weight:400;letter-spacing:.02em;background:#eef2f0;border:1px solid var(--line);color:var(--muted)}

    .instance-head-row td{background:#f4f6f5;font-size:12px;color:var(--muted)}

    .instance-head-row strong{color:var(--ink);font-size:13px}

    .entry-row.disabled td{opacity:.55}

    .entry-row.conflict td{background:#fdf6e8}

    #entries.busy{opacity:.55}

    .vlan-group{
      margin-bottom:14px;
      padding:15px 15px 7px;
      border:1px solid #e1e9e4;
      border-radius:10px;
      background:#fafcfb;
    }

    .vlan-group.missing{border-color:#e7c1b8;background:#fff7f5}

    .vlan-group-head{flex-wrap:wrap;gap:8px 10px}

    .vlan-group-title{font-size:14px}

    .vlan-group-meta{font-size:10px;letter-spacing:.045em}

    .vlan-group-head .button{margin-left:auto}

    .vlan-row{gap:10px}

    .instance-row{gap:10px}

    .instance-head-row td{background:#f3f7f4}

    .entry-row.conflict td{background:#fff9eb}

    .entry-row.conflict:hover td{background:#fff6e3}

    .alias-editor{display:grid;gap:7px;min-width:240px}

    .alias-list{display:flex;flex-wrap:wrap;gap:5px}

    .alias-chip-editor{display:inline-flex;align-items:center;gap:5px;padding:3px 5px 3px 8px;border:1px solid #dce6e0;border-radius:999px;background:#f4f8f5;color:#40564a;font-size:11px;line-height:1.4}

    .alias-chip-editor button{display:grid;width:18px;height:18px;place-items:center;padding:0;border:0;border-radius:50%;background:transparent;color:#7d554c;font-size:14px;line-height:1}

    .alias-chip-editor button:hover{background:#fbece8;color:#9f392a}

    .alias-entry-controls{display:flex;align-items:center;gap:6px}

    .alias-editor input{min-width:0;min-height:36px;padding:7px 9px;font-size:12px}

    .alias-entry-controls .button,.alias-editor>.button{min-height:36px;padding:7px 10px;font-size:11px}

    #panel-hosts .results{min-width:850px}

    @media(max-width:520px){
      .vlan-head{display:none}
      .vlan-row{grid-template-columns:1fr}
      .vlan-row .icon-button{justify-self:end}
    }

    @media(max-width:900px){
      .instance-head{display:none}

      .instance-row{grid-template-columns:1fr 1fr 1fr 1fr 1fr 34px 1fr;border-top:1px solid var(--line);padding-top:8px}

      .instance-row .row-actions{grid-column:7;justify-self:end}
    }

    @media(max-width:560px){
      .instance-row{grid-template-columns:1fr}

      .instance-row .row-actions{grid-column:auto;justify-self:start}
      .vlan-group{padding:12px 11px 6px}
      .vlan-group-head .button{margin-left:0}
      #panel-hosts .results{min-width:780px}
    }
  `;

  let latestStatus=null,lastCheckKey=null,seenStatus=false,entriesBusy=false,entriesPending=false;

  // This destination's own action route. The core forwards the action name and
  // the payload opaquely, so it never learns what an NPMPlus entry is.
  function pluginAction(action,payload){return api(`/api/destinations/${KEY}`,Object.assign({action:action},payload||{}))}
    function instanceName(row){return row.querySelector(".instance-name").value.trim().toLowerCase()}
    function instanceNames(){return [...$("instance-rows").children].map(instanceName).filter(Boolean)}
    function instanceDisplay(name){for(const row of $("instance-rows").children){if(instanceName(row)===name)return row.querySelector(".instance-label").value.trim()||name}return name}

    function renameVlanGroup(row,previous,next){if(!previous||!next||previous===next)return;const rowList=[...$("instance-rows").children];if(rowList.some(other=>other!==row&&instanceName(other)===next))return;const host=$("vlan-groups");if([...host.children].some(group=>group.dataset.instance===next))return;for(const group of host.children){if(group.dataset.instance===previous)group.dataset.instance=next}}
    function vlanGroup(instance){const host=$("vlan-groups");for(const group of host.children){if(group.dataset.instance===instance)return group}const group=document.createElement("div");group.className="vlan-group";group.dataset.instance=instance;const head=document.createElement("div");head.className="vlan-group-head";const title=document.createElement("span");title.className="vlan-group-title";const meta=document.createElement("span");meta.className="vlan-group-meta";const add=document.createElement("button");add.className="button secondary vlan-add";add.type="button";add.textContent="\uFF0B Add VLAN";add.onclick=()=>vlanRow(group);const drop=document.createElement("button");drop.className="button secondary vlan-drop";drop.type="button";drop.textContent="Delete these VLAN rows";drop.onclick=()=>{group.remove();refreshVlanGroups()};head.append(title,meta,add,drop);const rows=document.createElement("div");rows.className="vlan-group-rows";group.append(head,rows);host.appendChild(group);return group}
    function paintVlanGroup(group,names){const instance=group.dataset.instance;const live=names.includes(instance);const count=group.querySelectorAll(".vlan-row").length;group.classList.toggle("missing",!live);group.querySelector(".vlan-group-title").textContent=live?instanceDisplay(instance):`${instance} (no instance row)`;group.querySelector(".vlan-group-meta").textContent=live?(count?`${count} VLAN tag${count===1?"":"s"}`:"No VLAN tag yet"):"Add the instance back or delete these rows";group.querySelector(".vlan-add").hidden=!live;group.querySelector(".vlan-drop").hidden=live}
    function paintVlanEmpty(names){const host=$("vlan-groups");const existing=host.querySelector(".empty-vlans");if(!names.length&&!host.querySelectorAll(".vlan-group").length){if(!existing){const note=document.createElement("p");note.className="empty empty-vlans";note.textContent="Add an NPMPlus instance on the NPMPlus tab; each instance gets its own card here.";host.appendChild(note)}}else if(existing)existing.remove()}
    function refreshVlanGroups(){const rowList=[...$("instance-rows").children];const names=rowList.map(instanceName).filter(Boolean);for(const row of rowList){const input=row.querySelector(".instance-name");const key=instanceName(row);if(input.dataset.instanceKey&&key&&key!==input.dataset.instanceKey)renameVlanGroup(row,input.dataset.instanceKey,key);input.dataset.instanceKey=key}for(const name of names)vlanGroup(name);for(const group of [...$("vlan-groups").children]){if(group.dataset.instance===undefined)continue;const live=names.includes(group.dataset.instance);if(!live&&!group.querySelectorAll(".vlan-row").length)group.remove();else paintVlanGroup(group,names)}paintVlanEmpty(names)}
    function instanceRow(name="",label="",url="",identity="",secretConfigured=false,secretEnv="",verifyTls=true,autoApply=true){const row=document.createElement("div");row.className="instance-row";const nameInput=textInput("instance-name","NPMPlus instance name");nameInput.value=name;nameInput.dataset.instanceKey=name.trim().toLowerCase();const labelInput=textInput("instance-label","NPMPlus instance label");labelInput.value=label;const urlInput=textInput("instance-url","NPMPlus instance URL");urlInput.value=url;const verifyInput=document.createElement("input");verifyInput.type="checkbox";verifyInput.className="switch instance-verify";verifyInput.checked=verifyTls===false;verifyInput.title="Allow a self-signed certificate: skip the TLS certificate check for this instance's URL only";verifyInput.setAttribute("aria-label","Allow a self-signed certificate for this instance URL");const urlCell=document.createElement("div");urlCell.className="url-cell";urlCell.append(urlInput,verifyInput);const identityInput=textInput("instance-identity","NPMPlus instance identity","");identityInput.value=identity;const secretInput=textInput("instance-secret","NPMPlus instance secret","");secretField(secretInput,secretEnv,secretConfigured);const autoApplyInput=document.createElement("input");autoApplyInput.type="checkbox";autoApplyInput.className="switch instance-auto-apply";autoApplyInput.checked=autoApply;autoApplyInput.title="Allow the watcher to apply changes automatically";autoApplyInput.setAttribute("aria-label","Auto-apply changes for this instance");autoApplyInput.onchange=async()=>{autoApplyInput.disabled=true;try{await api(`/api/destinations/${KEY}/toggle_auto_apply`,{value:autoApplyInput.checked});toast(`Auto-apply ${autoApplyInput.checked?"enabled":"disabled"} for ${name}`)}catch(error){toast(error.message,"error")}finally{autoApplyInput.disabled=false}};const actions=document.createElement("div");actions.className="row-actions";const test=document.createElement("button");test.className="button secondary";test.type="button";test.textContent="Test";test.onclick=async()=>{test.disabled=true;try{await saveConfig();toast((await api("/api/test",{target:name}))["ok"]?"Connected":"Failed");}catch(error){toast(error.message,"error")}finally{test.disabled=false}};const apply=document.createElement("button");apply.className="button";apply.type="button";apply.textContent="Apply";apply.onclick=async()=>{apply.disabled=true;try{const result=await api(`/api/destinations/${KEY}/apply`);toast(result.message)}catch(error){toast(error.message,"error")}finally{apply.disabled=false}};actions.append(test,apply);const rowCells=document.createElement("div");// The layout is handled by CSS grid on .instance-row; we just append children in order.
row.append(nameInput,labelInput,urlCell,identityInput,secretInput,autoApplyInput,actions);return row}i/test",{target:instanceName(row)})).message)}catch(error){toast(error.message,"error")}finally{test.disabled=false}};const remove=document.createElement("button");remove.className="icon-button";remove.type="button";remove.title="Remove instance";remove.setAttribute("aria-label","Remove instance");remove.textContent="×";remove.onclick=()=>{row.remove();refreshVlanGroups()};actions.append(test,remove);nameInput.oninput=refreshVlanGroups;labelInput.oninput=refreshVlanGroups;row.append(nameInput,labelInput,urlCell,identityInput,secretInput,actions);$("instance-rows").appendChild(row);return row}
    function getInstances(){const instances={};for(const row of $("instance-rows").children){const name=instanceName(row);if(!name)continue;if(name in instances)throw new Error(`Two NPMPlus instance rows both use the name '${name}'`);instances[name]={label:row.querySelector(".instance-label").value.trim(),url:row.querySelector(".instance-url").value.trim(),identity:row.querySelector(".instance-identity").value.trim(),secret:row.querySelector(".instance-secret").value,verify_tls:!row.querySelector(".instance-verify").checked}}return instances}
    function vlanRow(group,tag="",zone=""){const row=document.createElement("div");row.className="vlan-row";const tagInput=document.createElement("input");tagInput.className="tag-input";tagInput.inputMode="numeric";tagInput.setAttribute("aria-label","VLAN tag");tagInput.placeholder="VLAN tag";tagInput.value=tag;const zoneInput=document.createElement("input");zoneInput.className="zone zone-input";zoneInput.setAttribute("aria-label","Guest DNS zone");zoneInput.placeholder="Guest DNS zone";zoneInput.value=zone;const remove=document.createElement("button");remove.className="icon-button remove-vlan";remove.type="button";remove.title="Remove VLAN";remove.setAttribute("aria-label","Remove VLAN");remove.textContent="×";remove.onclick=()=>{row.remove();refreshVlanGroups()};row.append(tagInput,zoneInput,remove);group.querySelector(".vlan-group-rows").appendChild(row);refreshVlanGroups();return row}
    function getVlanMap(){const map={};const names=instanceNames();const problems=[];for(const group of $("vlan-groups").children){const instance=group.dataset.instance;if(instance===undefined)continue;for(const row of group.querySelectorAll(".vlan-row")){const tag=row.querySelector(".tag-input").value.trim();const zone=row.querySelector(".zone-input").value.trim();if(!tag&&!zone)continue;if(!tag){problems.push(`A VLAN row for ${instance} has a guest DNS zone but no VLAN tag; fill in the tag or remove the row.`);continue}if(!names.includes(instance)){problems.push(`VLAN ${tag} is still mapped to NPMPlus instance '${instance}', which has no instance row; add the instance back or delete that VLAN row.`);continue}if(!zone){problems.push(`VLAN ${tag} has no guest DNS zone; fill it in or remove the row.`);continue}if(tag in map){problems.push(`VLAN ${tag} is mapped twice; each VLAN tag may point at one NPMPlus instance.`);continue}map[tag]={instance,zone}}}if(problems.length)throw new Error(problems.join(" "));return map}

    function entryCellActions(entry){const actions=document.createElement("div");actions.className="entry-actions";const adopting=entry.ownership==="unmanaged";const toggle=document.createElement("button");toggle.className="button secondary";toggle.type="button";toggle.textContent=adopting?"Adopt":"Release";toggle.title=adopting?"Let a matching guest update this entry":"Stop managing this entry";toggle.onclick=()=>changeOwnership(entry,adopting?"adopt":"release",toggle);const drop=document.createElement("button");drop.className="button ghost";drop.type="button";drop.textContent="Delete";drop.title="Delete this entry from NPMPlus now";drop.onclick=()=>removeEntry(entry,drop);actions.append(toggle,drop);return actions}
    function addAliasChip(list,value){const name=value.trim();if(!name)return;const duplicate=[...list.querySelectorAll("[data-alias]")].some(chip=>chip.dataset.alias.toLowerCase()===name.toLowerCase());if(duplicate)return;const chip=document.createElement("span");chip.className="alias-chip-editor";chip.dataset.alias=name;const label=document.createElement("span");label.textContent=name;const remove=document.createElement("button");remove.type="button";remove.textContent="×";remove.setAttribute("aria-label",`Remove alias ${name}`);remove.onclick=()=>chip.remove();chip.append(label,remove);list.appendChild(chip)}
    async function saveEntryAliases(entry,list,button){const aliases=[...list.querySelectorAll("[data-alias]")].map(chip=>chip.dataset.alias);button.disabled=true;try{const result=await pluginAction("aliases",{instance:entry.instance,host_id:entry.id,aliases});toast(result.message);await loadEntries()}catch(error){toast(error.message,"error")}finally{button.disabled=false}}
    async function saveEntryPort(entry,input,button){const raw=input.value.trim();const port=raw===""?null:Number(raw);button.disabled=true;try{const result=await pluginAction("port",{instance:entry.instance,host_id:entry.id,port});toast(result.message);await loadEntries()}catch(error){toast(error.message,"error")}finally{button.disabled=false}}
    async function changeOwnership(entry,action,button){if(action==="release"&&entry.ownership==="managed"){const released=(entry.domain_names||[]).join(", ")||`host ${entry.id}`;if(!confirm(`Release ${released} on ${entry.label}?\n\nThis app stops maintaining it: it is no longer updated, and the next run reports a conflict for this name until you adopt the entry again, exclude the guest, or delete the entry.`))return}button.disabled=true;try{const result=await pluginAction("ownership",{instance:entry.instance,host_id:entry.id,action});toast(result.message);await loadEntries()}catch(error){toast(error.message,"error")}finally{button.disabled=false}}
    async function removeEntry(entry,button){const names=(entry.domain_names||[]).join(", ")||`host ${entry.id}`;if(!confirm(`Delete ${names} from ${entry.label}?\n\nThis removes the entry from NPMPlus now. An entry this app manages is created again by the next apply while its guest is still in PSL; release it first to keep it away.`))return;button.disabled=true;try{const result=await pluginAction("delete",{instance:entry.instance,host_id:entry.id});toast(result.message);await loadEntries()}catch(error){toast(error.message,"error")}finally{button.disabled=false}}
    function entryRow(entry){const tr=document.createElement("tr");tr.className="entry-row";const tag=document.createElement("span");tag.className=`tag ${entry.ownership}`;tag.textContent=entry.ownership;const first=document.createElement("td");first.appendChild(tag);const badge=entryStatusBadge(entry);if(badge)first.appendChild(badge);const second=document.createElement("td");const names=document.createElement("div");names.className="entry-names";paintEntryNames(names,entry);const meta=document.createElement("div");meta.className="entry-meta";meta.textContent=[`host ${entry.id}`,entry.enabled?"":"disabled"].filter(Boolean).join(" \\u00b7 ");second.append(names,meta);const third=document.createElement("td");const upstream=document.createElement("div");upstream.textContent=`${entry.forward_scheme}://${entry.forward_host}:${entry.forward_port}`;const portControls=document.createElement("div");portControls.className="alias-entry-controls";const portInput=textInput("entry-port",`Port override for ${entry.guest_hostname||entry.domain_names?.[0]||`host ${entry.id}`}`,`Detected: ${entry.forward_port}`);portInput.type="number";portInput.min="1";portInput.max="65535";portInput.step="1";portInput.inputMode="numeric";portInput.value=entry.port_override??"";const portSave=document.createElement("button");portSave.className="button secondary";portSave.type="button";portSave.textContent="Save port";portSave.title="Save a port override, or clear it to use PSL detection";portSave.onclick=()=>saveEntryPort(entry,portInput,portSave);portControls.append(portInput,portSave);third.append(upstream,portControls);const aliasCell=document.createElement("td");const aliasEditor=document.createElement("div");aliasEditor.className="alias-editor";const aliasList=document.createElement("div");aliasList.className="alias-list";for(const alias of entry.aliases||[])addAliasChip(aliasList,alias);const aliasControls=document.createElement("div");aliasControls.className="alias-entry-controls";const aliasInput=textInput("entry-aliases",`Add aliases for ${entry.guest_hostname||entry.domain_names?.[0]||`host ${entry.id}`}`,"Alias or hostname");const addAlias=document.createElement("button");addAlias.className="button secondary";addAlias.type="button";addAlias.textContent="Add alias";const addTypedAliases=()=>{const names=aliasInput.value.split(",").map(name=>name.trim()).filter(Boolean);for(const name of names)addAliasChip(aliasList,name);aliasInput.value=""};addAlias.onclick=addTypedAliases;aliasInput.onkeydown=event=>{if(event.key==="Enter"){event.preventDefault();addTypedAliases()}};aliasControls.append(aliasInput,addAlias);const aliasSave=document.createElement("button");aliasSave.className="button secondary";aliasSave.type="button";aliasSave.textContent="Save aliases";aliasSave.title="Save aliases for this guest";aliasSave.onclick=()=>saveEntryAliases(entry,aliasList,aliasSave);aliasEditor.append(aliasList,aliasControls,aliasSave);aliasCell.appendChild(aliasEditor);const fourth=document.createElement("td");fourth.appendChild(entryCellActions(entry));if(!entry.enabled)tr.classList.add("disabled");if(entryConflict(entry))tr.classList.add("conflict");tr.append(first,second,third,aliasCell,fourth);return tr}
    function renderEntries(payload){const body=$("entries");body.replaceChildren();const rows=[];for(const instance of payload.instances||[]){for(const host of instance.hosts||[])rows.push({...host,instance:instance.instance,label:instance.label});if(instance.error)rows.push({instance:instance.instance,label:instance.label,error:instance.error})}if(!rows.length){const tr=document.createElement("tr");tr.innerHTML='<td colspan="5" class="empty">No proxy hosts on the saved instances.</td>';body.appendChild(tr);return}let currentInstance=null;for(const row of rows){if(row.instance!==currentInstance){currentInstance=row.instance;body.appendChild(instanceHeadRow(row,rows))}if(row.error){const tr=document.createElement("tr");const first=document.createElement("td");const tag=document.createElement("span");tag.className="tag ERROR";tag.textContent="ERROR";first.appendChild(tag);const second=document.createElement("td");second.textContent=row.label;const third=document.createElement("td");third.colSpan=3;third.textContent=row.error;tr.append(first,second,third);body.appendChild(tr);continue}body.appendChild(entryRow(row))}}
    function renderPlan(status){const body=$("plan-entries");body.replaceChildren();if(!status.last_check){const tr=document.createElement("tr");tr.innerHTML='<td colspan="3" class="empty">Run this page\'s Preview to see PSL records and skip reasons.</td>';body.appendChild(tr);return}const rows=[...(status.planned||[]).map(item=>({status:item.status,name:item.domain||item.name||"",destination:item.destination||"",instance:item.instance||"",details:item.details||""})),...(status.skipped||[]).map(item=>({status:item.status||plannerSkipStatus(item.reason),name:item.name||"",destination:item.destination||"",instance:item.instance||"",details:item.reason||item.details||""}))];if(!rows.length){const tr=document.createElement("tr");tr.innerHTML='<td colspan="3" class="empty">The latest run returned no route or skip records.</td>';body.appendChild(tr);return}for(const item of rows){const tr=document.createElement("tr");const result=document.createElement("td");const tag=document.createElement("span");tag.className=`tag ${item.status}`;tag.textContent=item.status;result.appendChild(tag);const guest=document.createElement("td");guest.textContent=item.name;const detail=document.createElement("td");detail.textContent=[item.destination,item.instance,item.details].filter(Boolean).join(" · ");tr.append(result,guest,detail);body.appendChild(tr)}}
    async function loadEntries(){if(entriesBusy){entriesPending=true;return}entriesBusy=true;const button=$("refresh-entries");button.disabled=true;setEntriesBusy(true);try{renderEntries(await pluginAction("list_entries"))}catch(error){toast(error.message,"error")}finally{button.disabled=false;setEntriesBusy(false);entriesBusy=false;if(entriesPending){entriesPending=false;loadEntries()}}}
    function renderGenerated(config){const host=$("generated");host.replaceChildren();const domain=(config.public_domain||"").trim();const instances=config.instances||{};const mapped={};for(const [tag,mapping]of Object.entries(config.vlan_map||{})){if(!mapping||!mapping.instance)continue;(mapped[mapping.instance]=mapped[mapping.instance]||[]).push({tag,zone:mapping.zone})}const names=Object.keys(instances);if(!names.length){host.innerHTML='<p class="status-meta">Add an NPMPlus instance to see the generated routes.</p>';return}for(const name of names){const rows=mapped[name]||[];const note=document.createElement("p");note.className="note generated-note";const title=document.createElement("b");title.textContent=rows.length?`${instanceDisplay(name)} · VLAN ${rows.map(row=>row.tag).join(", ")}`:`${instanceDisplay(name)} · no VLAN mapped`;const detail=rows.length?(domain?rows.map(row=>`app.${domain} → app.${row.zone}:port`).join("\n"):"Set the public hostname domain to preview generated names."):"Map a VLAN tag to this instance in its VLAN routing card.";note.append(title,document.createElement("br"),document.createTextNode(detail));host.appendChild(note)}}

    function plannerSkipStatus(reason){return /^duplicate hostname|already served by guest|already used by guest|already claimed/i.test(reason||"")?"CONFLICT":"SKIP"}
    function summarizeEntries(hosts){const counts={managed:0,adopted:0,unmanaged:0};let total=0;for(const host of hosts){total+=1;if(counts[host.ownership]!==undefined)counts[host.ownership]+=1}const parts=[`${total} ${total===1?"entry":"entries"}`];for(const state of ["managed","adopted","unmanaged"]){if(counts[state])parts.push(`${counts[state]} ${state}`)}return parts.join(" \u00b7 ")}
    function instanceHeadRow(row,rows){const tr=document.createElement("tr");tr.className="instance-head-row";const cell=document.createElement("td");cell.colSpan=5;const strong=document.createElement("strong");strong.textContent=row.label||row.instance;const meta=document.createElement("span");meta.className="entry-meta";const hosts=rows.filter(item=>item.instance===row.instance&&!item.error);meta.textContent=`${row.instance} \u00b7 ${summarizeEntries(hosts)}`;cell.append(strong,document.createTextNode(" "),meta);tr.appendChild(cell);return tr}
    function paintEntryNames(node,entry){const list=(entry.domain_names||[]).filter(Boolean);if(!list.length){node.textContent=`host ${entry.id}`;return}const primary=document.createElement("span");primary.textContent=list[0];node.appendChild(primary);for(const name of list.slice(1)){const chip=document.createElement("span");chip.className="alias-chip";chip.textContent=name;node.appendChild(chip)}}
    // The last run's verdict for an entry, matched on its first name. A skipped item
    // may carry neither a status nor an instance, so it is normalised here exactly the
    // way the reconciliation table does it, and a planned row wins over a skipped copy.
    function entryStatusFor(entry){if(!latestStatus)return null;const domain=(entry.domain_names||[])[0];if(!domain)return null;const named=item=>(item.domain||item.name)===domain&&(!item.instance||item.instance===entry.instance);const matches=(latestStatus.planned||[]).filter(named);if(!matches.length)matches.push(...(latestStatus.skipped||[]).filter(named));if(!matches.length)return null;const item=matches[matches.length-1];return {status:item.status||plannerSkipStatus(item.reason),details:item.details||item.reason||""}}
    function entryConflict(entry){const item=entryStatusFor(entry);return Boolean(item&&["CONFLICT","SKIP","ERROR"].includes(item.status))}
    function entryStatusBadge(entry){const item=entryStatusFor(entry);if(!item||!item.status)return null;const badge=document.createElement("span");badge.className=`tag ${item.status} entry-badge`;badge.textContent=item.status;badge.title=item.details||item.reason||item.status;return badge}
    function setEntriesBusy(busy){const body=$("entries");body.classList.toggle("busy",busy);body.querySelectorAll("button").forEach(button=>{button.disabled=busy})}

    // The settings this destination owns, sent back with every save.
    function collect(){
      const config={};
      config.public_domain=$("public-domain").value;
      config.max_deletes_per_run=$("max-deletes").value;
      config.max_deletes_per_run=config.max_deletes_per_run===""?null:Number(config.max_deletes_per_run);
      config.delete_removed_hosts=$("delete-removed").checked;
      config.instances=getInstances();
      config.vlan_map=getVlanMap();
      return config;
    }

    // The fields this destination owns, restored from the saved settings.
    function load(config){
      $("public-domain").value=config.public_domain??"";
      $("max-deletes").value=config.max_deletes_per_run??"";
      $("delete-removed").checked=Boolean(config.delete_removed_hosts);
      const autoApplyFlags=config.destinations_auto_apply||{};
      $("instance-rows").replaceChildren();for(const [name,entry]of Object.entries(config.instances||{}))instanceRow(name,entry.label||"",entry.url||"",entry.identity||"",Boolean(entry.secret_configured),entry.secret_env||"",entry.verify_tls!==false,autoApplyFlags[name]!==false);if(!Object.keys(config.instances||{}).length)instanceRow();$("vlan-groups").replaceChildren();for(const name of Object.keys(config.instances||{}))vlanGroup(name);for(const [tag,mapping]of Object.entries(config.vlan_map||{})){const instance=mapping&&mapping.instance?mapping.instance:"(unassigned)";vlanRow(vlanGroup(instance),tag,mapping&&mapping.zone?mapping.zone:"")}refreshVlanGroups();const domain=(config.public_domain||"").trim();$("domain-summary").textContent=domain?`Names are created under ${domain}.`:"No public hostname domain is saved yet, so nothing is created.";renderGenerated(config);}

    // The core publishes each destination's own rows under its own key, so this
    // panel reads only its own slice: another plugin's run cannot fill this table,
    // and a run this plugin sat out leaves the last one that looked at it alone.
    function ownSlice(status){return (status&&status.destinations&&status.destinations[KEY])||null}
    // The shell renders the status it was given and then offers it here. The
    // first status only plants the check key: the shell already asked for the
    // entries when the page opened. A finished check reloads them, so a row
    // never shows a button that the last run has already made stale.
    function status(status){
      const mine=ownSlice(status);
      if(mine){latestStatus=mine;renderPlan(mine)}
      const checkKey=(mine&&mine.last_check)||null;
      const changed=checkKey!==lastCheckKey;
      lastCheckKey=checkKey;
      if(changed&&checkKey&&seenStatus)loadEntries();
      seenStatus=true;
    }

    // The shell calls this after a run, so the entries the run changed are
    // re-read from the instance.
    function refresh(){return loadEntries()}

    // The panel wires its own buttons: the shell mounted this markup, so these
    // elements exist by the time this runs.
    function start(){
      $("add-instance").onclick=()=>{instanceRow();refreshVlanGroups()};
      $("save-instances").onclick=()=>saveConfig().catch(error=>toast(error.message,"error"));
      $("save-vlans").onclick=()=>saveConfig().catch(error=>toast(error.message,"error"));
      $("refresh-entries").onclick=()=>loadEntries();
      $("delete-removed").onchange=()=>saveConfig().catch(error=>toast(error.message,"error"));
    }

  register({
    key: KEY,
    label: "NPMPlus",
    html: HTML,
    styles: STYLE,
    start: start,
    load: load,
    collect: collect,
    status: status,
    refresh: refresh
  });
})();
