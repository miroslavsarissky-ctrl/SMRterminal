#!/usr/bin/env python3
"""Investor share-history backfill: exact per-CIK prior-season positions.

For every investor CIK in the current dataset, pulls their own 13F for each target season
window from the submissions API (latest filing in window, amendments included) and parses
the 16-issuer universe. Output: data/inv_history.json — shares by CIK/ticker/season plus a
CIK->name map. Deltas are computed client-side on SHARES, never value. Checkpointed per
(cik, season) so interrupted runs resume. Designed for a one-off workflow_dispatch run in
Actions (~25 min full depth); the quarterly collector appends new seasons afterwards.
Env: HIST_LIMIT caps investors per run (0 = all, ranked by current exposure)."""
import os, re, sys, json, time, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_investors as ci

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
SEASONS = [('Q1 2025', '2025-04-01', '2025-06-30'),
           ('Q4 2025', '2026-01-01', '2026-03-31')]
LIMIT = int(os.environ.get('HIST_LIMIT', '0'))
CK = '/tmp/invh_ck.json'

def ck_load():
    try: return json.load(open(CK))
    except Exception: return {}

def season_filing(cik, lo, hi):
    r = ci.get(f'https://data.sec.gov/submissions/CIK{cik}.json')
    if r is None:
        return None
    rec = r.json()['filings']['recent']
    cand = [(rec['filingDate'][i], rec['accessionNumber'][i]) for i in range(len(rec['form']))
            if rec['form'][i] in ('13F-HR', '13F-HR/A') and lo <= rec['filingDate'][i] <= hi]
    return max(cand)[1] if cand else ''

def main(out_dir=DATA):
    inv = json.load(open(os.path.join(out_dir, 'investors.json')))
    ciks = [(i['cik'], i['name'], i['total']) for i in inv['items']]
    ciks.sort(key=lambda x: -x[2])
    if LIMIT:
        ciks = ciks[:LIMIT]
    hp = os.path.join(out_dir, 'inv_history.json')
    hist = json.load(open(hp)) if os.path.exists(hp) else {'seasons': [], 'names': {}}
    bylab = {s['label']: s for s in hist['seasons']}
    for lab, lo, hi in SEASONS:
        bylab[lab] = {'label': lab, 'holders': {}}      # rebuild these seasons from checkpoint

    ck = ck_load()
    done = 0
    for cik, name, _ in ciks:
        hist['names'][cik] = name
        for lab, lo, hi in SEASONS:
            key = f'{cik}|{lab}'
            if key in ck:
                if ck[key] is not None:
                    bylab[lab]['holders'][cik] = ck[key]
                continue
            time.sleep(ci.SLEEP)
            adsh = season_filing(cik, lo, hi)
            if adsh is None:
                continue
            if not adsh:
                ck[key] = None                      # no filing that season -> not covered (n/c)
            else:
                time.sleep(ci.SLEEP)
                xml = ci.fetch_table(int(cik), adsh, 'x.nomatch')
                pos = ci.parse_positions(xml) if xml else {}
                sh = {t: v[1] for t, v in pos.items()}
                ck[key] = sh
                bylab[lab]['holders'][cik] = sh
            done += 1
            if done % 20 == 0:
                json.dump(ck, open(CK, 'w'))
                print(f'  …{done} pulls', flush=True)
    json.dump(ck, open(CK, 'w'))
    order = {'Q1 2025': 0, 'Q4 2025': 1}
    hist['seasons'] = sorted(bylab.values(), key=lambda s: order.get(s['label'], 99))
    hist['generated'] = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    json.dump(hist, open(hp, 'w'), ensure_ascii=False)
    open(os.path.join(out_dir, 'inv_history.js'), 'w').write('window.NIT_INVH = ' + json.dumps(hist, ensure_ascii=False) + ';')
    cov = {s['label']: len(s['holders']) for s in hist['seasons']}
    print('history coverage:', cov)

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
