"""Tela local de projetos; todas as operações passam pelos jobs do daemon."""
import html
import re
import setup_ui
import project_maps_ui
import project_insights_ui


def render_projects_page():
    return (_PAGE.replace("__TOKEN__", html.escape(setup_ui.SETUP_TOKEN, quote=True))
            .replace("__MAP_STYLE__", project_maps_ui.STYLE)
            .replace("__MAP_HTML__", project_maps_ui.HTML)
            .replace("__MAP_SCRIPT__", project_maps_ui.SCRIPT)
            .replace("__INSIGHTS_STYLE__", project_insights_ui.STYLE)
            .replace("__INSIGHTS_HTML__", project_insights_ui.HTML)
            .replace("__INSIGHTS_SCRIPT__", project_insights_ui.SCRIPT))


def render_map_window():
    """Janela independente compartilha o componente e os controles da página principal."""
    style = re.search(r'<style>(.*?)</style>', _PAGE, re.S).group(1)
    values = {
        '__TOKEN__': html.escape(setup_ui.SETUP_TOKEN, quote=True),
        '__STYLE__': style.replace('__MAP_STYLE__', project_maps_ui.STYLE).replace('__INSIGHTS_STYLE__', project_insights_ui.STYLE),
        '__MAP_HTML__': project_maps_ui.HTML,
        '__MAP_SCRIPT__': project_maps_ui.SCRIPT,
    }
    return re.sub('|'.join(re.escape(key) for key in values), lambda m:values[m[0]], _MAP_WINDOW)


_MAP_WINDOW = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="setup-token" content="__TOKEN__"><title>Project maps · Smart Tool</title><style>
__STYLE__
body.map-window{padding:0}.map-window header,.map-window main{max-width:none}.map-window header{padding:22px 28px 14px}.map-window main{padding:0 20px 30px}.map-window h1{font-size:22px}.map-window #window-root{margin:6px 0 0;font-size:12px;overflow-wrap:anywhere}.map-window .maps{margin-top:0}.map-window .maps>summary{display:none}.map-window .maps-workspace{grid-template-columns:minmax(0,1fr) 285px}.map-window #maps-svg{height:calc(100vh - 360px);min-height:430px}.map-window .maps-tools #maps-new-window{display:none}.map-window #maps-color-legend{max-height:90px}.map-window #window-message:empty{display:none}.map-window #window-message{margin-bottom:14px;overflow-wrap:anywhere}
@media(max-width:850px){.map-window .maps-workspace{grid-template-columns:1fr}.map-window header{padding-inline:18px}.map-window main{padding-inline:12px}.map-window #maps-svg{height:450px;min-height:0}}
</style></head><body class="map-window"><header><div><h1 id="window-title">Project maps</h1><p id="window-root" class="muted">Loading project…</p></div><a id="window-project-link" href="/projects">Open project manager</a></header><main><div id="window-message" role="status" aria-live="polite"></div>__MAP_HTML__</main>
<script>
const parameters=Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
const selected=parameters.project||'',token=document.querySelector('meta[name="setup-token"]').content;
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function message(text,error=false){const node=document.getElementById('window-message');node.textContent=text;node.style.color=error?'var(--bad)':'var(--good)'}
async function api(payload){const response=await fetch('/setup/projects',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':token},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw Error(data.error||'Could not query the project.');return data}
__MAP_SCRIPT__
document.getElementById('map-details').open=true;
(async()=>{if(!/^[a-f0-9]{16}$/.test(selected)){message('Open the project in the manager and use New window.',true);document.getElementById('load-map').disabled=true;return}try{const data=await api({action:'status',project_id:selected});document.getElementById('window-title').textContent='Maps · '+data.project.name;document.title=data.project.name+' · Maps · Smart Tool';document.getElementById('window-root').textContent=data.project.root;document.getElementById('window-project-link').href='/projects#'+selected;await window.SmartMaps.restore(parameters)}catch(e){message(e.message+' Open the manager to check the project.',true)}})();
</script></body></html>'''


_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="setup-token" content="__TOKEN__">
<title>Projects and indexing · Smart Tool</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--surface:#fff;--border:#dde1e6;--ink:#1b2430;--muted:#5b6472;--accent:#2f6fed;--good:#18723c;--bad:#b52c2c;--warn:#805300;--selected:#eaf1ff}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--surface:#1c2027;--border:#39414e;--ink:#e6e9ee;--muted:#a6afbb;--accent:#91baff;--good:#68dc93;--bad:#ff9696;--warn:#e0b565;--selected:#263751}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "Segoe UI",sans-serif}a{color:var(--accent)}button,input,textarea,select{font:inherit}select{max-width:100%;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--ink)}button{min-height:40px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--ink);padding:8px 14px;cursor:pointer}button:hover{border-color:var(--accent);background:var(--selected)}button.primary{background:var(--accent);color:#fff;border-color:transparent;font-weight:600}@media(prefers-color-scheme:dark){button.primary{color:#14171c}}button:disabled{opacity:.55;cursor:default}button.danger{color:var(--bad)}:focus-visible{outline:3px solid var(--accent);outline-offset:3px}input[type=text],textarea{width:100%;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--ink)}textarea{min-height:86px;resize:vertical;font:13px/1.5 Consolas,monospace}label{display:block;font-weight:600;margin:12px 0 5px}input[type=checkbox]{width:18px;height:18px;vertical-align:middle;margin:0 8px 0 0;accent-color:var(--accent)}header{max-width:1260px;margin:auto;padding:32px 32px 24px;display:flex;align-items:center;justify-content:space-between;gap:20px}header a{white-space:nowrap}h1{font-size:27px;line-height:1.25;margin:0 0 6px;letter-spacing:-.02em}h2{font-size:22px;line-height:1.3;margin:0 0 6px;overflow-wrap:anywhere}h3{font-size:16px;margin:0 0 12px}p{margin:6px 0 14px}small,.muted{color:var(--muted)}main{max-width:1260px;margin:auto;padding:0 32px 60px}.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-bottom:20px}.toolbar .count{margin-left:auto;color:var(--muted)}.workspace{display:grid;grid-template-columns:290px minmax(0,1fr);border:1px solid var(--border);border-radius:10px;overflow:hidden;background:var(--surface);min-height:570px}aside{border-right:1px solid var(--border);background:var(--bg);padding:18px 12px}.list-title{margin:0 10px 12px;color:var(--muted);font-size:13px;font-weight:600}.project-item{width:100%;text-align:left;border:1px solid transparent;background:transparent;margin:0 0 6px;padding:12px;display:block}.project-item[aria-current=true]{background:var(--selected);border-color:var(--accent)}.project-item strong{display:block;overflow-wrap:anywhere;font-size:14px}.project-item small{display:block;margin-top:4px}.detail{padding:28px 30px;min-width:0}.detail-head{display:flex;gap:12px;align-items:start;justify-content:space-between}.path{font:12px/1.6 Consolas,monospace;overflow-wrap:anywhere;color:var(--muted);margin:8px 0 18px}.state{font-size:12px;font-weight:600;white-space:nowrap}.state.good{color:var(--good)}.state.bad{color:var(--bad)}.state.warn{color:var(--warn)}.actions{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}.notice{border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin:14px 0;color:var(--warn);overflow-wrap:anywhere}.notice.error{color:var(--bad)}.job{padding:14px 0;border-top:1px solid var(--border)}.job-line{display:flex;justify-content:space-between;gap:12px}.job small{display:block}.job p{margin:4px 0;font-size:13px;overflow-wrap:anywhere}progress{width:100%;height:8px;accent-color:var(--accent);margin-top:12px}dl{margin:24px 0}dl>div{display:grid;grid-template-columns:155px minmax(0,1fr);gap:12px;border-top:1px solid var(--border);padding:10px 0;font-size:13px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}code{font:12px/1.6 Consolas,monospace}details{border-top:1px solid var(--border);padding:16px 0}summary{cursor:pointer;font-weight:600}details p{font-size:13px;margin-top:12px}.scope-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.empty{padding:60px 20px;max-width:550px}.empty h2{margin-bottom:12px}.empty p{color:var(--muted)}#add-form{border:1px solid var(--border);border-radius:8px;padding:18px 22px;margin:0 0 20px;background:var(--surface)}#message{margin:0 0 18px;overflow-wrap:anywhere}#message:empty{display:none}#message.error{color:var(--bad)}#message.success{color:var(--good)}.storage{font-size:12px;color:var(--muted);margin-top:18px;overflow-wrap:anywhere}.preview-samples{max-height:190px;overflow:auto;padding-left:20px;font:12px/1.8 Consolas,monospace}.check-label{font-size:14px;font-weight:400;display:flex;align-items:center}.history{margin-top:12px}footer{padding-top:16px;font-size:12px;color:var(--muted)}[hidden]{display:none!important}
@media(max-width:780px){header{padding:24px 20px 18px;align-items:start;flex-direction:column;gap:12px}main{padding:0 20px 40px}.workspace{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--border);max-height:225px;overflow:auto}.detail{padding:22px 18px}.detail-head{flex-direction:column}.scope-grid{grid-template-columns:1fr;gap:0}dl>div{grid-template-columns:1fr;gap:4px}h1{font-size:24px}.toolbar .count{width:100%;margin-left:0}.empty{padding:25px 0}.job-line{flex-wrap:wrap}}
__MAP_STYLE__
__INSIGHTS_STYLE__
</style></head><body>
<header><div><h1>Projects and indexing</h1><p class="muted">Smart Tool · Folders, updates and code search.</p></div><a href="/setup">Settings and models</a></header>
<main>
<div class="toolbar"><button class="primary" id="pick">Select folder</button><button id="add-toggle" aria-expanded="false" aria-controls="add-form">Enter path</button><span id="count" class="count">Loading projects…</span></div>
<form id="add-form" hidden><label for="root">Full folder path</label><input id="root" type="text" placeholder="C:\Projects\my-project" required autocomplete="off"><div class="actions"><button type="submit" class="primary">Add folder</button><button type="button" id="add-close">Close</button></div><small>After adding it, check the preview and start indexing.</small></form>
<div id="message" role="status" aria-live="polite"></div>
<div class="workspace"><aside aria-label="Registered projects"><p class="list-title">YOUR FOLDERS</p><div id="project-list"></div></aside>
<section class="detail" aria-label="Selected project">
<div id="empty" class="empty"><h2>Your code, ready to search.</h2><p>Select a folder to check what will be indexed. Then turn on watching to keep the index up to date when files change.</p><p>Indexes are stored in your Windows profile, outside the project folders.</p></div>
<div id="project-detail" hidden>
<div class="detail-head"><h2 id="name"></h2><span id="state" class="state"></span></div><div id="path" class="path"></div><p id="git-view" class="muted"></p>
<div id="current-jobs"></div><p id="job-announcement" role="status" aria-live="polite"></p><div id="project-warning" class="notice" hidden></div>
<div class="actions"><button id="open-maps">Open the three maps</button><button id="preview">Check preview</button><button id="index" class="primary">Index now</button><button id="pause">Pause</button><button id="resume" hidden>Resume</button><button id="cancel" hidden>Cancel job</button></div>
<label class="check-label"><input id="watch" type="checkbox">Watch for changes automatically</label><p id="watch-note" class="muted"></p>
<label for="update-mode">When to generate new embeddings</label><select id="update-mode"><option value="on_search">On the next search — save calls</option><option value="eager">After changes — prepare ahead of time</option></select><p class="muted">Detecting changes uses local resources. The ahead-of-time option may call the model even without a later search. Index now and Resume run an explicit update.</p>
<dl id="facts"></dl>
__INSIGHTS_HTML__
__MAP_HTML__
<details id="preview-details"><summary>Preview and skipped files</summary><div id="preview-data"><p class="muted">Check the preview to see the scope before generating embeddings.</p></div></details>
<details id="scope-details"><summary>Adjust scope</summary><p>Use paths relative to the folder, one per line. Exclusions accept patterns such as <code>reports/*</code>. Credentials and non-indexable files stay blocked.</p><label class="check-label"><input id="manual" type="checkbox">Keep a manual scope, without automatic redefinition</label><div class="scope-grid"><div><label for="includes">Include folders or files</label><textarea id="includes" spellcheck="false"></textarea></div><div><label for="excludes">Exclude from scope</label><textarea id="excludes" spellcheck="false"></textarea></div></div><label for="user-excludes">My permanent exclusions</label><textarea id="user-excludes" spellcheck="false"></textarea><small>These exclusions are kept when the model reassesses the scope.</small><div class="actions"><button id="save-scope">Save scope</button><button id="rescope">Reassess with the model</button></div></details>
<details><summary>Job history</summary><div id="history" class="history"></div></details>
<details><summary>Index maintenance</summary><p>Rebuild creates a new index and keeps the previous generation until it finishes. Remove deletes the index and the registration; the folder files are kept.</p><div class="actions"><button id="rebuild">Rebuild index</button><button id="remove" class="danger">Remove index and registration</button></div><label for="moved-root">Did the folder move?</label><input id="moved-root" type="text" placeholder="New full path" autocomplete="off"><p class="muted">Reassign the index to the new path. The next update checks the content before reusing the embeddings.</p><div class="actions"><button id="relocate">Reassign folder</button></div><small id="project-id"></small></details>
</div></section></div>
<p id="storage" class="storage"></p><details><summary>Model diagnostics</summary><p>Run a real embedding and rerank call with a test text. The result shows availability right now.</p><button id="probe">Check models now</button><p id="gateway" class="muted">Not checked yet in this daemon session.</p></details><footer>Content and embeddings stay in the local index. Eligible chunks are sent to the model configured in the model gateway.</footer>
</main>
<script>
const token=document.querySelector('meta[name="setup-token"]').content;
const $=id=>document.getElementById(id), esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let projects=[],selected=location.hash.slice(1),scopeKey='',scopeDirty=false,loading=false,actionBusy=false,announcementKey='';
const states={registered:'Registered',queued:'Queued',ready:'Up to date',dirty:'Update pending',paused:'Paused',error:'Needs attention',interrupted:'Interrupted',needs_config:'Check settings',unavailable:'Folder unavailable',migration_required:'Format update',preview_ready:'Preview ready',degraded:'Search with caveats',pending:'In progress',done:'Done',cancelled:'Cancelled'};
const kindNames={smart_search:'Code search',project_index:'Indexing',project_preview:'Preview'};
const formatTime=t=>t?new Date(t*1000).toLocaleString('en-US'):'Not done yet';
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'success'}
async function api(payload){const response=await fetch('/setup/projects',{method:payload?'POST':'GET',headers:{'Content-Type':'application/json','X-Setup-Token':token},...(payload?{body:JSON.stringify(payload)}:{})});const data=await response.json();if(!response.ok)throw Error(data.error||'Failed to reach the daemon.');return data}
function setAdd(open){const restore=$('add-form').contains(document.activeElement);$('add-form').hidden=!open;$('add-toggle').setAttribute('aria-expanded',String(open));if(open)$('root').focus();else if(restore)$('add-toggle').focus()}
function select(id){selected=id;scopeKey='';scopeDirty=false;window.SmartMaps?.reset();window.SmartInsights?.reset();history.replaceState(null,'','#'+id);render()}
function jobsHtml(rows){return rows.map(j=>`<div class="job"><div class="job-line"><strong>${esc(kindNames[j.kind]||j.kind)}</strong><span class="state ${j.status==='error'?'bad':''}">${esc(states[j.status]||j.status)}</span></div>${j.message?`<p>${esc(j.cancel_requested?'Cancellation requested; waiting for the current call to finish.':j.message)}</p>`:''}${j.status==='pending'?`<progress aria-label="Indexing progress" ${j.total_files>0&&j.processed_files!=null?`max="${j.total_files}" value="${j.processed_files}"`:''}></progress>`:''}${j.error?`<p class="state bad">${esc(j.error)}</p>`:''}${j.warning?`<p>${esc(j.warning)}</p>`:''}<small>${esc(j.job_id)}${j.elapsed_s!=null?' · '+j.elapsed_s+' s':''}</small></div>`).join('')}
function render(){
 $('count').textContent=projects.length+' folder'+(projects.length===1?' registered':'s registered');
 const list=$('project-list');
 for(const node of [...list.children])if(!projects.some(p=>p.id===node.dataset.project))node.remove();
 for(const p of projects){let button=list.querySelector(`[data-project="${p.id}"]`);if(!button){button=document.createElement('button');button.className='project-item';button.dataset.project=p.id;button.append(document.createElement('strong'),document.createElement('small'));list.append(button)}button.setAttribute('aria-current',String(p.id===selected));button.firstChild.textContent=p.name;button.lastChild.textContent=(states[p.status]||p.status)+(p.watch&&!p.paused?' · Watching':'')}
 if(!projects.length&&!list.children.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='No folders registered.';list.append(empty)}
 const p=projects.find(p=>p.id===selected);$('empty').hidden=!!p;$('project-detail').hidden=!p;if(!p)return;
 $('name').textContent=p.name;$('path').textContent=p.root;$('state').textContent=states[p.status]||p.status;$('state').className='state '+(['error','needs_config','unavailable'].includes(p.status)?'bad':p.status==='ready'?'good':'warn');
 $('git-view').textContent=p.view?.git?'Active branch/view: '+p.view.label+' · HEAD '+(p.view.commit?.slice(0,10)||'no commit'):'Folder without Git: one view of the working directory.';
 const active=p.jobs.filter(j=>j.status==='pending'), busy=active.length>0;
 const announcement=p.id+p.status+JSON.stringify(active.map(j=>[j.job_id,j.phase,Math.floor((j.processed_files||0)/20),j.cancel_requested]));if(announcement!==announcementKey){$('job-announcement').textContent=active.length?active.map(j=>j.cancel_requested?'Cancellation requested.':j.message).join(' '):'Project: '+(states[p.status]||p.status)+'.';announcementKey=announcement}
 $('current-jobs').innerHTML=jobsHtml(active);$('history').innerHTML=jobsHtml(p.jobs.filter(j=>j.status!=='pending'))||'<p class="muted">No finished operations.</p>';
 const warning=p.last_error||p.last_warning||p.watch_error||'';$('project-warning').hidden=!warning;$('project-warning').textContent=warning;
 $('index').textContent=p.index.exists?'Update index':'Index now';$('cancel').hidden=!busy;$('pause').hidden=p.paused||!p.enabled;$('resume').hidden=!p.paused&&!['error','interrupted','needs_config'].includes(p.status);
 for(const id of ['index','preview','rebuild','remove','relocate','save-scope','rescope'])$(id).disabled=busy||actionBusy;
 if(!actionBusy)$('watch').checked=p.watch;$('watch').disabled=actionBusy;
 if(!actionBusy)$('update-mode').value=p.update_mode||'on_search';$('update-mode').disabled=actionBusy;
 $('watch-note').textContent=p.paused?'Watching paused. A search still checks the content before answering.':!p.enabled?'Watching starts after the first indexing.':!p.watch?'No watcher: the next search or explicit update checks the files.':p.update_mode==='eager'?'Ahead-of-time update: events and reconciliation can start embeddings without a search.':'Local detection on. Changes stay pending; embeddings are updated on the next search.';
 const meta=p.index,stats=p.last_stats||{};
 const facts=[['Files and chunks',meta.exists?`${meta.files||0} files · ${meta.chunks||0} chunks`:'Index not created yet'],['Model',meta.model_id||'Validated on the first indexing'],['Dimensions',meta.dimensions||'Not checked yet'],['Last indexing',formatTime(p.last_indexed)],['Last check',formatTime(p.last_checked)],['Last update',Object.keys(stats).length?`${stats.added||0} new · ${stats.changed||0} changed · ${stats.removed||0} removed · ${stats.unchanged||0} unchanged`:'Not done yet'],['Storage',meta.path||'Created outside the project folder'],['Generation',meta.generation||'Waiting for indexing']];
 $('facts').innerHTML=facts.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');$('project-id').textContent='Project ID: '+p.id;
 if(Object.hasOwn(stats,'embedded_chunks'))$('facts').insertAdjacentHTML('beforeend',`<div><dt>Embedding usage</dt><dd>${stats.embedded_chunks} chunks sent · ${stats.reused_chunks||0} reused from cache · ${stats.embedded_characters||0} characters sent</dd></div>`);
 const preview=p.preview,skip=preview?.skipped||stats.skipped||{};
 $('preview-data').innerHTML=preview?`<p><strong>${preview.eligible_files} eligible files</strong> out of ${preview.included_files} paths in scope. ${(preview.bytes/1024/1024).toFixed(2)} MiB of content.</p><p>Skipped: ${esc(Object.entries(skip).map(([reason,n])=>n+' '+reason).join('; ')||'none in the preview')}.</p><p class="muted">Sample of analyzed paths:</p><ul class="preview-samples">${preview.sample_paths.map(x=>'<li>'+esc(x)+'</li>').join('')}</ul>`:`<p class="muted">Check the preview to see the scope before generating embeddings.</p>${Object.keys(skip).length?'<p>Last indexing: '+esc(Object.entries(skip).map(([r,n])=>n+' '+r).join('; '))+'</p>':''}`;
 const key=p.id+JSON.stringify(p.scope);if(key!==scopeKey&&!scopeDirty){const s=p.scope||{include:['.'],exclude:[]};$('manual').checked=!!s.manual;$('includes').value=(s.include||[]).join('\n');$('excludes').value=(s.exclude||[]).join('\n');$('user-excludes').value=(s.user_exclude||[]).join('\n');scopeKey=key}
}
async function refresh(){if(loading)return;loading=true;try{const data=await api();projects=data.projects;$('storage').textContent='Index directory: '+data.storage_dir;const g=data.gateway||{};if(g.checked_at)$('gateway').textContent=formatTime(g.checked_at)+' · '+(g.error||['embedding','rerank'].map(k=>k+': '+(g[k]?.status==='ok'?'available ('+g[k].elapsed_s+' s)':g[k]?.error||'not configured')).join(' · '));if(!projects.some(p=>p.id===selected)){window.SmartMaps?.reset();window.SmartInsights?.reset();selected=projects[0]?.id||'';}render()}catch(e){message(e.message+' Reopen this page if the daemon has restarted.',true)}finally{loading=false}}
async function act(action,extra={}){if(actionBusy)return;actionBusy=true;render();try{const data=await api({action,project_id:selected,...extra});if(data.cancelled){message('Folder selection cancelled.');return}if(data.project&&['register','pick','relocate'].includes(action)){if(selected!==data.project.id)window.SmartMaps?.reset();window.SmartInsights?.reset();selected=data.project.id;scopeDirty=false;scopeKey='';history.replaceState(null,'','#'+selected);setAdd(false)}if(action==='scope')scopeDirty=false;message(data.job?'Operation started. Progress shows up in the project.':action==='remove'?'Index and registration removed. Project files kept.':action==='pick'||action==='register'?'Folder added. Check the preview before indexing.':action==='probe'?'Check finished. See the model diagnostics.':'Change saved.');await refresh();if(action==='preview')$('preview-details').open=true;if(action==='remove')$('add-toggle').focus()}catch(e){message(e.message,true)}finally{actionBusy=false;render()}}
$('project-list').addEventListener('click',e=>{const b=e.target.closest('[data-project]');if(b)select(b.dataset.project)});
$('add-toggle').onclick=()=>setAdd($('add-form').hidden);$('add-close').onclick=()=>setAdd(false);
$('add-form').onsubmit=e=>{e.preventDefault();act('register',{project_root:$('root').value.trim()})};
$('pick').onclick=async()=>{$('pick').disabled=true;message('Select the folder in the Windows dialog.');await act('pick');$('pick').disabled=false};
for(const action of ['preview','index','pause','resume','cancel'])$(action).onclick=()=>act(action);
$('watch').onchange=()=>act('watch',{watch:$('watch').checked});
$('update-mode').onchange=()=>act('policy',{update_mode:$('update-mode').value});
for(const id of ['includes','excludes','user-excludes','manual'])$(id).addEventListener('input',()=>{scopeDirty=true});
const lines=id=>$(id).value.split('\n').map(s=>s.trim()).filter(Boolean);
$('save-scope').onclick=()=>act('scope',{manual:$('manual').checked,include:lines('includes'),exclude:lines('excludes'),user_exclude:lines('user-excludes')});
$('rescope').onclick=()=>act('preview',{force_scope:true});
$('rebuild').onclick=()=>{if(confirm('Rebuild the index of this project? Eligible content will be sent to the embedding model again.'))act('rebuild')};
$('remove').onclick=()=>{const p=projects.find(p=>p.id===selected);if(confirm('Remove the index and registration of '+p.name+'? The folder files will be kept.'))act('remove')};
$('relocate').onclick=()=>act('relocate',{project_root:$('moved-root').value.trim()});
$('probe').onclick=async()=>{$('probe').disabled=true;message('Checking the models with real calls…');await act('probe');$('probe').disabled=false};
__MAP_SCRIPT__
__INSIGHTS_SCRIPT__
$('open-maps').onclick=()=>{$('map-details').open=true;$('map-details').scrollIntoView({block:'start',behavior:'auto'});window.SmartMaps.load()};
window.addEventListener('hashchange',()=>select(location.hash.slice(1)));
refresh();setInterval(()=>{if(!document.hidden)refresh()},2500);
</script></body></html>'''
