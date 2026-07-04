#!/usr/bin/env python3
"""
energymix.py :: newcleo Nuclear Intel Terminal, state generation mix.

Fetches annual utility-scale net generation by fuel for every state from
the EIA v2 API and writes the mix sidecar (terminal/data/energymix.js).
The Energy tab renders it as a percentage breakdown under the operating
nuclear block, for states and for RTO territories (aggregated).

Design notes
------------
* Annual frequency, latest year available per state, so the mix is not
  distorted by hydro springs and gas summers.
* Utility-scale only (that is what this EIA dataset covers); small-scale
  rooftop solar is excluded, and the card says so.
* Values arrive in thousand MWh, which is GWh; stored as GWh so the
  frontend computes percentages.
* Writes only when the payload actually changed, so the daily run does
  not produce commit noise for annually-changing data.
* Fail-soft: no key means a clean skip, fetch failure keeps the old
  file. This runs inside the daily market-quotes workflow and must
  never break it.
* Stdlib only. No pip dependencies.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Lives at terminal/collectors/energymix.py, so parent.parent is terminal/.
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "energymix.js"

EB = "https://api.eia.gov/v2/electricity/electric-power-operational-data/data/"
UA = {"User-Agent": "newcleo-SMRterminal/1.0"}

FUELS = ["ALL", "COW", "NG", "NUC", "HYC", "HPS", "WND", "SUN",
         "GEO", "WWW", "WAS", "PET", "PC", "OOG", "OTH"]

STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL",
    "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE",
    "NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD",
    "TN","TX","UT","VT","VA","WA","WV","WI","WY",
}


def log(msg):
    print("[energymix] " + msg, flush=True)


def fetch_rows(key):
    rows, offset = [], 0
    while offset <= 15000:
        params = [
            ("api_key", key), ("frequency", "annual"),
            ("data[0]", "generation"),
            ("facets[sectorid][]", "99"),
            ("start", "2023"),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "desc"),
            ("length", "5000"), ("offset", str(offset)),
        ] + [("facets[fueltypeid][]", f) for f in FUELS]
        url = EB + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        batch = data.get("response", {}).get("data", [])
        rows.extend(batch)
        if len(batch) < 5000:
            break
        offset += 5000
    return rows


def fold_rows(rows):
    """Latest annual period per state; GWh by fuel; ALL becomes total."""
    latest = {}
    for x in rows:
        loc = x.get("location") or x.get("stateid")
        if loc not in STATES:
            continue
        per = str(x.get("period") or "")
        if per > latest.get(loc, ""):
            latest[loc] = per
    states = {}
    for x in rows:
        loc = x.get("location") or x.get("stateid")
        if loc not in STATES:
            continue
        if str(x.get("period") or "") != latest.get(loc):
            continue
        fuel = x.get("fueltypeid")
        gen = x.get("generation")
        if fuel not in FUELS or gen in (None, ""):
            continue
        gwh = round(float(gen), 1)
        st = states.setdefault(loc, {"period": latest[loc],
                                     "total": 0.0, "src": {}})
        if fuel == "ALL":
            st["total"] = gwh
        elif gwh > 0:
            st["src"][fuel] = st["src"].get(fuel, 0.0) + gwh
    return states


def main():
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        log("skipped: EIA_API_KEY not set")
        return
    try:
        rows = fetch_rows(key)
    except Exception as e:
        if OUT_PATH.exists():
            log("fetch failed (%s); keeping previous file" % repr(e)[:90])
            return
        log("fetch failed and no previous file (%s)" % repr(e)[:90])
        return
    states = fold_rows(rows)
    if len(states) < 40:
        log("only %d states returned; keeping previous file" % len(states))
        return
    period = max(s["period"] for s in states.values())
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "period": period,
        "states": states,
    }
    body = ("window.NIT_MIX=" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")) + ";\n")

    if OUT_PATH.exists():
        old = OUT_PATH.read_text(encoding="utf-8")
        import re
        strip = lambda t: re.sub(r'"generated":"[^"]*"', '', t)
        if strip(old) == strip(body):
            log("no change in mix data; nothing written")
            return
    tmp = OUT_PATH.with_suffix(".js.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)
    log("done | %d states | latest period %s" % (len(states), period))


if __name__ == "__main__":
    main()
