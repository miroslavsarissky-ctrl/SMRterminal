#!/usr/bin/env python3
"""
deal.py :: newcleo Nuclear Intel Terminal, NWCL deal monitor data.

Fetches the SEC EDGAR submission history for the de-SPAC vehicle
(NewHold Investment Corp. III, NHIC, becoming NWCL at close) and writes
the deal sidecar (terminal/data/deal.js) with the latest deal-relevant
filings, each linked to its primary document.

Design notes
------------
* Source is the official SEC submissions API (data.sec.gov). The SEC
  requires a declared User-Agent containing a contact address; set
  CONTACT below to a real one you own.
* Fail-soft: this runs in the same workflow as quotes.py and must never
  break it. On any fetch failure it keeps the previous deal.js and
  exits zero with a warning.
* Hand-curated deal facts (trust value per share, vote and closing
  milestones) live separately in data/deal_config.js so this collector
  never overwrites them. Add the newcleo holdco CIK to CIKS when the
  S-4 registrant number is known.
* Stdlib only. No pip dependencies.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Lives at terminal/collectors/deal.py, so parent.parent is terminal/.
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "deal.js"

CONTACT = "miroslav.sarissky@newcleo.com"   # set to a real contact you own
UA = {"User-Agent": "newcleo-SMRterminal/1.0 (%s)" % CONTACT,
      "Accept": "application/json"}

CIKS = [
    (2043699, "NHIC"),   # NewHold Investment Corp. III
    # (XXXXXXX, "NWCL"), # newcleo holdco S-4 registrant, add when known
]

FORMS = {
    "425", "S-4", "S-4/A", "8-K", "8-K/A",
    "DEFM14A", "DEFA14A", "DEF 14A", "PREM14A", "PRER14A",
    "EFFECT", "CORRESP", "UPLOAD",
    "S-1", "S-1/A", "10-K", "10-Q",
    "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
}
MAX_FILINGS = 14

# ---- AI titles and summaries (Claude) --------------------------------
API_URL = "https://api.anthropic.com/v1/messages"
AI_MODEL = os.environ.get("DEAL_AI_MODEL", "claude-sonnet-4-6")
AI_MAX = int(os.environ.get("DEAL_AI_MAX", "20"))
AI_MOCK = os.environ.get("DEAL_AI_MOCK", "0") == "1"
DOC_CHAR_CAP = 28000

AI_SYSTEM = """You title and summarise SEC filings for an internal \
monitoring terminal. You are given the text of one filing. Respond with \
strict JSON only: {"title": "...", "summary": "..."}.
Rules: the title is at most 12 words, concrete, and names the document \
type and subject. The summary is exactly two sentences, purely \
descriptive of what the document contains. British English. Never \
characterise the merits, valuation or attractiveness of the transaction \
or any securities; no advice; no quality adjectives; do not speculate \
beyond the text. No markdown fences, JSON only."""


def log(msg):
    print("[deal] " + msg, flush=True)


def load_prev_ai():
    """Carry summaries forward across rebuilds, keyed by document URL."""
    if not OUT_PATH.exists():
        return {}
    try:
        raw = OUT_PATH.read_text(encoding="utf-8")
        prev = json.loads(raw[raw.index("{"):].rstrip().rstrip(";"))
        return {f["url"]: {"ai_t": f["ai_t"], "ai_s": f["ai_s"]}
                for f in prev.get("filings", []) if f.get("ai_t")}
    except (ValueError, KeyError, json.JSONDecodeError):
        return {}


def strip_html(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;|&amp;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_doc_text(f):
    try:
        text = strip_html(http_get(f["url"]))
    except Exception as e:
        log("  ! doc fetch failed %s %s" % (f["date"], repr(e)[:60]))
        return ""
    if len(text) >= 800:
        return text[:DOC_CHAR_CAP]
    # thin cover page: try the largest exhibit in the accession folder
    try:
        folder = f["url"].rsplit("/", 1)[0]
        idx = json.loads(http_get(folder + "/index.json"))
        items = [i for i in idx.get("directory", {}).get("item", [])
                 if i.get("name", "").endswith(".htm")
                 and folder + "/" + i["name"] != f["url"]]
        items.sort(key=lambda i: int(i.get("size") or 0), reverse=True)
        if items:
            alt = strip_html(http_get(folder + "/" + items[0]["name"]))
            if len(alt) > len(text):
                return alt[:DOC_CHAR_CAP]
    except Exception:
        pass
    return text[:DOC_CHAR_CAP]


def summarise(text):
    if AI_MOCK:
        return {"title": "Mock filing title",
                "summary": "Mock sentence one. Mock sentence two."}
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    body = json.dumps({
        "model": AI_MODEL, "max_tokens": 300, "temperature": 0,
        "system": AI_SYSTEM,
        "messages": [{"role": "user",
                      "content": "Filing text:\n" + text}],
    }).encode("utf-8")
    for delay in (0, 6, 20):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(API_URL, data=body, method="POST",
                headers={"content-type": "application/json",
                         "x-api-key": key,
                         "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = "".join(b.get("text", "") for b in data.get("content", [])
                          if b.get("type") == "text").strip()
            out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out)
            s, e = out.find("{"), out.rfind("}")
            got = json.loads(out[s:e + 1])
            title = str(got.get("title") or "").strip()[:110]
            summary = str(got.get("summary") or "").strip()[:400]
            if title and summary:
                return {"title": title, "summary": summary}
            return None
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 529):
                log("  ! AI HTTP %s" % err.code)
                return None
        except Exception as err:
            log("  ! AI %s" % repr(err)[:70])
            return None
    return None


def fetch_submissions(cik):
    url = "https://data.sec.gov/submissions/CIK%010d.json" % cik
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                ValueError, TimeoutError) as e:
            if attempt == 2:
                log("  ! CIK %s %s" % (cik, repr(e)[:80]))
            time.sleep(2.0)
    return None


def filings_for(cik, label):
    data = fetch_submissions(cik)
    if not data:
        return None
    r = data.get("filings", {}).get("recent", {})
    out = []
    rows = zip(r.get("form", []), r.get("filingDate", []),
               r.get("accessionNumber", []), r.get("primaryDocument", []),
               r.get("primaryDocDescription", []))
    for form, date, acc, doc, desc in rows:
        if form not in FORMS:
            continue
        acc_nodash = acc.replace("-", "")
        url = ("https://www.sec.gov/Archives/edgar/data/%d/%s/%s"
               % (cik, acc_nodash, doc or ""))
        out.append({"form": form, "date": date,
                    "desc": (desc or form)[:80], "url": url,
                    "filer": label})
    return out


def main():
    all_filings, failed = [], False
    for cik, label in CIKS:
        got = filings_for(cik, label)
        if got is None:
            failed = True
        else:
            all_filings.extend(got)
        time.sleep(0.5)

    if failed and not all_filings:
        if OUT_PATH.exists():
            log("fetch failed; keeping previous deal.js")
            return
        log("fetch failed and no previous file; writing empty shell")

    all_filings.sort(key=lambda f: f["date"], reverse=True)
    kept = all_filings[:MAX_FILINGS]

    # AI titles and summaries: cached rows carry forward, new ones cost
    # one model call each. Never allowed to break the run.
    prev_ai = load_prev_ai()
    have_key = AI_MOCK or bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if not have_key:
        log("AI summaries skipped: ANTHROPIC_API_KEY not set")
    cached = fresh = 0
    for f in kept:
        c = prev_ai.get(f["url"])
        if c:
            f.update(c); cached += 1
            continue
        if not have_key or fresh >= AI_MAX:
            continue
        text = fetch_doc_text(f)
        if len(text) < 400:
            log("  thin document, no summary: %s %s"
                % (f["form"], f["date"]))
            continue
        got = summarise(text)
        if got:
            f["ai_t"], f["ai_s"] = got["title"], got["summary"]
            fresh += 1
            log("  summarised %s %s: %s"
                % (f["form"], f["date"], got["title"][:58]))
        time.sleep(0.8)
    log("ai summaries: %d carried forward, %d new" % (cached, fresh))

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "ciks": [c for c, _ in CIKS],
        "filings": kept,
    }
    body = ("window.NIT_DEAL=" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    tmp = OUT_PATH.with_suffix(".js.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)
    log("done | %d filings kept | latest %s %s" % (
        len(payload["filings"]),
        payload["filings"][0]["date"] if payload["filings"] else "n/a",
        payload["filings"][0]["form"] if payload["filings"] else ""))


if __name__ == "__main__":
    main()
