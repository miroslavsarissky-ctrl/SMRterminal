#!/usr/bin/env python3
"""
digest.py :: newcleo Nuclear Intel Terminal, morning digest.

Composes a daily briefing from the terminal's own data files and sends
it via SMTP email and/or a Slack webhook, whichever secrets are set.

Sections
--------
1. NWCL deal desk: NHIC price, premium to trust when configured, and
   filings not yet reported in a previous digest.
2. New on the verified layer since the last digest, best first.
3. Deadline radar: open deadlines within DIGEST_DEADLINE_DAYS, plus any
   hand-curated milestones from deal_config.js.
4. Market moves: tickers whose daily change exceeds DIGEST_MOVE_PCT.

State (terminal/data/digest_state.json) prevents repeats and advances
only after at least one transport succeeds, so a failed send retries
the same content next run. Each digest is also archived to
terminal/data/digest_latest.md.

Environment
-----------
SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / DIGEST_TO   email
SLACK_WEBHOOK_URL                                           slack
DIGEST_MOVE_PCT       default 5 (percent, absolute daily move)
DIGEST_DEADLINE_DAYS  default 10
TERMINAL_URL          link in the footer
Stdlib only. No pip dependencies.
"""

import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Lives at terminal/collectors/digest.py, so parent.parent is terminal/.
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE_PATH = DATA / "digest_state.json"
ARCHIVE_PATH = DATA / "digest_latest.md"

MOVE_PCT = float(os.environ.get("DIGEST_MOVE_PCT", "5"))
DEADLINE_DAYS = int(os.environ.get("DIGEST_DEADLINE_DAYS", "10"))
TERMINAL_URL = os.environ.get(
    "TERMINAL_URL",
    "https://miroslavsarissky-ctrl.github.io/SMRterminal/terminal/terminal.html")
MAX_VERIFIED = 12


def log(msg):
    print("[digest] " + msg, flush=True)


def load_js(name):
    p = DATA / name
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8")
    try:
        return json.loads(raw[raw.index("{"):].rstrip().rstrip(";"))
    except (ValueError, json.JSONDecodeError):
        return None


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    return {"last_sent": "1970-01-01T00:00:00+00:00", "seen_filings": []}


# ------------------------------------------------------------- content
def gather():
    feed = load_js("feed.js") or {"items": []}
    verified = load_js("verified.js") or {"items": {}}
    quotes = load_js("quotes.js") or {"tickers": {}}
    deal = load_js("deal.js") or {"filings": []}
    cfg = load_js("deal_config.js") or {}
    state = load_state()

    by_id = {i["id"]: i for i in feed.get("items", [])}
    now = datetime.now(timezone.utc)
    today = now.date()

    # 1. deal desk
    tick = cfg.get("ticker") or "NHIC"
    q = (quotes.get("tickers") or {}).get(tick)
    seen = set(state.get("seen_filings", []))
    new_filings = []
    for f in deal.get("filings", []):
        key = f["form"] + "|" + f["date"] + "|" + f["url"]
        if key not in seen:
            new_filings.append(f)
    trust = cfg.get("trust_ps")
    prem = (round((q["last"] / trust - 1) * 100, 1)
            if (q and trust) else None)

    # 2. new verified items
    last_sent = state.get("last_sent", "1970-01-01T00:00:00+00:00")
    fresh = []
    for iid, v in (verified.get("items") or {}).items():
        if v.get("at", "") > last_sent:
            it = by_id.get(iid)
            if it:
                fresh.append({"score": v.get("score", 0),
                              "why": v.get("why", ""),
                              "at": v.get("at", ""),
                              "title": it["title"], "url": it["url"],
                              "source": it.get("source", "")})
    fresh.sort(key=lambda x: (-x["score"], x["at"]), reverse=False)
    # ADAMS emits several records per document (pdf, html, transmittal
    # email); collapse them on normalised title, best score kept first.
    deduped, seen_titles = [], set()
    for v in sorted(fresh, key=lambda x: (-x["score"], x["at"])):
        key = v["title"].lower()
        if key.startswith("email - "):
            key = key[8:]
        key = key.strip(" .")
        contained = key in seen_titles or (len(key) >= 25 and any(
            key in s or s in key for s in seen_titles))
        if contained:
            continue
        seen_titles.add(key)
        deduped.append(v)
    fresh = deduped[:MAX_VERIFIED]

    # 3. deadlines: merged radar (feed + programmes + events + gates)
    prog = load_js("programmes.js") or {"items": []}
    conf = load_js("conferences.js") or {"items": []}
    internal = load_js("internal.js") or {"items": []}

    def days_to(s):
        m = re.search(r"\d{4}-\d{2}-\d{2}", str(s or ""))
        if not m:
            return None, None
        try:
            dd = (datetime.strptime(m.group(0), "%Y-%m-%d").date()
                  - today).days
        except ValueError:
            return None, None
        return (dd, m.group(0)) if 0 <= dd <= DEADLINE_DAYS else (None, None)

    def clean_url(s):
        u = str(s or "").split(" ")[0]
        return u if u.startswith("http") else ""

    dls = []
    for i in feed.get("items", []):
        dd, dt = days_to(i.get("deadline"))
        if dd is not None:
            dls.append({"d": dd, "date": dt,
                        "label": i.get("deadline_label") or "Deadline",
                        "title": i["title"], "url": i["url"]})
    for p in prog.get("items", []):
        dd, dt = days_to(p.get("deadline"))
        if dd is not None:
            dls.append({"d": dd, "date": dt, "label": "Programme",
                        "title": p.get("name", ""),
                        "url": clean_url(p.get("link"))})
    for cf in conf.get("items", []):
        dd, dt = days_to(cf.get("deadline"))
        if dd is not None:
            dls.append({"d": dd, "date": dt, "label": "Event",
                        "title": cf.get("name", ""),
                        "url": clean_url(cf.get("link"))})
    for g in internal.get("items", []):
        dd, dt = days_to(g.get("date"))
        if dd is not None:
            dls.append({"d": dd, "date": dt, "label": "Gate",
                        "title": g.get("label", "Internal gate"),
                        "url": clean_url(g.get("url"))})
    for m in (cfg.get("milestones") or []):
        dd, dt = days_to(m.get("date"))
        if dd is not None:
            dls.append({"d": dd, "date": dt, "label": "Milestone",
                        "title": m.get("label", "Milestone"), "url": ""})
    dls.sort(key=lambda x: x["d"])

    # 4. market moves
    moves = []
    for t, tq in (quotes.get("tickers") or {}).items():
        if tq.get("d1") is not None and abs(tq["d1"]) >= MOVE_PCT:
            moves.append({"t": t, "last": tq["last"], "d1": tq["d1"]})
    moves.sort(key=lambda x: -abs(x["d1"]))

    return {"q": q, "tick": tick, "prem": prem, "trust": trust,
            "new_filings": new_filings, "fresh": fresh, "dls": dls,
            "moves": moves, "state": state, "now": now}


# ------------------------------------------------------------ renderers
def pct(v, signed=True):
    if v is None:
        return "n/a"
    s = "%+.1f%%" % v if signed else "%.1f%%" % v
    return s


def build_text(c):
    day = c["now"].strftime("%a %d %b %Y")
    L = ["NUCLEAR INTEL DIGEST  " + day, ""]

    L.append("NWCL DEAL DESK")
    if c["q"]:
        line = "%s %.2f  1d %s  7d %s  30d %s" % (
            c["tick"], c["q"]["last"], pct(c["q"]["d1"]),
            pct(c["q"].get("d7")), pct(c["q"].get("d30")))
        if c["prem"] is not None:
            line += "  |  %s vs trust $%.2f" % (pct(c["prem"]), c["trust"])
        L.append(line)
    if c["new_filings"]:
        for f in c["new_filings"][:6]:
            L.append("  NEW %s  %s  %s" % (
                f["form"], f["date"], f.get("ai_t") or "filing"))
            if f.get("ai_s"):
                L.append("      %s" % f["ai_s"])
            L.append("      %s" % f["url"])
    else:
        L.append("  No new SEC filings.")
    L.append("")

    L.append("NEW ON THE VERIFIED LAYER (%d)" % len(c["fresh"]))
    if c["fresh"]:
        for v in c["fresh"]:
            L.append("  [%d] %s" % (v["score"], v["title"]))
            if v["why"]:
                L.append("      %s" % v["why"])
            L.append("      %s | %s" % (v["source"], v["url"]))
    else:
        L.append("  Nothing new promoted since the last digest.")
    L.append("")

    L.append("DEADLINE RADAR (next %d days)" % DEADLINE_DAYS)
    if c["dls"]:
        for d in c["dls"][:8]:
            L.append("  D-%d  %s  %s: %s" % (
                d["d"], d["date"], d["label"], d["title"][:80]))
            if d["url"]:
                L.append("        %s" % d["url"])
    else:
        L.append("  No deadlines inside the window.")
    L.append("")

    L.append("MARKET MOVES (over %.0f%%)" % MOVE_PCT)
    if c["moves"]:
        for m in c["moves"]:
            L.append("  %-5s %8.2f  %s" % (m["t"], m["last"], pct(m["d1"])))
    else:
        L.append("  No watchlist ticker moved more than %.0f%%." % MOVE_PCT)
    L.append("")
    L.append("Terminal: " + TERMINAL_URL)
    return "\n".join(L)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;")


def build_html(c):
    day = c["now"].strftime("%A %d %B %Y")
    up, dn, mut = "#1a7a4f", "#b3362c", "#6b7a82"

    def h2(t):
        return ('<h2 style="font:700 12px monospace;letter-spacing:.14em;'
                'color:#025152;margin:22px 0 8px">' + t + "</h2>")

    o = ['<div style="font-family:Georgia,serif;max-width:640px;color:#222">']
    o.append('<p style="font:700 15px monospace;color:#025152;margin:0">'
             'NUCLEAR INTEL DIGEST</p>')
    o.append('<p style="font:12px monospace;color:%s;margin:2px 0 0">%s'
             '</p>' % (mut, day))

    o.append(h2("NWCL DEAL DESK"))
    if c["q"]:
        col = up if (c["q"]["d1"] or 0) >= 0 else dn
        line = ('<b>%s %.2f</b> <span style="color:%s">%s</span>'
                ' <span style="color:%s">7d %s &#183; 30d %s</span>'
                % (c["tick"], c["q"]["last"], col, pct(c["q"]["d1"]),
                   mut, pct(c["q"].get("d7")), pct(c["q"].get("d30"))))
        if c["prem"] is not None:
            line += (' <span style="color:%s">&#183; %s vs trust $%.2f'
                     '</span>' % (mut, pct(c["prem"]), c["trust"]))
        o.append("<p>" + line + "</p>")
    if c["new_filings"]:
        o.append("<ul style='padding-left:18px'>")
        for f in c["new_filings"][:6]:
            o.append('<li><b>%s</b> %s: %s<br>'
                     % (esc(f["form"]), f["date"],
                        esc(f.get("ai_t") or "filing")))
            if f.get("ai_s"):
                o.append('<span style="color:#00868a">%s</span><br>'
                         % esc(f["ai_s"]))
            o.append('<a href="%s" style="font:11px monospace">'
                     'document</a></li>' % esc(f["url"]))
        o.append("</ul>")
    else:
        o.append('<p style="color:%s">No new SEC filings.</p>' % mut)

    o.append(h2("NEW ON THE VERIFIED LAYER (%d)" % len(c["fresh"])))
    if c["fresh"]:
        for v in c["fresh"]:
            o.append('<p style="margin:8px 0"><b>[%d]</b> '
                     '<a href="%s" style="color:#025152">%s</a><br>'
                     % (v["score"], esc(v["url"]), esc(v["title"])))
            if v["why"]:
                o.append('<span style="color:#00868a">%s</span><br>'
                         % esc(v["why"]))
            o.append('<span style="font:11px monospace;color:%s">%s'
                     '</span></p>' % (mut, esc(v["source"])))
    else:
        o.append('<p style="color:%s">Nothing new promoted since the last '
                 'digest.</p>' % mut)

    o.append(h2("DEADLINE RADAR (next %d days)" % DEADLINE_DAYS))
    if c["dls"]:
        for d in c["dls"][:8]:
            t = esc(d["title"][:90])
            if d["url"]:
                t = '<a href="%s" style="color:#222">%s</a>' % (
                    esc(d["url"]), t)
            o.append('<p style="margin:5px 0"><b style="color:#F0782E">'
                     'D-%d</b> <span style="font:11px monospace;color:%s">'
                     '%s</span> %s: %s</p>'
                     % (d["d"], mut, d["date"], esc(d["label"]), t))
    else:
        o.append('<p style="color:%s">No deadlines inside the window.</p>'
                 % mut)

    o.append(h2("MARKET MOVES (over %.0f%%)" % MOVE_PCT))
    if c["moves"]:
        for m in c["moves"]:
            col = up if m["d1"] >= 0 else dn
            o.append('<p style="font:13px monospace;margin:3px 0">'
                     '%-5s %.2f <span style="color:%s">%s</span></p>'
                     % (m["t"], m["last"], col, pct(m["d1"])))
    else:
        o.append('<p style="color:%s">No watchlist ticker moved more than '
                 '%.0f%%.</p>' % (mut, MOVE_PCT))

    o.append('<p style="margin-top:26px;font:11px monospace">'
             '<a href="%s" style="color:#025152">Open the terminal</a></p>'
             % TERMINAL_URL)
    o.append("</div>")
    return "".join(o)


# ------------------------------------------------------------ transports
def send_email(subject, text, html):
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    to = [a.strip() for a in os.environ.get("DIGEST_TO", "").split(",")
          if a.strip()]
    if not (host and user and pw and to):
        return None
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.starttls()
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())
        s.quit()
        log("email sent to " + ", ".join(to))
        return True
    except Exception as e:
        log("email FAILED: " + repr(e)[:120])
        return False


def send_slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return None
    try:
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        log("slack sent")
        return True
    except Exception as e:
        log("slack FAILED: " + repr(e)[:120])
        return False


# ------------------------------------------------------------------ main
def main():
    c = gather()
    subject = "Nuclear intel digest %s" % c["now"].strftime("%a %d %b")
    text = build_text(c)
    html = build_html(c)

    ARCHIVE_PATH.write_text(text + "\n", encoding="utf-8")
    print("\n" + text + "\n")

    results = [r for r in (send_email(subject, text, html),
                           send_slack(text)) if r is not None]
    if not results:
        log("no transport configured (set SMTP_* + DIGEST_TO and/or "
            "SLACK_WEBHOOK_URL); state not advanced")
        return
    if not any(results):
        log("ERROR: all configured transports failed; state not advanced")
        sys.exit(1)

    state = c["state"]
    state["last_sent"] = c["now"].isoformat(timespec="seconds")
    seen = set(state.get("seen_filings", []))
    for f in c["new_filings"]:
        seen.add(f["form"] + "|" + f["date"] + "|" + f["url"])
    state["seen_filings"] = sorted(seen)[-60:]
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log("state advanced to " + state["last_sent"])


if __name__ == "__main__":
    main()
