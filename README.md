# Nuclear Intel Terminal

A single-page intelligence terminal that aggregates United States nuclear-sector primary sources into one Bloomberg-style view, refreshed automatically and served as a static site.

**Live:** https://miroslavsarissky-ctrl.github.io/SMRterminal/terminal/terminal.html

---

## Overview

The terminal pulls from federal registers, regulators, the SEC, the legislature, the national labs, energy statistics and the nuclear trade press, then presents them as a filterable feed alongside a deadline radar, a legislation tracker and a state-level energy map. It is built for newcleo's United States market-intelligence work: watching the advanced-reactor and fuel-cycle landscape, the regulatory and funding pipeline, and the companies and agencies that matter to that space.

The design is deliberately simple to host. There is no server and no database. A scheduled job runs a Python collector, the collector writes data files into the repository, and a single self-contained HTML file reads those files in the browser. Everything is committed to the repo and served by GitHub Pages.

## How it works

```
GitHub Actions (cron, every 30 minutes, or run on demand)
  └─ runs  terminal/collectors/collect.py
       ├─ fetches each source, normalises items, applies per-source throttles
       ├─ merges with existing history and the verified layer
       ├─ writes terminal/data/*.json and *.js
       └─ commits the data back to the repo as "intel-bot"

GitHub Pages serves terminal/terminal.html, which reads the committed
data client-side. No runtime backend.
```

Because the data is just committed files, the page has no runtime dependencies and loads instantly. The trade-off is that the feed is only as fresh as the last successful collector run.

## Repository structure

```
SMRterminal/
├─ terminal/
│  ├─ terminal.html               # the single-file UI (Intel + Energy views)
│  ├─ collectors/
│  │  └─ collect.py               # the data collector run by GitHub Actions
│  └─ data/                       # generated data, committed by the bot
│     ├─ feed.json / feed.js      # the intelligence feed
│     ├─ watchlist.json / .js     # tracked entities (companies, agencies, labs)
│     ├─ energy.json / energy.js  # EIA state electricity prices + nuclear capacity
│     ├─ usmap.js                 # static US state geometry (AlbersUSA projection)
│     ├─ logo.png                 # newcleo mark
│     └─ *_state.json             # per-source throttle timestamps
└─ .github/
   └─ workflows/
      └─ refresh.yml              # cron + manual trigger that runs collect.py
```

The `.js` files are the same payloads as the matching `.json`, wrapped as `window.NIT_*` globals so the page can load them with a plain `<script>` tag and no fetch.

## Data sources

| Source | Section | Cadence | Key required |
|---|---|---|---|
| Federal Register | Regulatory | every run | no |
| Regulations.gov | Regulatory | ~6h | `API_DATA_GOV_KEY` |
| Congress (bills) | Legislation panel | ~6h | `API_DATA_GOV_KEY` |
| NRC ADAMS | NRC | ~4h | `NRC_APS_KEY` |
| SEC EDGAR | Filings | every run | no |
| Grants.gov | Funding | every run | no |
| SAM.gov | Funding | ~3h | `SAM_GOV_API_KEY` |
| OSTI | Research | every run | no |
| Publisher feeds (World Nuclear News, ANS, Neutron Bytes) | News | every run | no |
| Google News | News | every run | no |
| X (Twitter) | Social | off by default | `X_BEARER_TOKEN` |
| EIA | Energy view | ~daily | `EIA_API_KEY` |

EDGAR is pulled two ways: per-company submissions for the watchlist, plus a full-text search for "newcleo" that surfaces the NewHold / NHIC de-SPAC trail inside other parties' filings. Sources with no key requirement work out of the box; the rest stay dark until their key is supplied.

## The terminal

Two views, switched from the header.

### Intel

The default view. A KPI strip across the top counts the last seven days by bucket (Regulatory, NRC, Funding, Filings, Research, News, Social), a scrolling tape carries the latest headlines, and the main column is the feed itself with search, a time window (24h / 7d / all), a verified-only toggle, and entity filtering by clicking any tagged company or agency. The right rail carries a deadline radar (comment windows and other dated items, soonest first), a Legislation panel tracking current-Congress nuclear bills with their latest action and sponsor, watchlist movers, and a coverage summary.

Items arrive raw and uncorroborated. A subset can be promoted to a verified layer; the verified-only filter and the badges in the feed reflect that distinction, which matters before anything is cited upward.

### Energy

A geographic map of United States electricity economics, drawn from real state borders (US Census geometry, AlbersUSA projection with Alaska and Hawaii as insets).

The default layer shades each state by retail electricity price, with a sector toggle for All, Residential, Commercial and Industrial. Colour is a five-band quintile scale running green (cheapest fifth) through amber to red (priciest fifth), so the spread is visible rather than washed out; the price is printed on each state and the legend shows the band cut-offs, which recompute per sector. Operating-nuclear states carry a small radiation marker on this layer.

A nuclear overlay re-shades the map by operating reactor capacity in green-intensity quintiles, with the GW printed on each reactor state and no-nuclear states greyed. Hovering any state gives a tooltip with its current value, and the side panel shows the full breakdown: price by sector and the nuclear fleet there.

Energy data comes from the EIA API v2, specifically `electricity/retail-sales` (price by state and sector) and `electricity/operating-generator-capacity` (nuclear capacity and unit count by state). The retail series lags roughly two months, so the map shows the latest published month rather than the live week.

## Configuration (API keys)

Keys are stored as GitHub Actions repository secrets (**Settings → Secrets and variables → Actions**).

| Secret | Enables |
|---|---|
| `API_DATA_GOV_KEY` | Regulations.gov dockets and Congress legislation |
| `NRC_APS_KEY` | NRC ADAMS document pulls |
| `SAM_GOV_API_KEY` | SAM.gov contract opportunities |
| `EIA_API_KEY` | Energy view (state prices and nuclear capacity) |
| `X_BEARER_TOKEN` | X / social posts (optional) |

**Important:** adding a secret is not enough on its own. Each secret must also be passed through to the script in the Collect step's `env:` block in `.github/workflows/refresh.yml`, for example:

```yaml
      - name: Collect
        env:
          API_DATA_GOV_KEY: ${{ secrets.API_DATA_GOV_KEY }}
          NRC_APS_KEY:      ${{ secrets.NRC_APS_KEY }}
          SAM_GOV_API_KEY:  ${{ secrets.SAM_GOV_API_KEY }}
          EIA_API_KEY:      ${{ secrets.EIA_API_KEY }}
          X_BEARER_TOKEN:   ${{ secrets.X_BEARER_TOKEN }}
        run: python terminal/collectors/collect.py
```

A secret that is not wired into the `env:` block is invisible to the collector, and that source simply stays dark with no error. This is the single most common reason a source fails to populate.

## Deploying and updating

The project is maintained through the GitHub web interface; no local git is required.

1. To change the UI, upload `terminal/terminal.html` (overwrite). It is front-end only, so no data refresh is needed; give Pages a minute, then hard-refresh the browser (Cmd/Ctrl+Shift+R) to clear the cached copy.
2. To change collection logic, upload `terminal/collectors/collect.py`, then trigger the workflow under **Actions → refresh-intel → Run workflow** to regenerate the data.
3. To add a source key, create the secret and add its line to the workflow `env:` block as above, then run the workflow once.
4. The static map geometry (`terminal/data/usmap.js`) is uploaded once and only needs touching if the projection or resolution changes.

The workflow also runs on its own every 30 minutes, so most data refreshes without any manual step.

## Running locally

```bash
# from the repository root
pip install requests
# keys are optional; sources without one are simply skipped
export API_DATA_GOV_KEY=...  EIA_API_KEY=...  NRC_APS_KEY=...  SAM_GOV_API_KEY=...
python3 terminal/collectors/collect.py
# then open terminal/terminal.html in a browser
```

Running the collector writes the same `data/*.json` and `*.js` files locally that the bot would commit. Opening `terminal.html` directly off disk works because it reads those files as scripts.

## Maintenance notes

The watchlist drives entity tagging in the feed and the company set behind several panels; when companies are added on the related dashboards, mirror them into `watchlist.json` so the terminal recognises them. Per-source throttles are tracked in the `*_state.json` files and stop the slower APIs from being hit on every run; deleting one forces that source to re-pull on the next run. The collector self-heals news from open aggregators each run, while verified items and trusted publisher feeds persist as history.

## Notes and caveats

This is an internal tool. The raw feed is uncorroborated until items are promoted to the verified layer, and it should be treated as a monitoring surface rather than a source of record. Coverage favours primary sources (regulators, the SEC, the legislature, the national labs, official statistics) over secondary commentary. Data latency varies by source, from minutes for news to roughly two months for EIA retail prices.

---

Built and maintained by newcleo, Communications & Market Intelligence.
