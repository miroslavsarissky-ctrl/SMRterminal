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


def log(msg):
    print("[deal] " + msg, flush=True)


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
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "ciks": [c for c, _ in CIKS],
        "filings": all_filings[:MAX_FILINGS],
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
