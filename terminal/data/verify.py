#!/usr/bin/env python3
"""
verify.py :: newcleo Nuclear Intel Terminal, verification layer.

Scores feed items for newcleo relevance with the Claude API and promotes
qualifying items to a verified sidecar (terminal/data/verified.js).

Design notes
------------
* Sidecar, not in-place: collect.py rewrites feed.js on every run, so
  verification state lives in its own file keyed by item id (the ids are
  stable content hashes). The frontend merges the two at load time.
* Incremental: only ids never scored before are sent to the API. Steady
  state is a handful of items per run.
* Promotion is a rule, not a vibe. An item is verified when BOTH hold:
    1. relevance score >= PROMOTE_MIN (default 2 on a 0..3 scale), and
    2. it is a primary-source bucket (regulatory, nrc, filings, funding,
       research all come from official APIs) OR it was independently
       carried by at least one other outlet (dupes >= 1).
  The social bucket never auto-promotes.
* Stdlib only. No pip dependencies.

Environment
-----------
ANTHROPIC_API_KEY   required
VERIFY_MODEL        default claude-haiku-4-5-20251001
VERIFY_MAX_ITEMS    per-run cap on new items scored, default 150
VERIFY_BACKFILL     "1" lifts the cap (first run over the whole feed)
VERIFY_PROMOTE_MIN  default 2
VERIFY_MOCK         "1" skips the API and assigns score 0 to everything;
                    pipeline test only, never use in production
Flags
-----
--dry-run           select and print what would be sent, write nothing

NOTE: this file lives in the repository. Until the repo is private, keep
the rubric below to publicly announced facts about newcleo only.
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

ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = ROOT / "terminal" / "data" / "feed.js"
SIDE_PATH = ROOT / "terminal" / "data" / "verified.js"

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("VERIFY_MODEL", "claude-haiku-4-5-20251001")
MAX_ITEMS = int(os.environ.get("VERIFY_MAX_ITEMS", "150"))
BACKFILL = os.environ.get("VERIFY_BACKFILL", "0") == "1"
PROMOTE_MIN = int(os.environ.get("VERIFY_PROMOTE_MIN", "2"))
MOCK = os.environ.get("VERIFY_MOCK", "0") == "1"
BATCH = 25
MAX_TOKENS = 2500

PRIMARY_BUCKETS = {"regulatory", "nrc", "filings", "funding", "research"}
NEVER_PROMOTE_BUCKETS = {"social"}

TAG_VOCAB = [
    "mox", "fuel-cycle", "feedstock", "reprocessing", "srs",
    "doe-authorisation", "launch-pad", "foci", "fast-reactor", "lfr",
    "nrc", "part-70", "part-53", "competitor", "oklo",
    "capital-markets", "haleu", "policy", "safeguards", "plutonium",
]

# Rubric: publicly announced facts about newcleo only (see NOTE above).
SYSTEM_PROMPT = """You are the triage analyst for a nuclear-sector market \
intelligence terminal operated by newcleo, an advanced nuclear company. \
newcleo's publicly announced positioning: it develops lead-cooled fast \
reactors (LFR), plans MOX fuel fabrication from recycled nuclear material, \
targets US deployment at the Savannah River Site through the DOE Nuclear \
Energy Launch Pad programme, partners with Oklo under the DOE surplus \
plutonium utilization programme and with SHINE on recycling feedstock, and \
is listing in the US via NewHold Investment Corp. III (NHIC, ticker NWCL).

Score each item 0 to 3 for decision relevance to newcleo:
3 = direct: mentions newcleo, NHIC or NWCL; Savannah River Site; DOE Launch \
Pad or Reactor Pilot Program authorisations; surplus plutonium or its \
disposition; MOX; lead-cooled or other fast reactors; Oklo fuel-cycle \
activity; FOCI or foreign-ownership rules at DOE sites; 10 CFR Part 70; \
implementation of Executive Orders 14301 or 14302.
2 = adjacent: advanced-reactor competitor milestones; recycling or \
reprocessing (SHINE, Curio, Orano and peers); NRC advanced-reactor \
licensing policy including Part 53; HALEU and enrichment policy; nuclear \
capital markets, SPACs and listings; South Carolina energy policy; DOE-NE \
funding programmes.
1 = background: general nuclear industry news, conventional LWR fleet \
operations, uranium miners, utility power agreements.
0 = noise: routine regulatory paperwork with no strategic content, \
off-topic energy items.

For every item also produce:
- "why": one sentence, maximum 140 characters, stating why it matters to \
newcleo. Concise, factual, British English, no hype, no hedging. Empty \
string when score is below 2.
- "tags": up to 3 tags drawn ONLY from this vocabulary: {vocab}

Respond with a JSON array only. No markdown fences, no preamble, no \
commentary. One object per input item: \
{{"id": "...", "score": 0, "why": "...", "tags": []}}. \
Every input id must appear exactly once in the output.""".format(
    vocab=", ".join(TAG_VOCAB)
)


def log(msg):
    print("[verify] " + msg, flush=True)


def load_js_object(path, prefix_hint):
    """Parse window.X = {...}; files written by the collectors."""
    raw = path.read_text(encoding="utf-8")
    start = raw.index("{")
    body = raw.rstrip().rstrip(";")[start:]
    obj = json.loads(body)
    if prefix_hint not in raw[:start]:
        log("warning: unexpected prefix in %s" % path.name)
    return obj


def load_sidecar():
    if not SIDE_PATH.exists():
        return {"generated": None, "model": MODEL, "scored": {}, "items": {}}
    try:
        side = load_js_object(SIDE_PATH, "NIT_VERIFIED")
    except (ValueError, json.JSONDecodeError):
        log("warning: sidecar unreadable, starting fresh")
        return {"generated": None, "model": MODEL, "scored": {}, "items": {}}
    side.setdefault("scored", {})
    side.setdefault("items", {})
    return side


def write_sidecar(side):
    side["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    side["model"] = MODEL
    payload = "window.NIT_VERIFIED=" + json.dumps(
        side, ensure_ascii=False, separators=(",", ":")
    ) + ";\n"
    tmp = SIDE_PATH.with_suffix(".js.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(SIDE_PATH)


def compact(item):
    """The slim view of an item that goes to the model."""
    return {
        "id": item["id"],
        "title": (item.get("title") or "")[:200],
        "source": (item.get("source") or "")[:60],
        "bucket": item.get("bucket") or "",
        "summary": (item.get("summary") or "")[:300],
        "entities": (item.get("entities") or [])[:6],
    }


def call_api(batch):
    if MOCK:
        return [{"id": b["id"], "score": 0, "why": "", "tags": []} for b in batch]
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log("ERROR: ANTHROPIC_API_KEY is not set")
        sys.exit(1)
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": "Score these items:\n" + json.dumps(
                batch, ensure_ascii=False
            ),
        }],
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    })
    delays = [0, 4, 15, 40]
    last_err = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = "".join(
                blk.get("text", "") for blk in data.get("content", [])
                if blk.get("type") == "text"
            )
            return parse_model_json(text)
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.read()[:300])
            if e.code not in (429, 500, 502, 503, 529):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ValueError) as e:
            last_err = repr(e)
    log("ERROR: API call failed after retries: %s" % last_err)
    sys.exit(1)


def parse_model_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in model output")
    return json.loads(text[start:end + 1])


def sanitise(result):
    try:
        score = max(0, min(3, int(result.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    why = str(result.get("why") or "").strip()[:150]
    tags = [t for t in (result.get("tags") or []) if t in TAG_VOCAB][:3]
    return score, why, tags


def promotable(item, score):
    if score < PROMOTE_MIN:
        return False
    bucket = item.get("bucket") or ""
    if bucket in NEVER_PROMOTE_BUCKETS:
        return False
    if bucket in PRIMARY_BUCKETS:
        return True
    return int(item.get("dupes") or 0) >= 1


def main():
    dry = "--dry-run" in sys.argv

    feed = load_js_object(FEED_PATH, "NIT_FEED")
    items = feed.get("items", [])
    by_id = {i["id"]: i for i in items}
    side = load_sidecar()

    todo = [i for i in items if i["id"] not in side["scored"]]
    cap = len(todo) if BACKFILL else MAX_ITEMS
    todo = todo[:cap]

    log("feed items: %d | already scored: %d | to score now: %d%s" % (
        len(items), len(side["scored"]), len(todo),
        " (backfill)" if BACKFILL else "",
    ))

    if dry:
        for i in todo[:10]:
            log("would send: %s | %s" % (i["bucket"], i["title"][:80]))
        log("dry run, nothing written")
        return

    promoted = 0
    for pos in range(0, len(todo), BATCH):
        batch = todo[pos:pos + BATCH]
        results = call_api([compact(i) for i in batch])
        seen = set()
        for r in results:
            iid = r.get("id")
            if iid not in by_id or iid in seen:
                continue
            seen.add(iid)
            score, why, tags = sanitise(r)
            side["scored"][iid] = score
            if promotable(by_id[iid], score):
                side["items"][iid] = {
                    "score": score, "why": why, "tags": tags,
                    "at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"),
                }
                promoted += 1
        missing = [b["id"] for b in batch if b["id"] not in seen]
        if missing:
            log("warning: %d ids missing from model output, "
                "will retry next run" % len(missing))
        log("batch %d..%d done" % (pos + 1, pos + len(batch)))

    # Prune ids that have rotated out of the feed.
    live = set(by_id)
    side["scored"] = {k: v for k, v in side["scored"].items() if k in live}
    side["items"] = {k: v for k, v in side["items"].items() if k in live}

    write_sidecar(side)
    log("done | newly promoted: %d | verified total: %d | scored total: %d"
        % (promoted, len(side["items"]), len(side["scored"])))


if __name__ == "__main__":
    main()
