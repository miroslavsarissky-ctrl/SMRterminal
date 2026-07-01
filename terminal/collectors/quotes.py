#!/usr/bin/env python3
"""
quotes.py :: newcleo Nuclear Intel Terminal, market data rail.

Fetches daily price history for every ticker in the watchlist and writes
a quotes sidecar (terminal/data/quotes.js) with last price, 1d/7d/30d
percentage moves and a 30-point sparkline series per ticker.

Design notes
------------
* Tickers are read from data/watchlist.js at run time, so adding a ticker
  to the watchlist automatically adds it to the market rail.
* Source is Yahoo's public chart endpoint (no key). It is unofficial, so
  the run is fail-soft: individual tickers may drop out, and if fewer
  than half succeed the script exits non-zero WITHOUT writing, leaving
  yesterday's data in place. The source URL sits behind one constant so
  a keyed provider can be swapped in later without redesign.
* Percentage moves are computed on trading rows: d1 is versus the prior
  row, d7 versus 5 rows back, d30 versus 21 rows back.
* Stdlib only. No pip dependencies.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Lives at terminal/collectors/quotes.py, so parent.parent is terminal/.
ROOT = Path(__file__).resolve().parent.parent
WL_PATH = ROOT / "data" / "watchlist.js"
OUT_PATH = ROOT / "data" / "quotes.js"

SOURCE = "yahoo"
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{t}?range=3mo&interval=1d")
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36")}
SPARK_N = 30
MIN_OK_FRACTION = 0.5


def log(msg):
    print("[quotes] " + msg, flush=True)


def load_js_object(path):
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw[raw.index("{"):].rstrip().rstrip(";"))


def watchlist_tickers():
    wl = load_js_object(WL_PATH)
    return sorted({e["ticker"] for e in wl.get("entities", [])
                   if e.get("ticker")})


def fetch_history(ticker):
    """Return (dates, closes, currency) for one ticker, or None."""
    url = CHART_URL.format(t=ticker)
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data["chart"]["result"][0]
            stamps = result.get("timestamp") or []
            closes = result["indicators"]["quote"][0].get("close") or []
            cur = (result.get("meta") or {}).get("currency") or "USD"
            pairs = [(ts, c) for ts, c in zip(stamps, closes)
                     if c is not None]
            if len(pairs) >= 8:
                dates = [datetime.fromtimestamp(ts, tz=timezone.utc)
                         .strftime("%Y-%m-%d") for ts, _ in pairs]
                return dates, [round(c, 4) for _, c in pairs], cur
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
                IndexError, ValueError, TimeoutError) as e:
            if attempt == 2:
                log("  ! %s %s" % (ticker, repr(e)[:70]))
        time.sleep(1.5)
    return None


def metrics(closes):
    last = closes[-1]

    def pct(back):
        if len(closes) > back and closes[-1 - back]:
            return round((last / closes[-1 - back] - 1) * 100, 2)
        return None

    return {
        "last": round(last, 2),
        "d1": pct(1),
        "d7": pct(5),
        "d30": pct(21),
        "spark": [round(c, 3) for c in closes[-SPARK_N:]],
    }


def main():
    tickers = watchlist_tickers()
    if not tickers:
        log("ERROR: no tickers found in watchlist.js")
        sys.exit(1)
    log("fetching %d tickers from %s" % (len(tickers), SOURCE))

    quotes, missing, asof = {}, [], ""
    for t in tickers:
        got = fetch_history(t)
        if got:
            dates, closes, cur = got
            q = metrics(closes)
            q["cur"] = cur
            quotes[t] = q
            asof = max(asof, dates[-1])
        else:
            missing.append(t)
        time.sleep(0.4)

    if len(quotes) < len(tickers) * MIN_OK_FRACTION:
        log("ERROR: only %d of %d tickers fetched; keeping previous data"
            % (len(quotes), len(tickers)))
        sys.exit(1)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "asof": asof,
        "source": SOURCE,
        "count": len(quotes),
        "missing": missing,
        "tickers": quotes,
    }
    body = ("window.NIT_QUOTES=" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    tmp = OUT_PATH.with_suffix(".js.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)
    log("done | %d tickers | asof %s%s" % (
        len(quotes), asof,
        (" | missing: " + ",".join(missing)) if missing else ""))


if __name__ == "__main__":
    main()
