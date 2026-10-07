import sys, os
D = os.path.dirname(os.path.abspath(__file__))

def icon(d):
    return f'<svg viewBox="0 0 16 16" aria-hidden="true" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">{d}</svg>'
NAV = [
 ("Home", icon('<path d="M3 8.5 8 4l5 4.5V13H3z"/>')),
 ("Recall", icon('<circle cx="7" cy="7" r="4"/><path d="m10 10 3.5 3.5"/>')),
 ("Owed", icon('<path d="M3 4h10M3 8h7M3 12h5"/><path d="m11 11 1.5 1.5L15 10"/>')),
 ("Activity", icon('<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3l2 1.5"/>')),
 ("Harnesses", icon('<path d="M5 3v3M11 3v3M3 6h10v3a5 5 0 0 1-10 0z"/><path d="M8 11v3"/>')),
 ("Settings", icon('<path d="M3 5h10M3 11h10"/><circle cx="6" cy="5" r="1.5"/><circle cx="10" cy="11" r="1.5"/>')),
]
SUB = ["Database","Capture & models","Optional features","Embeddings","Data & backups","Another Mac","Components","Updates","Advanced"]

def shell(active_sub, pane, dialog="", theme="light"):
    nav = "".join(f'<button type="button" class="nav{" active" if n=="Settings" else ""}"{" aria-current=\"page\"" if n=="Settings" else ""}>{i}<span class="nav-label">{n}</span></button>' for n,i in NAV)
    sub = "".join(f'<button type="button" role="tab" aria-selected="{"true" if s==active_sub else "false"}"{" class=\"on\"" if s==active_sub else ""}>{s}</button>' for s in SUB)
    css = "App-dark.css" if theme=="dark" else "App-light.css"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Khipu settings mock</title><link rel="stylesheet" href="{css}"><link rel="stylesheet" href="mock.css"></head>
<body><div id="root"><div class="shell">
<nav class="rail" aria-label="Primary"><div class="rail-drag"></div>
<div class="brand"><img class="brand-icon" src="khipu-icon.png" width="26" height="26" alt=""><span class="brand-name">Khipu</span></div>
<div class="rail-nav"><div class="nav-group">{nav}</div></div>
<div class="rail-foot"><div class="rail-health"><span class="hdot ok" aria-hidden="true"></span><span>Memory is working</span></div>
<div class="rail-meta"><button type="button" class="rail-feedback">Feedback</button><span class="version-chip">v0.4.7</span></div></div></nav>
<main class="main"><section class="panel"><header class="panel-head"><div class="panel-head-text"><h1>Settings</h1><p class="lede">Capture, models, data and this Mac.</p></div></header>
<div class="panel-body wide settings-body"><div class="settings-split">
<div class="subnav" role="tablist" aria-label="Settings sections">{sub}</div>
<div class="settings-pane">{pane}</div></div></div></section></main></div></div>
{dialog}
<script>document.querySelectorAll('dialog').forEach(d=>d.showModal());if(document.activeElement)document.activeElement.blur();var l=document.getElementById('libs');if(l&&location.hash==='#libs')var m=document.querySelector('.main');m.scrollTop+=l.getBoundingClientRect().top-92;</script></body></html>'''


def tag(t, txt, dot=False):
    return f'<span class="tag {t}">{"<span class=dot aria-hidden=true></span>" if dot else ""}{txt}</span>'

def cov(label, done, total, tone="", tagtxt="", tagtone=""):
    pct = round(done*100/total) if total else 0
    cls = "bar ok" if tone=="ok" else "bar"
    t = tag(tagtone, tagtxt, True) if tagtxt else ""
    return f'<span>{label}</span><div class="{cls}" role="progressbar" aria-label="{label} coverage" aria-valuemin="0" aria-valuemax="{total}" aria-valuenow="{done}"><i style="width:{pct}%"></i></div><span class="num">{done:,} of {total:,} pieces</span><span>{t}</span>'

def btn(txt, cls="", dis=False):
    return f'<button type="button"{f" class=\"{cls}\"" if cls else ""}{" disabled" if dis else ""}>{txt}</button>'

def act(txt, cls="", dis=False, why=""):
    w = f'<span class="why">{why}</span>' if why else ""
    return f'<div class="act">{btn(txt,cls,dis)}{w}</div>'

def row_head(name, pills, q):
    return f'<div class="emb-top"><div class="emb-id"><span class="job-name mono">{name}</span>{pills}</div><div class="emb-q">Typical search delay <b>{q}</b></div></div>'

def gemini_row(redo=False):
    if redo:
        c = cov("Memory",21067,22271,"","1,204 to redo","warn")
        panel = f'''<div class="receipt" role="group" aria-label="Pieces of text to redo"><div class="top emb-top"><div class="job-main"><span class="job-name">Redo some</span><span class="job-meta">1,204 pieces of text have no index entry or an out-of-date one.</span></div></div>
<div class="line"><span><b>1,120</b> missing (never embedded)</span><span><b>84</b> stale (the text changed after it was embedded)</span></div>
<ul class="redo-list" aria-label="Examples"><li><span>Session 18,402 · Library sources and bring-your-own embeddings</span>{tag("warn","Missing")}</li><li><span>Session 18,399 · Settings parity review</span>{tag("warn","Missing")}</li><li><span>Topic page · embedding-profiles</span>{tag("neutral","Stale")}</li><li><span>Topic page · nightly-jobs</span>{tag("neutral","Stale")}</li><li><span>and 1,200 more</span></li></ul>
<div class="emb-acts" style="margin-top:var(--s1)">{act("Embed missing (1,204)","primary",False,"Shows an estimate first.")}</div></div>'''
        acts = f'<div class="emb-acts">{act("Embed missing (1,204)",why="Shows an estimate first.")}{act("Delete index",dis=True,why="Memory search uses it.")}</div>'
        return f'''<div class="emb-row" role="group" aria-label="gemini-embedding-2@768">{row_head("gemini-embedding-2@768",tag("accent","Active for memory"),"412 ms")}
<span class="job-meta">Gemini · Key: saved</span><div class="emb-cov">{c}</div>{panel}</div>'''
    return f'''<div class="emb-row" role="group" aria-label="gemini-embedding-2@768">{row_head("gemini-embedding-2@768",tag("accent","Active for memory"),"412 ms")}
<span class="job-meta">Gemini · Key: saved</span>
<div class="emb-cov">{cov("Memory",22271,22271,"ok","Complete","ok")}</div>
<div class="emb-acts">{act("Embed missing",dis=True,why="Nothing missing.")}{act("Delete index",dis=True,why="Memory search uses it.")}</div></div>'''

def voyage_row(unused=False):
    if unused:
        pills = tag("neutral","Not used")
        c = f'<span>Index</span><span class="sub-sm" style="grid-column:2/5">329,224 pieces of text. No library or memory uses it.</span>'
        acts = f'<div class="emb-acts">{act("Delete index")}</div>'
    else:
        pills = tag("neutral","Library: biblical")
        c = cov("Library “biblical”",329224,329224,"ok","Complete","ok")
        acts = f'<div class="emb-acts">{act("Delete index",dis=True,why="Used by biblical.")}</div>'
    return f'''<div class="emb-row" role="group" aria-label="voyage-3@1024">{row_head("voyage-3@1024",pills,"180 ms")}
<span class="job-meta">Voyage · Key: saved</span><div class="emb-cov">{c}</div>{acts}</div>'''

def nomic_row(state):
    keytxt = "No key needed"
    base = f'''<div class="emb-row" role="group" aria-label="nomic-embed-text@768">{row_head("nomic-embed-text@768","","210 ms")}
<span class="job-meta">OpenAI-compatible · <span class="mono">http://localhost:11434</span> · {keytxt}</span>'''
    if state=="running":
        return base + f'''<div class="emb-cov">{cov("Memory",8120,22271,"","In progress","warn")}</div>
<div class="emb-job" role="group" aria-label="Re-embedding job"><div class="top"><div class="job-main"><span class="job-name">Re-embedding memory</span><span class="job-meta">8,120 of 22,271 pieces of text, about 40 min left. Search keeps using gemini-embedding-2@768.</span></div><div class="job-actions">{btn("Cancel","sm")}</div></div>
<div class="bar" role="progressbar" aria-label="Re-embedding memory" aria-valuemin="0" aria-valuemax="22271" aria-valuenow="8120"><i style="width:36%"></i></div></div>
<div class="emb-acts">{act("Make active for memory",dis=True,why="14,151 missing")}{act("Embed missing",dis=True,why="A job is running.")}{act("Delete index",dis=True,why="A job is running.")}</div></div>'''
    if state=="failed":
        return base + f'''<div class="emb-cov">{cov("Memory",8120,22271,"","Partial","warn")}</div>
<div class="emb-job err-job" role="alert"><div class="top"><div class="job-main"><span class="job-name">Re-embedding stopped</span><span class="job-meta" style="color:var(--text)">Embedded 8,120 of 22,271, stopped: the server at localhost:11434 did not answer.</span></div><div class="job-actions">{btn("Retry","sm primary")}{btn("Cancel","sm")}</div></div></div>
<div class="emb-acts">{act("Make active for memory",dis=True,why="14,151 missing")}{act("Delete index",dis=True,why="Cancel or retry the job first.")}</div></div>'''
    if state=="ready":
        return base + f'''<div class="emb-cov">{cov("Memory",22271,22271,"ok","Complete","ok")}</div>
<div class="emb-acts">{act("Make active for memory","primary",False,"Takes effect on the next search.")}{act("Embed missing",dis=True,why="Nothing missing.")}{act("Delete index")}</div></div>'''
    return base + f'''<div class="emb-cov">{cov("Memory",0,22271,"","Not started","warn")}</div>
<div class="emb-acts">{act("Re-embed memory with this model",why="Shows an estimate first.")}{act("Make active for memory",dis=True,why="22,271 missing")}{act("Delete index",dis=True,why="Nothing to delete yet.")}</div></div>'''

def models_card(state, redo=False, voyage_unused=False):
    return f'''<div class="section-card"><div class="section-head">Models<span class="spacer"></span>{btn("Add a model","primary")}</div>
<div class="emb-list">{gemini_row(redo)}{voyage_row(voyage_unused)}{nomic_row(state)}</div>
<p class="help-line">Nothing is embedded until you confirm an estimate.</p></div>'''

def lib_card(mode="normal"):
    if mode=="empty":
        row = '<p class="set-help help-pad" style="padding-bottom:var(--s3)">No libraries yet.</p>'
    else:
        receipt = ""
        if mode=="receipt":
            receipt = f'''<div class="receipt" role="group" aria-label="Import receipt"><div class="top emb-top"><div class="job-main"><span class="job-name">Imported an index</span></div>{tag("ok","Done",True)}</div>
<div class="line"><span>Read <b>329,859</b> rows</span><span>·</span><span>Imported <b>329,224</b></span><span>·</span><span>Skipped <b>635</b> corrupt</span><span>·</span><span><b>0</b> with no matching file</span></div></div>'''
        row = f'''<div class="lib-row" role="group" aria-label="Library biblical">
<div class="emb-top"><div class="emb-id"><span class="job-name">biblical</span>{tag("ok","100% embedded",True)}</div></div>
<span class="path">~/Documents/biblical-library</span>
<div class="lib-stats"><span><b>5,581</b> documents</span><span><b>329,224</b> pieces of text</span><span>Model <b style="font-family:var(--mono);font-size:var(--t-sm)">voyage-3@1024</b></span></div>
<div class="emb-acts">{act("Scan")}{act("Embed missing",dis=True,why="Nothing missing.")}{act("Re-embed biblical with…")}{act("Import an index")}{act("Remove","danger")}</div>{receipt}</div>'''
    add = ""
    if mode=="empty":
        folder, name = "", ""
    else:
        folder, name = "~/Documents/talks", "talks"
    ph_f = ' placeholder="/path/to/folder"' if not folder else ""
    ph_n = ' placeholder="Short name"' if not name else ""
    en = bool(folder and name)
    add = f'''<div class="lib-add"><span class="set-label" id="addlib">Add a library</span>
<div class="fields" role="group" aria-labelledby="addlib">
<div class="set-field"><label class="set-label" for="lf">Folder</label><div class="folder"><input id="lf" class="mono" value="{folder}"{ph_f} spellcheck="false"><button type="button">Choose…</button></div></div>
<div class="set-field"><label class="set-label" for="ln">Name</label><input id="ln" value="{name}"{ph_n}></div>
<div class="set-field full"><label class="set-label" for="lm">Model</label><select id="lm"><option>voyage-3@1024 (typical delay 180 ms · $0.06 per 1M tokens)</option><option>gemini-embedding-2@768 (typical delay 412 ms · price unknown)</option><option>nomic-embed-text@768 (typical delay 210 ms · local, no charge)</option></select></div></div>
<div class="emb-acts" style="margin-top:0">{act("Add library","primary",not en,"" if en else "Choose a folder and a name.")}</div></div>'''
    return f'''<div class="section-card" id="libs"><div class="section-head">Libraries</div>
<p class="set-help help-pad">A library is a folder of .txt and .md files that search can read. Each library has its own model.</p>
{row}{add}<p class="help-line">Embed missing and Re-embed show an estimate first. Nothing is embedded until you confirm it.</p></div>'''

INTRO = '<p class="set-help set-intro">Khipu turns text into numbers so it can search by meaning. Each model below is one way of doing that.</p>'

def pane(state="running", mode="normal", **kw):
    return INTRO + models_card(state, **kw) + lib_card(mode)

def dlg(title, body, actions, labelid):
    return f'<dialog class="kit-dialog" aria-labelledby="{labelid}"><div class="kit-dialog-body"><h2 id="{labelid}">{title}</h2>{body}<div class="kit-dialog-actions">{actions}</div></div></dialog>'

ADD_BODY = f'''<div class="field-grid">
<div class="set-field"><label class="set-label" for="prov">Provider</label><select id="prov"><option>Gemini</option><option>Voyage</option><option selected>OpenAI-compatible</option></select></div>
<div class="two"><div class="set-field"><label class="set-label" for="mid">Model id</label><input id="mid" class="mono" value="nomic-embed-text" spellcheck="false"></div>
<div class="set-field"><label class="set-label" for="dim">Dimensions</label><input id="dim" class="mono" inputmode="numeric" value="768" aria-describedby="dimhelp"></div></div>
<p id="dimhelp" class="set-help" style="margin-top:calc(var(--s2) * -1)">Dimensions: ask your provider; wrong values are refused.</p>
<div class="set-field"><label class="set-label" for="ep">Endpoint</label><input id="ep" class="mono" value="http://localhost:11434" spellcheck="false"></div>
<div class="set-field"><label class="set-label" for="key">Key</label><div class="test-line"><input id="key" type="password" value="sk-example" autocomplete="new-password" aria-describedby="keyhelp" style="flex:1"><button type="button">Test key</button>{tag("ok","ok, 196 ms",True)}</div>
<p id="keyhelp" class="set-help">Optional for a local server. Stored in the Keychain and never shown again.</p></div></div>'''

ESTIMATE_BODY = '''<dl class="dl"><dt>Size</dt><dd>22,271 pieces of text, about 14M tokens</dd>
<dt>Time</dt><dd>about 40 min at the measured rate</dd>
<dt>Price</dt><dd>unknown for this provider (a local server costs nothing)</dd></dl>
<p>Search keeps using gemini-embedding-2 until you choose Make active.</p>'''

DELETE_BODY = '<p>This removes the index entries for 22,271 pieces of text. The text stays. You can embed again later.</p>'

def write(name, html):
    open(os.path.join(D, name + ".html"), "w").write(html)

for th in ("light","dark"):
    write(f"embeddings-list-{th}", shell("Embeddings", pane("running"), theme=th))
    write(f"libraries-{th}", shell("Embeddings", pane("running"), theme=th))
    write(f"embeddings-add-model-{th}", shell("Embeddings", pane("running"), dlg("Add a model", ADD_BODY, btn("Cancel")+btn("Add model","primary"), "add-title"), th))
write("embeddings-estimate-light", shell("Embeddings", pane("estimate"), dlg("Re-embed memory with nomic-embed-text?", ESTIMATE_BODY, btn("Cancel")+btn("Start","primary"), "est-title"), "light"))
write("embeddings-coverage", shell("Embeddings", pane("running", redo=True)))
write("embeddings-delete-confirm", shell("Embeddings", pane("ready"), dlg("Delete the index for nomic-embed-text?", DELETE_BODY, btn("Cancel")+btn("Delete index","danger"), "del-title")))
write("embeddings-import-receipt", shell("Embeddings", pane("running", mode="receipt")))
write("embeddings-job-failed", shell("Embeddings", pane("failed")))
write("embeddings-ready", shell("Embeddings", pane("ready")))
