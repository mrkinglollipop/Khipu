"""Regenerate the routing slice 14 mocks: python3 gen.py

Builds App-light.css and App-dark.css from the app's own stylesheets (apps/desktop/src), then writes every
screen in a light and a dark file. All accounts, projects, Macs, words and times are fictional. "Now" is
Wednesday 2026-10-21 at about 2:40 PM.
"""
import os
import re
import shutil

D = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(D, "..", "..", "..", "apps", "desktop", "src"))
CSS = ["App.css", "routingOverview.css", "accounts.css"]


# --- Stylesheets: the app's own, with the dark media blocks resolved one way or the other. -------------

def _media_blocks(text):
    """Yield (start, inner_start, inner_end, end) for each prefers-color-scheme: dark block."""
    for m in re.finditer(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{", text):
        depth, i = 1, m.end()
        while depth:
            c = text[i]
            depth += c == "{"
            depth -= c == "}"
            i += 1
        yield m.start(), m.end(), i - 1, i


def themed(text, dark):
    out, pos = [], 0
    for s, a, b, e in _media_blocks(text):
        out.append(text[pos:s])
        if dark:
            out.append(text[a:b])
        pos = e
    out.append(text[pos:])
    return "".join(out)


def build_css():
    raw = "\n".join(open(os.path.join(SRC, f), encoding="utf-8").read() for f in CSS)
    for name, dark in (("App-light.css", False), ("App-dark.css", True)):
        head = f"/* Built by gen.py from apps/desktop/src/{', '.join(CSS)}; {'dark' if dark else 'light'} theme. */\n"
        scheme = ":root{color-scheme:dark}\n" if dark else ":root{color-scheme:light}\n"
        open(os.path.join(D, name), "w", encoding="utf-8").write(head + themed(raw, dark) + scheme)
    shutil.copyfile(os.path.join(SRC, "assets", "khipu-icon.png"), os.path.join(D, "khipu-icon.png"))


# --- Small builders. --------------------------------------------------------------------------------------

def icon(d, size=16):
    return (f'<svg viewBox="0 0 16 16" aria-hidden="true" width="{size}" height="{size}" fill="none" stroke="currentColor" '
            f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">{d}</svg>')


CHEV = ('<svg class="disclosure-chevron" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>')

NAV = [
    ("Home", '<path d="M3 8.5 8 4l5 4.5V13H3z"/>'),
    ("Recall", '<circle cx="7" cy="7" r="4"/><path d="m10 10 3.5 3.5"/>'),
    ("Owed", '<path d="M3 4h10M3 8h7M3 12h5"/><path d="m11 11 1.5 1.5L15 10"/>'),
    ("Activity", '<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3l2 1.5"/>'),
    ("Harnesses", '<path d="M5 3v3M11 3v3M3 6h10v3a5 5 0 0 1-10 0z"/><path d="M8 11v3"/>'),
    ("Settings", '<path d="M3 5h10M3 11h10"/><circle cx="6" cy="5" r="1.5"/><circle cx="10" cy="11" r="1.5"/>'),
]
SUB = ["Database", "Capture & models", "Accounts and routing", "Optional features", "Embeddings", "Data & backups",
       "Another Mac", "Components", "Updates", "Advanced"]


def shell(title, pane, theme):
    nav = "".join(
        f'<button type="button" class="nav{" active" if n == "Settings" else ""}"'
        f'{" aria-current=\"page\"" if n == "Settings" else ""}>{icon(p)}<span class="nav-label">{n}</span></button>'
        for n, p in NAV)
    sub = "".join(
        f'<button type="button" role="tab" aria-selected="{"true" if s == "Accounts and routing" else "false"}"'
        f'{" class=\"on\"" if s == "Accounts and routing" else ""}>{s}</button>' for s in SUB)
    css = "App-dark.css" if theme == "dark" else "App-light.css"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Khipu mock: {title}</title><link rel="stylesheet" href="{css}"><link rel="stylesheet" href="mock.css"></head>
<body><div id="root"><div class="shell">
<nav class="rail" aria-label="Primary"><div class="rail-drag"></div>
<div class="brand"><img class="brand-icon" src="khipu-icon.png" width="26" height="26" alt=""><span class="brand-name">Khipu</span></div>
<div class="rail-nav"><div class="nav-group">{nav}</div></div>
<div class="rail-foot"><div class="rail-health"><span class="hdot ok" aria-hidden="true"></span><span>All harnesses recording</span></div>
<div class="rail-meta"><button type="button" class="rail-feedback">Feedback</button><span class="version-chip">v0.6.0</span></div></div></nav>
<main class="main"><section class="panel"><header class="panel-head"><div class="panel-head-text"><h1>Settings</h1><p class="lede">Capture, models, data and this Mac.</p></div></header>
<div class="panel-body wide settings-body"><div class="settings-split">
<div class="subnav" role="tablist" aria-label="Settings sections">{sub}</div>
<div class="settings-pane"><div class="route-settings">{pane}</div></div></div></div></section></main></div></div></body></html>'''


def tag(tone, text, dot=True):
    cls = f"tag {tone}".strip()
    d = '<span class="dot" aria-hidden="true"></span>' if dot else ""
    return f'<span class="{cls}">{d}{text}</span>'


def btn(text, cls="sm", label=None, dis=False, extra=""):
    a = f' aria-label="{label}"' if label else ""
    return f'<button type="button" class="{cls}"{a}{" disabled" if dis else ""}{extra}>{text}</button>'


def group(key, name, line, tags="", body="", open_=True):
    exp = "true" if open_ else "false"
    hidden = "" if open_ else " hidden"
    return (f'<div class="fold disclose route-group" data-open="{exp}">'
            f'<button type="button" class="disclosure fold-btn" aria-expanded="{exp}" aria-controls="g-{key}">{CHEV}'
            f'<span class="gname">{name}</span><span class="gline">{line}</span><span class="gtags">{tags}</span></button>'
            f'<div class="fold-body" id="g-{key}" role="region" aria-label="{name}"{hidden}><div class="disclose-body">{body}</div></div></div>')


def sec(head, inner, meta="", help_=""):
    m = f' <span class="meta">{meta}</span>' if meta else ""
    h = f'<p class="set-help">{help_}</p>' if help_ else ""
    return f'<div class="section-body"><h2 class="sub-title">{head}{m}</h2>{h}{inner}</div>'


def subfold(name, line):
    return (f'<div class="fold disclose route-sub-fold"><button type="button" class="disclosure fold-btn" aria-expanded="false">'
            f'{CHEV}<span>{name}</span><span class="sline">{line}</span></button></div>')


def top():
    return ('<div class="route-top"><p class="set-help set-intro">Khipu tells your conversations which accounts have room, '
            'what you paused and what your rules are. Your agents decide where work goes.</p><div class="grp-btns">'
            + btn("Open all groups") + btn("Close all groups") + '</div></div>')


def needs(items):
    lis = "".join(f'<li><span>{t}</span>{btn("Show", label="Show " + t)}</li>' for t in items)
    return f'<div class="callout warn needs" role="region" aria-label="Needs you"><div class="txt"><b>Needs you</b><ul>{lis}</ul></div></div>'


def panel(cap, inner):
    return f'<section class="state-panel" aria-label="{cap}"><p class="state-cap">{cap}</p>{inner}</section>'


def err_box(text):
    return (f'<div class="set-error err-inline" role="alert"><span>{text}</span>{btn("Try again")}</div>')


def empty(title, text, action=""):
    a = f'<div class="toolbar" style="margin-top:var(--s2)">{action}</div>' if action else ""
    return f'<div class="empty"><b>{title}</b><span>{text}</span>{a}</div>'


# --- The closed group lines, shared by the overview and each open screen. ---------------------------------

CLOSED = {
    "accounts": ("Accounts", "7 accounts: 5 open, 1 at its limit, 1 not read yet", tag("warn", "1 at its limit")),
    "pace": ("Pace, caps and advice", "Claude reaches a cap at 4:40 PM; Muse closed",
             tag("warn", "Claude outpacing") + tag("warn", "3 hold back for jobs")),
    "priority": ("Priority", "Codex first, then Claude second, then Claude", ""),
    "rules": ("Your note and standing rules", "Note from Oct 20 · 7 rules", ""),
    "proposals": ("Proposals and holds", "2 proposals in use, 1 crossed · 2 holds", tag("warn", "2 to confirm")),
    "checks": ("Checks and copies", "3 model mismatches today · 2 contradictions · 4 copies in files",
               tag("warn", "2 contradictions")),
    "telling": ("Telling conversations", "8 live conversations; 1 is behind · 1 Mac behind",
                tag("err", "1 behind") + tag("err", "1 Mac behind")),
    "resets": ("Checking for resets", "On: up to 3 checks after each reset", ""),
    "lanes": ("Providers and lanes", "6 providers · 2 folders, 9 lanes", tag("warn", "2 lanes without an account")),
    "table": ("Old routing table file", "Not read; Khipu's record is the only source", ""),
    "changes": ("Recent changes", "11 changes today", ""),
}
ORDER = ["accounts", "pace", "priority", "rules", "proposals", "checks", "telling", "resets", "lanes", "table", "changes"]
NEEDS = ["Codex in project-b hasn't been told 2 changes",
         "MacBook Air's routing is 2 h 14 min old",
         "Claude reaches your five-hour 60% at 4:40 PM",
         "2 proposals from conversations to confirm",
         "Lane claude in project-a matches two accounts",
         "Priority 2 names Grok, which you paused"]


def page(open_key=None, body="", closed_only=False):
    parts = [top(), needs(NEEDS)]
    for k in ORDER:
        n, line, tags = CLOSED[k]
        if k == open_key:
            parts.append(group(k, n, line, tags, body, True))
        else:
            parts.append(group(k, n, line, tags, "", False))
    return "".join(parts)


# --- 1. Delivery and health: the "Telling conversations" group. -------------------------------------------

def conv_row(who, sub, mac, last, state):
    return (f'<div class="mrow conv" role="listitem"><span><span class="name">{who}</span><br><span class="src">{sub}</span></span>'
            f'<span class="mac-col">{mac}</span><span class="num">{last}</span><span class="end">{state}</span></div>')


def delivery_body(red=True):
    alert = ""
    if red:
        alert = ('<div class="callout err" role="alert"><div class="txt"><b>Two things haven\'t reached conversations</b>'
                 '<span>Codex in project-b still hasn\'t been told 2 changes after 6 prompts. Khipu expected it within 4. '
                 'Its next prompt will try again.</span>'
                 '<span>MacBook Air\'s copy of routing is 2 h 14 min old. Conversations there are told so.</span>'
                 '</div></div>')
    conv = ('<div class="mrows" role="list" aria-label="Live conversations">'
            '<div class="mhead conv" aria-hidden="true"><span>Conversation</span><span class="mac-col">Mac</span><span>Last prompt</span><span>What it has</span></div>'
            + conv_row("Claude Code · project-a", "and 2 helpers it started", "This Mac", "2 min ago", tag("ok", "Up to date"))
            + conv_row("Codex · project-b", "hears at each prompt", "This Mac", "6 min ago", tag("err", "2 changes owed, 6 prompts"))
            + conv_row("T3 thread · project-c", "now on Codex, was Claude Code", "This Mac", "18 min ago", tag("ok", "Up to date"))
            + conv_row("Job runner · project-c", "3 jobs running", "Office mini", "1 min ago", tag("ok", "Up to date"))
            + conv_row("Grok Build · project-d", "hears only when it calls Khipu", "This Mac", "1 h ago", tag("", "1 change waiting for its next call"))
            + conv_row("Aegis · project-a", "hears only when it calls Khipu", "MacBook Air", "3 h ago", tag("", "Not known: that Mac hasn't synced"))
            + conv_row("Grok Bot", "through the cloud copy", "Cloud", "9 min ago", tag("ok", "Up to date"))
            + conv_row("Claude Code · project-d", "", "Office mini", "4 h ago", tag("ok", "Up to date"))
            + '</div>')
    chg = ('<div class="mrows" role="list" aria-label="Recent changes and who has them">'
           '<div class="mrow chg" role="listitem"><span class="num">2:31 PM</span><span>You edited rule 1<br><span class="src">Changed by you in this app</span></span>'
           '<span class="end">' + tag("warn", "7 of 8 have it") + btn("Who's waiting", label="Who is waiting for rule 1") + '</span></div>'
           '<div class="mrow chg" role="listitem"><span class="num">1:55 PM</span><span>Muse reached its weekly limit, back Mon 12:00 AM<br><span class="src">Reported by a Claude Code conversation in project-a</span></span>'
           '<span class="end">' + tag("ok", "8 of 8 have it") + '</span></div>'
           '<div class="mrow chg" role="listitem"><span class="num">11:20 AM</span><span>You ended the hold on Claude second<br><span class="src">Changed by you from a conversation</span></span>'
           '<span class="end">' + tag("ok", "8 of 8 have it") + '</span></div></div>')
    macs = ('<div class="mrows" role="list" aria-label="Other Macs and the cloud copy">'
            '<div class="mrow mac" role="listitem"><span class="name">MacBook Air</span><span>Its copy is 2 h 14 min old</span><span class="end">' + tag("err", "Behind") + '</span></div>'
            '<div class="mrow mac" role="listitem"><span class="name">Office mini</span><span>Its copy is 40 s old</span><span class="end">' + tag("ok", "Current") + '</span></div>'
            '<div class="mrow mac" role="listitem"><span class="name">Cloud copy</span><span>Updated 3 min ago, for Grok Bot and other cloud agents</span><span class="end">' + tag("ok", "Current") + '</span></div></div>')
    return (alert
            + sec("Live conversations", conv, "a prompt in the last 12 hours",
                  "A conversation that is behind keeps getting the changes; nothing is stopped.")
            + sec("Recent changes and who has them", chg)
            + sec("Other Macs and the cloud", macs)
            + '<div class="section-body">' + subfold("What each kind of conversation hears", "7 kinds")
            + subfold("Sizes and times", "Live for 12 h · warn after 2 extra prompts · warn when a Mac is 10 min behind") + '</div>')


def delivery_states():
    n, _, _ = CLOSED["telling"]
    a = group("t1", n, "No live conversations", "", empty(
        "No conversation has had a prompt in the last 12 hours",
        "When one starts, it shows here with what it has been told.") +
        '<div class="section-body">' + subfold("Sizes and times", "Live for 12 h") + '</div>')
    b = group("t2", n, "8 live conversations; 2 can't be checked", tag("", "2 can't be checked"),
              '<div class="mrows" role="list" aria-label="Live conversations">'
              + conv_row("Claude Code · project-a", "", "This Mac", "2 min ago", tag("ok", "Up to date"))
              + conv_row("A Khipu caller with no conversation name", "a tool that doesn\'t say which conversation it is in", "This Mac", "5 min ago", tag("", "Can't be tracked"))
              + conv_row("Aegis · project-a", "hears only when it calls Khipu", "MacBook Air", "3 h ago", tag("", "Not known: that Mac hasn't synced"))
              + '</div><p class="set-help">A caller with no conversation name gets the current routing each time, but Khipu can\'t tell what it has seen. '
                'Give it a conversation name to track it.</p>')
    c = group("t3", n, '<span class="reading">Couldn\'t read</span>', tag("err", "Can't read delivery"),
              err_box("Couldn't read who has been told what: the delivery ledger didn't open. "
                      "Nothing below is a guess; it stays blank until the read works.") +
              '<p class="set-help">Conversations still get routing at each prompt; only this list is unavailable.</p>')
    d = group("t4", n, "8 live conversations · this Mac can't reach the others", tag("err", "Offline"),
              '<div class="callout err" role="alert"><div class="txt"><b>This Mac can\'t reach your other Macs</b>'
              '<span>For 14 min. Routing from MacBook Air and Office mini is 14 min old, and every conversation here is told so. '
              'Changes made here are kept and sent when the connection is back.</span></div></div>'
              + sec("Other Macs and the cloud",
                    '<div class="mrows" role="list" aria-label="Other Macs"><div class="mrow mac" role="listitem"><span class="name">MacBook Air</span><span>Last heard 14 min ago</span><span class="end">' + tag("err", "Not reachable") + '</span></div>'
                    '<div class="mrow mac" role="listitem"><span class="name">Office mini</span><span>Last heard 14 min ago</span><span class="end">' + tag("err", "Not reachable") + '</span></div>'
                    '<div class="mrow mac" role="listitem"><span class="name">Cloud copy</span><span>Last updated 14 min ago; cloud agents see it as of then</span><span class="end">' + tag("warn", "Old") + '</span></div></div>'))
    return (panel("No live conversations (empty)", a) + panel("Some conversations can't be checked (unknown)", b)
            + panel("The read failed (error)", c) + panel("This Mac is offline (error)", d))


# --- 2. Proposals and holds. -----------------------------------------------------------------------------

def proposals_body(choice_open=False):
    props = ('<div class="mrows" role="list" aria-label="Proposals">'
             '<div class="mrow prop" role="listitem"><span class="name">Rule 1</span><span>Proposed by a Claude Code conversation in project-a, 25 min ago. '
             'In use now on top of your version 14; ends in 7 days unless you confirm it.</span><span class="end">' + tag("accent", "In use") + '</span>'
             '<div class="side"><div><span class="cap">Your words, version 14</span>Grok, GLM and DeepSeek never build or fix privacy, safety, sign-in or money code. '
             'Use at most <del>80%</del> of the xAI week.</div><div><span class="cap">Proposed words</span>Grok, GLM and DeepSeek never build or fix privacy, safety, sign-in or money code. '
             'Use at most <ins>76%</ins> of the xAI week.</div></div>'
             '<div class="acts">' + btn("Confirm", "sm primary", "Confirm the proposed rule 1") + btn("Edit, then confirm", label="Edit the proposed rule 1") + btn("Dismiss", label="Dismiss the proposed rule 1") + '</div></div>'
             '<div class="mrow prop" role="listitem"><span class="name">Priority 1</span><span>Proposed by a Codex conversation in project-b, 1 h ago, for Codex\'s role.</span><span class="end">' + tag("accent", "In use") + '</span>'
             '<div class="side"><div><span class="cap">Your words, version 9</span>Paced: new launches only while its week is at most <del>10</del> points ahead of an even pace.</div>'
             '<div><span class="cap">Proposed words</span>Paced: new launches only while its week is at most <ins>15</ins> points ahead of an even pace.</div></div>'
             '<div class="acts">' + btn("Confirm", "sm primary", "Confirm the proposed priority 1") + btn("Edit, then confirm", label="Edit the proposed priority 1") + btn("Dismiss", label="Dismiss the proposed priority 1") + '</div></div>'
             '<div class="mrow prop" role="listitem"><span class="name">Your note</span><span>Proposed by Grok Build in project-d against version 12. You changed the note since (version 13), '
             'so it never took effect. Grok Build was told.</span><span class="end">' + tag("warn", "Crossed") + '</span>'
             '<div class="acts">' + btn("Read it", label="Read the crossed note proposal") + btn("Dismiss", label="Dismiss the crossed note proposal") + '</div></div>'
             '<div class="mrow prop" role="listitem"><span class="name">Rule 6</span><span>You changed rule 6 on two Macs at once: this Mac at 1:02 PM and MacBook Air at 1:03 PM. '
             'The later one is in use; both Macs\' conversations were told.</span><span class="end">' + tag("", "Overtaken") + '</span>'
             '<div class="acts">' + btn("Compare", label="Compare the two versions of rule 6") + btn("Use the earlier one", label="Use the earlier rule 6") + '</div></div>'
             '</div>'
             '<p class="set-help">Ended today: 3 (2 replaced by newer proposals, 1 dismissed). ' + btn("Show", label="Show ended proposals") + '</p>')
    choice = ""
    if choice_open:
        choice = ('<div class="choice" role="group" aria-label="End or keep the hold on Grok">'
                  '<fieldset><legend>Your change names Grok, which you are holding until Fri 6:00 PM. End that hold or keep it?</legend>'
                  '<label><input type="radio" name="g1"> End the hold now</label>'
                  '<label><input type="radio" name="g1"> Keep the hold until Fri 6:00 PM</label></fieldset>'
                  '<span class="set-help" style="margin:0">Khipu never reads this from your words, so the change waits for your choice.</span>'
                  '<div class="toolbar">' + btn("Save change", "sm primary", dis=True) + btn("Cancel") + '</div></div>')
    holds = ('<div class="mrows" role="list" aria-label="Holds">'
             '<div class="mrow hold" role="listitem"><span class="name">Grok</span><span>Held by you until Fri 6:00 PM: “Keep Grok for Grok Bot this week.”<br><span class="src">Set Oct 20 at 9:14 AM · every Mac</span></span>'
             '<span class="end">' + btn("End now", label="End the hold on Grok now") + '</span>' + choice + '</div>'
             '<div class="mrow hold" role="listitem"><span class="name">Claude second</span><span>Held on MacBook Air only until 10:40 PM: the Mac is busy.<br><span class="src">Set by a Claude Code conversation in project-a · 24-hour default</span></span>'
             '<span class="end">' + btn("End now", label="End the hold on Claude second now") + '</span></div></div>'
             '<div class="toolbar">' + btn("Add a hold…") + '</div>')
    return (sec("Proposals from conversations", props, "",
                "A conversation's change is in use until you confirm or dismiss it.")
            + sec("Holds", holds)
            + '<div class="section-body">' + subfold("How long holds last", "Conversations: 24 h, at most 7 days") + '</div>')


def proposals_states():
    n, _, _ = CLOSED["proposals"]
    a = group("p1", n, "No proposals · no holds", "", sec("Proposals from conversations", empty("Nothing waiting for you",
              "When a conversation proposes a change to a rule, your note or your priority, it shows here beside your words."))
              + sec("Holds", empty("No holds", "Hold an account to keep it out of work for a while.", btn("Add a hold…"))))
    b = group("p2", n, "1 proposal on another Mac", tag("warn", "1 to confirm"),
              '<div class="mrows" role="list" aria-label="Proposals"><div class="mrow prop" role="listitem"><span class="name">Rule 3</span>'
              '<span>A pending rule proposal from MacBook Air. Its words stay on that Mac, so confirm it there. '
              'It is already in use everywhere.</span><span class="end">' + tag("accent", "In use") + '</span>'
              '<div class="acts">' + btn("Dismiss", label="Dismiss the rule 3 proposal") + '</div></div></div>'
              '<p class="set-help">Proposal words are kept on the Mac they came from because “Share proposal words between your Macs” is off in Telling conversations.</p>')
    c = group("p3", n, "2 holds", "",
              '<div class="callout warn" role="alert"><div class="txt"><b>Your priority wasn\'t saved yet</b>'
              '<span>It names Grok and Claude second, which are both held. Say for each one whether its hold ends or stays.</span></div></div>'
              '<div class="choice" role="group" aria-label="End or keep each hold"><fieldset><legend>Grok: held by you until Fri 6:00 PM</legend>'
              '<label><input type="radio" name="h1"> End the hold now</label><label><input type="radio" name="h1" checked> Keep it</label></fieldset>'
              '<fieldset><legend>Claude second: held on MacBook Air until 10:40 PM by a conversation</legend>'
              '<label><input type="radio" name="h2" checked> End the hold now</label><label><input type="radio" name="h2"> Keep it</label></fieldset>'
              '<div class="toolbar">' + btn("Save priority", "sm primary") + btn("Cancel") + '</div></div>')
    d = group("p4", n, '<span class="reading">Couldn\'t read</span>', tag("err", "Can't read"),
              err_box("Couldn't read the routing record: Khipu's routing command failed. Proposals and holds stay blank until the read works."))
    return (panel("No proposals and no holds (empty)", a) + panel("Proposal words on another Mac (unknown here)", b)
            + panel("A change that names held accounts: end or keep each", c) + panel("The read failed (error)", d))


# --- 3. Providers, job runners and lanes. ----------------------------------------------------------------

def prov(name, maker, how, models, accts, tg=""):
    return (f'<div class="mrow prov" role="listitem"><span class="name">{name}</span><span>{maker} · {how}<br>'
            f'<span class="src">{accts}{" · " + models if models else ""}</span></span><span class="end">{tg}{btn("Edit", label="Edit " + name)}</span></div>')


def lane(name, folder, acct, how, tg, acts=""):
    return (f'<div class="mrow lane" role="listitem"><span><span class="mono">{name}</span><br><span class="src">{folder}</span></span>'
            f'<span class="arrow" aria-hidden="true">→</span><span>{acct}</span><span class="src">{how}</span><span class="end">{tg}</span>{acts}</div>')


def lanes_body():
    p = ('<div class="mrows" role="list" aria-label="Providers">'
         + prov("Claude", "Anthropic", "plan read with the Claude command line", "", "2 accounts: Claude, Claude second")
         + prov("Codex", "OpenAI", "plan read with the Codex command line", "", "1 account")
         + prov("Grok", "xAI", "plan read with Grok's command line; it starts an agent, so every 30 min", "", "1 account")
         + prov("GLM", "Zhipu", "plan not read; job runner lines and reports only", "only glm-5.3 and glm-5.3-flash", "1 account")
         + prov("DeepSeek", "DeepSeek", "plan not read; job runner lines and reports only", "only deepseek-flash", "1 account")
         + prov("Muse", "Meta", "plan not read; reports only", "only muse-wonder-full-1.0", "1 account", tag("", "On request", False))
         + '</div><div class="toolbar">' + btn("Add a provider…") + '</div>')
    a = ('<div class="mrows" role="list" aria-label="Accounts and their Macs">'
         '<div class="mrow acct" role="listitem"><span class="name">Claude</span><span>This Mac and MacBook Air: one plan, counted once</span></div>'
         '<div class="mrow acct" role="listitem"><span class="name">Claude second</span><span>This Mac and Office mini</span></div>'
         '<div class="mrow acct" role="listitem"><span class="name">Codex</span><span>This Mac, MacBook Air and Office mini</span></div>'
         '<div class="mrow acct" role="listitem"><span class="name">Grok</span><span>This Mac</span></div>'
         '<div class="mrow acct" role="listitem"><span class="name">GLM, DeepSeek</span><span>Office mini, through the project-c job runner</span></div>'
         '<div class="mrow acct" role="listitem"><span class="name">Muse</span><span>No Mac signs in; known from reports</span></div></div>')
    cands = ('<div class="cands">Could be: ' + btn("Claude", label="Map lane claude to Claude") + btn("Claude second", label="Map lane claude to Claude second")
             + '</div>')
    none_ = ('<div class="cands"><span>Add an account line for this lane in the runner\'s settings, or map it here.</span>'
             + btn("Map to an account…", label="Map lane review-2 to an account") + '</div>')
    lanes_list = ('<div class="mrows" role="list" aria-label="Lanes">'
         + lane("claude", "project-a runner", "No account", "two accounts match the name", tag("warn", "Pick one"), cands)
         + lane("main-sonnet", "project-c runner", "Claude", "the runner says so; a job from it used Claude's login", tag("ok", "Confirmed"))
         + lane("secondary-haiku", "project-c runner", "Claude second", "the runner says so; no job has proved it yet", tag("", "Claimed"))
         + lane("codex", "project-c runner", "Codex", "matched by name", tag("", "By name"))
         + lane("glm", "project-c runner", "GLM", "matched by name", tag("", "By name"))
         + lane("deepseek", "project-c runner", "DeepSeek", "matched by name", tag("", "By name"))
         + lane("grok-review", "project-a runner", "Grok", "you mapped it", tag("", "Yours"))
         + lane("review-2", "project-a runner", "No account", "no name match and nothing declared", tag("warn", "No account"), none_)
         + '</div><p class="set-help">A lane without an account still runs; its limits just count toward no account.</p>')
    return (sec("Providers", p) + sec("Accounts and their Macs", a)
            + sec("Lanes", lanes_list, "9 lanes in 2 job runner folders")
            + '<div class="section-body">' + subfold("Job runner folders", "2 folders, both reporting") + '</div>')


def lanes_states():
    n, _, _ = CLOSED["lanes"]
    form = ('<form class="form" aria-label="Add a provider" onsubmit="return false">'
            '<label for="pn">Name</label><input id="pn" value="DeepSeek">'
            '<label for="pm">Made by</label><select id="pm"><option>DeepSeek</option></select>'
            '<span class="help">Used to check that a second opinion comes from a different maker.</span>'
            '<label for="ph">How Khipu reads its plan</label><select id="ph"><option>Not read: job runner lines and reports only</option><option>A usage address you give</option></select>'
            '<label for="pl">Lane names</label><input id="pl" value="deepseek">'
            '<label for="po">Models it may use</label><input id="po" value="deepseek-flash">'
            '<span class="help">Leave empty to allow any.</span>'
            '<label for="pu">Use</label><select id="pu"><option>Normal</option><option>Only when you ask</option></select>'
            '<div class="toolbar">' + btn("Add provider", "sm primary") + btn("Cancel") + '</div></form>')
    a = group("l1", n, "Adding a provider", "", form)
    b = group("l2", n, "No job runner folders", "",
              sec("Providers", '<div class="mrows" role="list" aria-label="Providers">'
                  + prov("Claude", "Anthropic", "plan read with the Claude command line", "", "1 account") + '</div>')
              + sec("Lanes", empty("No job runners yet", "Add a job runner folder and its lanes show here with the account each one uses.",
                                   btn("Add a folder…", "sm primary"))))
    c = group("l3", n, "9 lanes", tag("warn", "1 lane conflicts"),
              '<div class="mrows" role="list" aria-label="Lanes">'
              + lane("main-opus", "project-c runner", "No account", "the runner names Claude, but its login is Claude second's", tag("warn", "Conflict"),
                     '<div class="cands"><span>Fix the runner\'s settings, or pick the account here.</span>' + btn("Map to an account…", label="Map lane main-opus") + '</div>')
              + '</div>')
    d = group("l4", n, '<span class="reading">Couldn\'t read</span>', tag("err", "Can't read"),
              err_box("Couldn't read the provider list: Khipu's routing command failed. Nothing below is a guess."))
    return (panel("Adding a provider such as DeepSeek", a) + panel("No job runner folders (empty)", b)
            + panel("A lane whose declared account disagrees with its login (unknown)", c) + panel("The read failed (error)", d))


# --- 4. Checks and copies. -------------------------------------------------------------------------------

TIERS = [("Scopes and designs", "Opus"), ("Mocks", "Opus"), ("Rulings", "Opus"), ("Hardest security code", "Opus"),
         ("A fix that already failed", "Opus"), ("Builds", "Sonnet"), ("Fix rounds", "Sonnet"), ("Tests", "Sonnet"),
         ("Docs", "Sonnet"), ("Reports and summaries", "Sonnet"), ("Searches and lookups", "Haiku")]


def adv(title, sub, tg="", acts=""):
    a = f'<div class="acts">{acts}</div>' if acts else ""
    return f'<div class="mrow adv" role="listitem"><span>{title}</span><span class="end">{tg}</span><span class="sub">{sub}</span>{a}</div>'


def checks_body():
    mism = ('<div class="mrows" role="list" aria-label="Model mismatches today">'
            + adv("A fix round went to Opus; your table says Sonnet", "Claude Code in project-a, 1:12 PM · the brief said it was a fix round", tag("", "From the brief", False))
            + adv("Tests went to Opus; your table says Sonnet", "Claude Code in project-b, 11:40 AM · guessed from the brief's words", tag("", "Guess", False))
            + adv("A search went to Sonnet; your table says Haiku", "Codex in project-c, 9:05 AM · guessed from the brief's words", tag("", "Guess", False))
            + '</div>')
    contra = ('<div class="mrows" role="list" aria-label="Contradictions and out-of-date words">'
              + adv("Priority 2 names Grok, which you paused until Fri 6:00 PM", "Your priority and your pause can't both hold.", tag("warn", "Contradiction"),
                    btn("Open priority", label="Open priority 2") + btn("End the pause…", label="End the pause on Grok"))
              + adv("Your priority lists Antigravity, which Khipu doesn't track", "It can never be open or limited here.", tag("warn", "Contradiction"), btn("Open priority", label="Open the Antigravity entry"))
              + adv("Codex's role says “Codex is low”; Codex read 96% left 4 min ago", "A level with an end date stays true; words don't.", tag("", "Out of date"),
                    btn("Set a level instead…", label="Set a level for Codex"))
              + '</div>')
    upkeep = ('<div class="mrows" role="list" aria-label="Rule upkeep">'
              + adv("Rule 9 says “until Friday”", "A dated limit belongs on the account's priority, with an end date.", "",
                    btn("Move to Grok's priority…", label="Move rule 9 to Grok's priority"))
              + adv("Rules 3 and 6 both cover fast models", "Two rules on one account and topic.", "", btn("Compare", label="Compare rules 3 and 6"))
              + adv("A conversation's edit to rule 7 has waited 31 h", "It is in use until you confirm or drop it.", "",
                    btn("Confirm", label="Confirm the rule 7 edit") + btn("Drop", label="Drop the rule 7 edit"))
              + '</div>')
    copies = ('<div class="mrows" role="list" aria-label="Copies of routing found in files">'
              + adv('<span class="path">~/Projects/project-a/CLAUDE.md</span> line 42', '<p class="quote">“Main account tight until Mon 2026-10-19”</p>',
                    "")
              + adv('<span class="path">~/Projects/project-c/AGENTS.md</span> line 18', '<p class="quote">“Delegation order: second Claude account first”</p>',
                    "")
              + adv("A memory note in project-a", '<p class="quote">“Codex on credits, limited, until Oct 20”</p>', "")
              + adv('Runner settings in <span class="path">~/Projects/project-c/lanes.json</span>',
                    "Caps DeepSeek at $40 a week; Khipu's cap is $35.", tag("warn", "Differs"), btn("Open the cap", label="Open DeepSeek's cap"))
              + '</div><div class="toolbar">' + btn("Review a cleanup…") + '<span class="meta">Shows each change and keeps a backup; nothing is edited until you say so.</span></div>')
    said = ('<div class="mrows" role="list" aria-label="Said in a conversation, not in Khipu">'
            + adv("“GLM, one request at a time”", "You said this in a Claude Code conversation in project-c at 11:02 AM. Nothing in Khipu matches it.", "",
                  btn("Make it a cap…", "sm primary", "Make GLM one at a time a cap") + btn("Dismiss", label="Dismiss this direction"))
            + '</div>')
    return ('<p class="set-help">Khipu flags these so you can fix them. None of them stops any work.</p>'
            + sec("Model mismatches today", mism, "3")
            + '<div class="section-body">' + subfold("Your model table", "11 kinds of work · confirmed by you Oct 20") + '</div>'
            + sec("Contradictions and out-of-date words", contra)
            + sec("Rule upkeep", upkeep)
            + sec("Routing written in files", copies, "4 found", "Conversations are told Khipu's record wins over these.")
            + sec("Said in a conversation, not in Khipu", said, "1"))


def checks_states():
    n, _, _ = CLOSED["checks"]
    unconf = ('<div class="callout warn" role="region" aria-label="Confirm your model table"><div class="txt"><b>Confirm your model table</b>'
              '<span>Khipu filled it from your rule 12. Until you confirm it, Khipu doesn\'t compare handed-off jobs with it.</span></div></div>'
              + '<dl class="tiers">' + "".join(f'<div><dt>{a}</dt><dd>{b}</dd></div>' for a, b in TIERS) + '</dl>'
              + '<div class="toolbar">' + btn("Confirm table", "sm primary") + btn("Edit first…") + '</div>')
    a = group("c1", n, "Model table not confirmed", tag("warn", "1 to confirm"), unconf)
    b = group("c2", n, "Nothing found today", "",
              empty("Nothing to fix", "Every handed-off job today matched your model table, and no rule or file disagrees with Khipu.")
              + '<p class="set-help">Files last looked at 2:10 PM: 14 files, 0 copies.</p>')
    c = group("c3", n, "Files not looked at yet", tag("", "Not checked"),
              sec("Routing written in files", empty("Khipu hasn't looked at your files yet",
                  "Nothing is known about copies until the first look. It reads only, and never edits a file.", btn("Look now", "sm primary"))))
    d = group("c4", n, '<span class="reading">Couldn\'t read</span>', tag("err", "Can't read"),
              err_box("Couldn't read the checks: Khipu's routing command failed. That is not the same as nothing found."))
    return (panel("Model table waiting for your confirmation", a) + panel("Nothing found (empty)", b)
            + panel("Files never looked at (unknown)", c) + panel("The read failed (error)", d))


# --- 5. Pace, caps and advice. ---------------------------------------------------------------------------

def spark(points, need=None, cap=None, gaps=(), label=""):
    """points: used % at each hour over the last 24 h (None = no reading). need: rate per hour from the first point."""
    vals = [p for p in points if p is not None]
    extra = [cap] if cap is not None else []
    if need is not None:
        extra.append(vals[0] + need * (len(points) - 1))
    lo = min(vals + extra) - 2
    hi = max(vals + extra) + 2
    W, H = 120, 28
    n = len(points) - 1

    def X(i):
        return round(i * W / n, 1)

    def Y(v):
        return round(H - (v - lo) * H / (hi - lo), 1)

    segs, cur = [], []
    for i, p in enumerate(points):
        if p is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(f"{X(i)},{Y(p)}")
    if cur:
        segs.append(cur)
    out = [f'<svg class="trend" viewBox="0 0 {W} {H}" role="img" aria-label="{label}" preserveAspectRatio="none">']
    for a, b in gaps:
        out.append(f'<rect class="gap" x="{X(a)}" y="0" width="{X(b) - X(a)}" height="{H}"/>')
    if cap is not None:
        out.append(f'<line class="cap" x1="0" x2="{W}" y1="{Y(cap)}" y2="{Y(cap)}"/>')
    if need is not None:
        out.append(f'<line class="need" x1="0" y1="{Y(vals[0])}" x2="{W}" y2="{Y(vals[0] + need * n)}"/>')
    for s in segs:
        out.append(f'<polyline class="used" points="{" ".join(s)}"/>')
    out.append("</svg>")
    return "".join(out)


def ramp(start, rate, n=25, flat=()):
    v, pts = start, []
    for i in range(n):
        pts.append(round(v, 2))
        if i not in flat:
            v += rate
    return pts


def win(name, pct, tone, r1, r2, trend):
    meter = (f'<span class="plan-meter{" " + tone if tone else ""}" role="progressbar" aria-label="{name} used" aria-valuemin="0" '
             f'aria-valuemax="100" aria-valuenow="{pct}"><i style="width:{pct}%"></i></span>' if pct is not None else '<span></span>')
    p = f'<span class="plan-pct{" " + tone if tone else ""}">{pct}%</span>' if pct is not None else "<span></span>"
    return f'<li class="pace-win"><span class="wname">{name}</span>{meter}{p}<span class="pace-rate"><span class="r1">{r1}</span><span class="r2">{r2}</span></span>{trend}</li>'


def cap_row(name, state, tg):
    return f'<div class="cap-row"><span class="cname">{name}</span><span class="cstate">{state}</span><span class="end">{tg}</span></div>'


def adv_row(who, word, why):
    cls = {"Go": "go", "Hold back": "hold", "Closed": "closed"}[word]
    return f'<div class="adv-row"><span class="who">{who}</span><span class="word {cls}">{word}</span><span class="why">{why}</span></div>'


def block(name, plan, head_tags, say, wins, caps, advice, evid, tone=""):
    c = f'<div class="pace-sec"><h4>Your caps <span class="evid">· confirmed by you Oct 20</span></h4>{caps}</div>' if caps else ""
    return (f'<div class="plan-block pace-block{" " + tone if tone else ""}" role="group" aria-label="{name} pace">'
            f'<h3 class="plan-block-head">{name} <span class="meta">{plan}</span><span class="spacer"></span>{head_tags}</h3>'
            f'<p class="pace-say">{say}</p><ul class="pace-wins" aria-label="{name} windows">{wins}</ul>{c}'
            f'<div class="pace-sec"><h4>Khipu\'s advice <span class="evid">· {evid}</span></h4>{advice}</div></div>')


CONF = ""


def pace_blocks():
    claude = block(
        "Claude", "Max plan", tag("warn", "Outpacing"),
        "Reaches your five-hour 60% at <b>4:40 PM</b>, 30 min before that window resets. At the week's rate it would run out Sun 2:46 AM, 1 d 8 h before the reset.",
        win("Five hours", 42, "", "9% an hour · " + tag("warn", "Outpacing your 60%", False), "7.2% an hour lands on 60% at the 5:10 PM reset",
            spark(ramp(33, 0.375), need=0.3, cap=60, label="Five hours: used rose from 33% to 42% in the last hour; your 60% cap line above")) +
        win("Week", 31, "", "0.82% an hour · " + tag("warn", "Outpacing", False), "0.59% an hour lands on 100% at the reset Mon 11:00 AM",
            spark(ramp(11.3, 0.82), need=0.59, label="Week: used rose from 11% to 31% over the last 24 hours, faster than the dashed rate that lands on the limit")),
        cap_row("Five hours: at most 60%", "42% now · reaches it 4:40 PM", CONF)
        + cap_row("Week: at most 91%", "31% now · reaches it Sat 3:52 PM", CONF)
        + cap_row("Week: even pace to 65%, 5 points slack", "Allowed 25% by now, used 31% · " + tag("warn", "6 points ahead", False), CONF)
        + cap_row("Jobs: at most 13% a day", "11.4% in the last 24 h · reaches 13% at 5:10 PM", CONF),
        adv_row("Jobs", "Hold back", "6 points ahead of your even pace to 65%; near its 13% a day.")
        + adv_row("Lead conversations", "Go", "Your even pace and daily cap apply only to jobs. Under 60% and 91%."),
        "readings fresh, 3 min ago", "warn")
    second = block(
        "Claude second", "Max 20x plan", tag("", "Underpacing"),
        "About 48 points of the week will go unused at the reset at this rate.",
        win("Five hours", 0, "", "No use in the last hour", "Resets 6:30 PM", spark([0] * 13, label="Five hours: no use in the last hour")) +
        win("Week", 22, "", "0.41% an hour · Underpacing", "1.08% an hour lands on 100% at the reset Sat 3:00 PM",
            spark(ramp(12.2, 0.41), need=1.08, label="Week: used rose from 12% to 22% over the last 24 hours, slower than the dashed rate that lands on the limit")),
        "",
        adv_row("Jobs", "Go", "Room to spare.") + adv_row("Lead conversations", "Go", "Room to spare."),
        "readings fresh, 3 min ago")
    codex = block(
        "Codex", "Pro plan", tag("warn", "Ahead of your pace"),
        "On pace for the plan limit, but 1.7 points ahead of the even pace you set.",
        win("Five hours", 12, "", "6% an hour · Underpacing", "Resets 4:57 PM", spark(ramp(6, 0.5, 13), label="Five hours: used rose from 6% to 12% in the last hour")) +
        win("Week", 46, "", "0.55% an hour · On pace", "0.49% an hour lands on 100% at the reset Mon 4:57 AM",
            spark(ramp(32.8, 0.55), need=0.49, label="Week: used rose from 33% to 46% over the last 24 hours, close to the dashed rate that lands on the limit")),
        cap_row("Week: no more than 10 points ahead of even", "Allowed 44.3% by now, used 46% · " + tag("warn", "1.7 points ahead", False), CONF),
        adv_row("Jobs", "Hold back", "1.7 points ahead of even plus 10. Clears when the allowance reaches 47%, about 4.5 h from now if no more is used.")
        + adv_row("Lead conversations", "Go", "Your pace limit applies only to jobs."),
        "readings fresh, 4 min ago", "warn")
    grok = block(
        "Grok", "xAI Ultra, shared with Grok Bot and Cursor", tag("warn", "Outpacing your 76%"),
        "Reaches your 76% Fri 6:10 AM, 15 h before the reset. Under the plan limit at this rate.",
        win("Five hours", None, "", "Can't tell", "Grok is checked every 30 min, too seldom for a five-hour pace", '<span class="trend-none">no trend</span>') +
        win("Week", 61, "warn", "0.38% an hour · " + tag("warn", "Outpacing your 76%", False), "0.28% an hour lands on 76% at the reset Fri 9:00 PM",
            spark(ramp(51.9, 0.38), need=0.28, cap=76, label="Week: used rose from 52% to 61% over the last 24 hours, faster than the dashed rate that lands on your 76% cap")),
        cap_row("Week: at most 76%", "61% now · reaches it Fri 6:10 AM", CONF),
        adv_row("Jobs", "Go", "Reaches 76% in 39 h. Khipu says hold back when a cap is less than an hour away.")
        + adv_row("Lead conversations", "Go", "Same."),
        "readings fresh, 12 min ago")
    glm = block(
        "GLM", "Coding plan", tag("warn", "1 of 1 running"),
        "Khipu doesn't read GLM's plan, so there is no pace. It counts the jobs its runners report.",
        "",
        cap_row("One job at a time", "1 running, from the project-c runner, 1 min ago", CONF),
        adv_row("Jobs", "Hold back", "1 of 1 running.") + adv_row("Lead conversations", "Go", "The one-at-a-time cap counts runner jobs only."),
        "running count fresh, 1 min ago")
    deep = block(
        "DeepSeek", "Pay as you go", tag("", "Spend can't be told"),
        "$21.40 reported this week. The project-c runner has a 3 h gap on Tue, so the total isn't known.",
        "",
        cap_row("At most $35 a rolling 7 days", "$21.40 reported · gap Tue 3:00 to 6:00 AM", CONF),
        adv_row("Jobs", "Go", "Khipu can't tell the spend, so it says go and marks the evidence unknown. Your runner decides what to do with that.")
        + adv_row("Lead conversations", "Go", "Same."),
        "evidence unknown: spend gap")
    muse = block(
        "Muse", "Power plan", tag("err", "Limit reached"),
        "At its weekly limit, back Mon 12:00 AM. Reported by a Claude Code conversation in project-a.",
        "", "",
        adv_row("Jobs", "Closed", "Limit reached until Mon 12:00 AM.") + adv_row("Lead conversations", "Closed", "Same."),
        "reported 45 min ago")
    return [("Claude", claude), ("Codex", codex), ("Grok", grok), ("Claude second", second), ("GLM", glm), ("DeepSeek", deep), ("Muse", muse)]


PACE_LINES = {
    "Codex": (tag("warn", "Ahead of your pace"), "1.7 points ahead of your even pace", "Jobs: hold back"),
    "Grok": (tag("warn", "Outpacing your 76%"), "Reaches 76% Fri 6:10 AM", "Jobs: go"),
    "Claude second": (tag("", "Underpacing"), "About 48 points unused at the reset", "Jobs: go"),
    "GLM": (tag("warn", "1 of 1 running"), "No pace: plan not read", "Jobs: hold back"),
    "DeepSeek": (tag("", "Spend can't be told"), "$21.40 reported; a 3 h gap", "Jobs: go, unsure"),
    "Muse": (tag("err", "Limit reached"), "Back Mon 12:00 AM", "Closed to all"),
}


def pace_line(name):
    tg, say, advice = PACE_LINES[name]
    return (f'<div role="listitem"><div class="fold disclose route-aline"><button type="button" class="disclosure fold-btn" aria-expanded="false">'
            f'{CHEV}<span class="an">{name}</span><span class="as">{tg}</span><span class="ap"><span class="ap-week">{say}</span></span>'
            f'<span class="at"><span class="ap-pace">{advice}</span></span></button></div></div>')


def pace_body(all_open=False):
    legend = ('<div class="legend" aria-hidden="true"><span><i></i>used, last 24 h (last hour for five hours)</span>'
              '<span><i class="need"></i>rate that lands on the limit</span><span><i class="cap"></i>your cap</span></div>')
    return ('<p class="set-help">How fast each account is being used against its limit and your caps, at the last 24 hours\' rate '
            '(the last hour\'s for five-hour windows). Khipu\'s advice is for your agents and job runners; it starts and stops nothing.</p>'
            + legend + (('<div class="pace-blocks">' + "".join(b for _, b in pace_blocks()) + '</div>') if all_open else
                        ('<div class="pace-blocks">' + pace_blocks()[0][1] + '</div><div class="route-alist" role="list" aria-label="Other accounts">'
                         + "".join(pace_line(n) for n, _ in pace_blocks()[1:]) + '</div>'))
            + '<div class="toolbar">' + btn("Add a cap…") + '</div>'
            + '<div class="section-body">' + subfold("When pace changes are sent to conversations", "At a verdict change, and 24 h, 6 h, 1 h and 15 min before a limit") + '</div>')


def pace_states():
    n, _, _ = CLOSED["pace"]
    unk = (block("Claude", "Max plan", tag("", "Can't tell yet"),
                 "Usage dropped from 95% to 1% between two readings. That may be a reset; waiting for the next reading.",
                 win("Week", 1, "", "Can't tell", "Possible reset; waiting for the next reading",
                     spark([90, 91, 92, 93, 94, 95, 1], label="Week: usage dropped from 95% to 1%; waiting to see if it reset")),
                 "", adv_row("Jobs", "Go", "Can't tell, so Khipu says go with the evidence marked unknown.") + adv_row("Lead conversations", "Go", "Same."),
                 "evidence unknown: possible reset")
           + block("Codex", "Pro plan", tag("", "Can't tell yet"),
                   "Codex's reset time spreads over more than an hour between readings.",
                   win("Week", 46, "", "Can't tell", "Reset time disagrees between readings", '<span class="trend-none">no trend</span>'),
                   "", adv_row("Jobs", "Go", "Evidence unknown.") + adv_row("Lead conversations", "Go", "Same."), "evidence unknown: reset time")
           + block("Claude second", "Max 20x plan", tag("", "Can't tell yet"),
                   "Readings cover only 9 of the last 24 h, so there's no rate yet.",
                   win("Week", 22, "", "Can't tell", "Readings cover only 9 of the last 24 h",
                       spark([12, 12.3, None, None, None, None, None, None, None, None, None, None, None, None, None, None, 18, 18.6, 19.4, 20.1, 20.8, 21.4, 21.8, 22, 22],
                             gaps=[(2, 16)], label="Week: readings missing for 14 of the last 24 hours")),
                   "", adv_row("Jobs", "Go", "Evidence unknown.") + adv_row("Lead conversations", "Go", "Same."), "evidence unknown: gappy readings")
           + block("GLM", "Coding plan", tag("", "Can't tell yet"),
                   "The project-c runner stopped reporting 25 min ago, so the running count has expired.",
                   "", cap_row("One job at a time", "Not known: the runner's last report has expired", CONF),
                   adv_row("Jobs", "Go", "Evidence unknown.") + adv_row("Lead conversations", "Go", "Same."), "evidence old: 25 min"))
    a = group("u1", n, "4 accounts can't be told yet", tag("", "4 can't be told"), '<div class="pace-blocks">' + unk + '</div>')
    sugg = ('<div class="callout warn" role="region" aria-label="A cap found in your words"><div class="txt"><b>A cap found in your words</b>'
            '<span>Your priority for Claude says “13% a day for Claude jobs”. Make it a cap? Khipu never changes your words.</span>'
            '<div class="toolbar" style="margin-top:var(--s2)">' + btn("Make it a cap: 13% of the week per rolling day, jobs only", "sm primary") + btn("Not a cap") + '</div></div></div>')
    form = ('<form class="form" aria-label="Add a cap" onsubmit="return false">'
            '<label for="ca">Account</label><select id="ca"><option>Claude</option></select>'
            '<label for="ck">Kind</label><select id="ck"><option>A share of a window</option><option>Per rolling day</option>'
            '<option>An even pace to a target</option><option>Spend over some days</option><option>Jobs at once</option></select>'
            '<label for="cw">Window</label><select id="cw"><option>Week</option><option>Five hours</option></select>'
            '<label for="cp">At most</label><input id="cp" value="91%">'
            '<span class="help">Khipu says hold back once this is reached, or less than an hour away.</span>'
            '<div class="toolbar">' + btn("Add cap", "sm primary") + btn("Cancel") + '</div></form>')
    b = group("u2", n, "1 cap to confirm", tag("warn", "1 to confirm"), sugg + form)
    c = group("u3", n, "No readings yet", "",
              empty("No usage readings yet", "Pace needs at least 3 readings in a day. Khipu reads plans every 30 min for a week, every 10 min for five hours.",
                    btn("Check plans now", "sm primary")))
    d = group("u4", n, '<span class="reading">Couldn\'t read</span>', tag("err", "Can't read"),
              err_box("Couldn't work out pace: the usage readings didn't open. Advice falls back to go with the evidence unknown; "
                      "nothing below is a guess."))
    return (panel("Not enough evidence (unknown)", a) + panel("A cap found in your words, and adding one", b)
            + panel("No readings yet (empty)", c) + panel("The read failed (error)", d))


# --- Screens. ---------------------------------------------------------------------------------------------

SCREENS = {
    "overview": ("Accounts and routing, every group closed", lambda: page()),
    "delivery": ("Telling conversations: delivery and health", lambda: page("telling", delivery_body())),
    "delivery-states": ("Telling conversations: states", lambda: top() + delivery_states()),
    "proposals": ("Proposals and holds", lambda: page("proposals", proposals_body())),
    "proposals-states": ("Proposals and holds: states", lambda: top() + proposals_states()),
    "lanes": ("Providers and lanes", lambda: page("lanes", lanes_body())),
    "lanes-states": ("Providers, job runners and lanes: states", lambda: top() + lanes_states()),
    "checks": ("Checks and copies", lambda: page("checks", checks_body())),
    "checks-states": ("Checks and copies: states", lambda: top() + checks_states()),
    "pace": ("Pace, caps and advice", lambda: page("pace", pace_body())),
    "pace-all": ("Pace, caps and advice, every account open", lambda: page("pace", pace_body(True))),
    "pace-states": ("Pace, caps and advice: states", lambda: top() + pace_states()),
}


def main():
    build_css()
    for key, (title, fn) in SCREENS.items():
        pane = fn()
        for theme in ("light", "dark"):
            open(os.path.join(D, f"{key}-{theme}.html"), "w", encoding="utf-8").write(shell(title, pane, theme))
    print(f"wrote {len(SCREENS) * 2} screens")


if __name__ == "__main__":
    main()
