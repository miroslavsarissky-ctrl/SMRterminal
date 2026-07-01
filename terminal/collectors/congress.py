#!/usr/bin/env python3
"""
congress.py :: newcleo Nuclear Intel Terminal, legislation tracker.

Pulls recently updated bills from the official Congress.gov API, keeps
those whose title or latest action mentions nuclear-sector keywords,
enriches each with sponsor and policy area, and writes the legislation
sidecar (terminal/data/legislation.js) for the Legislation panel.

Design notes
------------
* Key: uses CONGRESS_API_KEY, else API_DATA_GOV_KEY (Congress.gov keys
  are api.data.gov keys, so the Regulations.gov key usually works),
  else the shared DEMO_KEY, which is heavily throttled and only good
  for smoke tests. A 403 in the log means the key is not enrolled for
  Congress.gov: a one-minute signup at api.congress.gov fixes it.
* Title-plus-action keyword matching. Honest limitation: nuclear
  provisions buried inside omnibus or appropriations bills whose titles
  never say so will not be caught.
* Fail-soft: on total fetch failure the previous legislation.js is kept
  and the script exits zero, so it never breaks the daily workflow.
* Stdlib only. No pip dependencies.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Lives at terminal/collectors/congress.py, so parent.parent is terminal/.
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "legislation.js"

KEY = (os.environ.get("CONGRESS_API_KEY")
       or os.environ.get("API_DATA_GOV_KEY")
       or "DEMO_KEY").strip()
CONGRESS = int(os.environ.get("CONGRESS_NUMBER", "119"))
BASE = "https://api.congress.gov/v3"
LOOKBACK_DAYS = 60
PAGES = 4          # 4 x 250 = up to 1000 most recently updated bills
MAX_BILLS = 12     # detail-enriched and shipped
UA = {"User-Agent": "newcleo-SMRterminal/1.0"}

KEYWORDS = re.compile(
    r"nuclear|reactor|uranium|HALEU|plutonium|fission|fuel cycle|"
    r"spent fuel|radioisotope|atomic energy|uranium enrichment|"
    r"\bNRC\b|\bMOX\b", re.IGNORECASE)
EXCLUDE = re.compile(r"nuclear famil|nuclear option", re.IGNORECASE)
POLICY_BLOCK = {"Immigration", "Families", "Sports and Recreation",
                "Arts, Culture, Religion", "Animals"}

CHAMBER_URL = {
    "HR": "house-bill", "S": "senate-bill",
    "HRES": "house-resolution", "SRES": "senate-resolution",
    "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
    "HCONRES": "house-concurrent-resolution",
    "SCONRES": "senate-concurrent-resolution",
}
BILLNO = {
    "HR": "H.R.", "S": "S.", "HRES": "H.Res.", "SRES": "S.Res.",
    "HJRES": "H.J.Res.", "SJRES": "S.J.Res.",
    "HCONRES": "H.Con.Res.", "SCONRES": "S.Con.Res.",
}


def log(msg):
    print("[congress] " + msg, flush=True)


def get(url):
    """GET with backoff for the shared-pool rate limit."""
    for delay in (0, 25, 50):
        if delay:
            log("  rate limited, waiting %ds" % delay)
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                code = str(data["error"].get("code", ""))
                if "RATE_LIMIT" in code:
                    continue
                log("  ! API error: %s" % json.dumps(data["error"])[:120])
                return None
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                continue
            log("  ! HTTP %s (a 403 means the key is not enrolled for "
                "Congress.gov; sign up at api.congress.gov)" % e.code)
            return None
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            log("  ! %s" % repr(e)[:90])
            return None
    return None


def recent_bills():
    since = (datetime.now(timezone.utc)
             - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    out, failures = [], 0
    for page in range(PAGES):
        params = urllib.parse.urlencode({
            "format": "json", "limit": 250, "offset": page * 250,
            "sort": "updateDate desc", "fromDateTime": since,
            "api_key": KEY})
        data = get("%s/bill/%d?%s" % (BASE, CONGRESS, params))
        if data is None:
            failures += 1
            continue
        bills = data.get("bills", [])
        out.extend(bills)
        if len(bills) < 250:
            break
        time.sleep(1.2)
    return out, failures


def bill_detail(btype, number):
    params = urllib.parse.urlencode({"format": "json", "api_key": KEY})
    data = get("%s/bill/%d/%s/%s?%s"
               % (BASE, CONGRESS, btype.lower(), number, params))
    return (data or {}).get("bill")


def main():
    if KEY == "DEMO_KEY":
        log("warning: running on the shared DEMO_KEY; expect throttling")
    bills, failures = recent_bills()
    log("scanned %d recently updated bills (%d page failures)"
        % (len(bills), failures))

    if not bills:
        if OUT_PATH.exists():
            log("fetch failed; keeping previous legislation.js")
            return
        log("fetch failed and no previous file; writing empty shell")

    hits, seen = [], set()
    for b in bills:
        text = (b.get("title") or "") + " " + \
               ((b.get("latestAction") or {}).get("text") or "")
        if not KEYWORDS.search(text) or EXCLUDE.search(text):
            continue
        key = (b.get("type"), b.get("number"))
        if key in seen:
            continue
        seen.add(key)
        hits.append(b)
    hits.sort(key=lambda b: ((b.get("latestAction") or {})
                             .get("actionDate") or ""), reverse=True)
    hits = hits[:MAX_BILLS]
    log("keyword matches kept: %d" % len(hits))

    # Partial-failure guard: a degraded scan must never shrink a good
    # panel to nothing. If pages failed and we matched nothing, keep
    # the previous file.
    if failures and not hits and OUT_PATH.exists():
        log("degraded scan with zero matches; keeping previous file")
        return

    items = []
    for b in hits:
        btype = (b.get("type") or "").upper()
        number = str(b.get("number") or "")
        la = b.get("latestAction") or {}
        detail = bill_detail(btype, number) or {}
        sp = (detail.get("sponsors") or [{}])[0]
        policy = ((detail.get("policyArea") or {}).get("name") or "")
        if policy in POLICY_BLOCK:
            log("  dropped %s %s (policy area: %s)"
                % (btype, number, policy))
            continue
        items.append({
            "billno": "%s %s" % (BILLNO.get(btype, btype), number),
            "title": (b.get("title") or "")[:160],
            "url": "https://www.congress.gov/bill/%dth-congress/%s/%s"
                   % (CONGRESS, CHAMBER_URL.get(btype, "bill"), number),
            "status": (la.get("text") or "")[:120],
            "status_date": la.get("actionDate") or "",
            "sponsor": (sp.get("fullName") or "").strip(),
            "policy": policy,
            "ts": (b.get("updateDate") or "") + "T00:00:00+00:00"
                  if len(b.get("updateDate") or "") == 10
                  else (b.get("updateDate") or ""),
        })
        time.sleep(1.2)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "congress": CONGRESS,
        "count": len(items),
        "items": items,
    }
    body = ("window.NIT_BILLS=" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    tmp = OUT_PATH.with_suffix(".js.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)
    log("done | %d bills | latest action %s" % (
        len(items), items[0]["status_date"] if items else "n/a"))


if __name__ == "__main__":
    main()
