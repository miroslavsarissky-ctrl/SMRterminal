# newcleo Nuclear Intel Terminal — v1

Live market-intelligence feed tracking the companies in the US fuel-cycle and
advanced-reactor dashboards. Public primary sources plus open news feeds,
refreshed every 30 minutes.

## What it pulls

| Source | What | Key needed |
|---|---|---|
| Federal Register API | NRC + DOE notices/rules, with comment deadlines | no |
| Grants.gov API | nuclear FOAs / RFAs | no |
| NRC ADAMS API | docket documents matching the watchlist | no |
| SEC EDGAR | material filings (8-K, 10-K/Q, S-1…) for tickered companies | no |
| Google News RSS | rotating per-company + standing topic queries | no |
| SAM.gov API | RFIs / RFPs / sources-sought | `SAM_GOV_API_KEY` |
| X API v2 | posts from watchlist handles (pay-per-use reads) | `X_BEARER_TOKEN` |

## Deploy (GitHub Pages)

1. Create a private-or-public repo, push this folder's contents to the root.
2. Settings → Pages → deploy from branch `main`, folder `/ (root)`.
3. Settings → Secrets and variables → Actions → add:
   - `SAM_GOV_API_KEY` — free key from https://api.data.gov (instant)
   - `X_BEARER_TOKEN` — from the X developer console (pay-per-use billing;
     ~50 handles at 30-min polling lands around $20–60/month in reads)
4. Actions tab → enable workflows → run **refresh-intel** once manually.
5. Open `terminal.html` on your Pages URL. Link it from the hub `index.html`.

The workflow commits refreshed `data/` files; the page self-reloads when it
sees a newer snapshot (checks every 5 minutes).

## Run locally

    pip install requests feedparser
    python3 collectors/collect.py
    open terminal.html        # reads data/feed.js — works from file://

## Editing the watchlist

`data/watchlist.json` is generated from the two dashboards (223 entities).
Edit it directly to add aliases, tickers, or X handles. Fields:
`tier` 1 = active monitoring, 2 = tagging only; `query` = gets its own
Google News query; `x.verified` — set true after you confirm the handle.
**Verify X handles before enabling the X collector** — they are best-guess.

## The two-tier rule

Everything lands as **raw feed** (uncorroborated). Promote an item to the
**verified layer** by setting `"verified": true` on it in `data/feed.json`
(or via a future curation UI). The verified flag survives refreshes.
This keeps the terminal honest against the dashboards' US-primary-source
corroboration standard.

## Known v1 behaviour

- Wire stories appear once per outlet (no near-duplicate clustering yet).
- ADAMS keeps only documents matching watchlist names/topics, max 40/run.
- Google News rotates 12 companies per run, so each of the 45 query
  entities refreshes roughly every 2 hours.
