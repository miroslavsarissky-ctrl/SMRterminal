#!/usr/bin/env python3
"""Congress.gov legislation tracker -> data/congress.js (window.NIT_CONGRESS).

Two layers: TRACKED bills fetched individually for full latest-action detail (the REFUEL
Act pair and any bill Miro pins here), plus a scan of recently-acted bills in the current
Congress whose titles match nuclear keywords. Complements collect.py's GovInfo full-text
discovery (which catches bills once text publishes); the terminal merges both sidecars.
Key: api.data.gov (CONGRESS_API_KEY or API_DATA_GOV_KEY env; DEMO_KEY fallback for seeding).
"""
import os, re, sys, json, time, datetime as dt
import requests

UA = {'User-Agent': 'newcleo-nuclear-intel-terminal/1.0 (market intelligence; contact: comms@newcleo.com)'}
KEY = os.environ.get('CONGRESS_API_KEY') or os.environ.get('API_DATA_GOV_KEY') or 'DEMO_KEY'
B = 'https://api.congress.gov/v3'
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
CONGRESS = 119
TRACKED = [('hr', 3978), ('s', 2082)]                     # Nuclear REFUEL Act pair
KW = re.compile(r'nuclear|uranium|reactor|\bNRC\b|atomic|isotope|HALEU|fission|radioactive|spent fuel',
                re.I)
NOW = dt.datetime.now(dt.timezone.utc)

def get(path, params=None):
    p = {'api_key': KEY, 'format': 'json'}
    p.update(params or {})
    for a in range(3):
        try:
            r = requests.get(f'{B}{path}', params=p, headers=UA, timeout=40)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (a + 1)); continue
        except requests.RequestException:
            time.sleep(1.5)
    return None

def bill_item(b, tracked=False):
    no = f"{b['type']} {b['number']}".replace('HR', 'H.R.').replace('SRES', 'S.Res.').replace('S ', 'S. ') \
        if isinstance(b.get('type'), str) else b.get('number', '?')
    la = b.get('latestAction') or {}
    return {'billno': ('★ ' if tracked else '') + no,
            'title': (b.get('title') or '')[:160],
            'url': f"https://www.congress.gov/bill/{CONGRESS}th-congress/"
                   f"{'house-bill' if str(b.get('type','')).upper().startswith('H') else 'senate-bill'}/{b.get('number')}",
            'status': (la.get('text') or '')[:140],
            'status_date': la.get('actionDate') or '',
            'sponsor': '', 'tracked': tracked,
            'ts': (la.get('actionDate') or '') + 'T00:00:00+00:00'}

def main(out_dir=DATA):
    items, seen = [], set()
    for btype, num in TRACKED:
        d = get(f'/bill/{CONGRESS}/{btype}/{num}')
        time.sleep(0.6)
        if not d:
            continue
        b = d['bill']
        it = bill_item({'type': b['type'], 'number': b['number'], 'title': b['title'],
                        'latestAction': b.get('latestAction')}, tracked=True)
        sp = (b.get('sponsors') or [{}])[0]
        it['sponsor'] = sp.get('fullName', '')
        items.append(it); seen.add(it['billno'].replace('★ ', ''))
    # recent-action scan, newest first, keyword-matched titles
    fetched = 0
    for off in (0, 250):
        d = get(f'/bill/{CONGRESS}', {'limit': 250, 'offset': off, 'sort': 'updateDate+desc'})
        time.sleep(0.6)
        if not d:
            break
        for b in d.get('bills', []):
            if not KW.search(b.get('title') or ''):
                continue
            it = bill_item(b)
            key = it['billno']
            if key in seen:
                continue
            seen.add(key); items.append(it); fetched += 1
        if fetched >= 20:
            break
    items.sort(key=lambda i: (not i['tracked'], i['ts']), reverse=False)
    items.sort(key=lambda i: i['ts'], reverse=True)
    items.sort(key=lambda i: not i['tracked'])
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'congress': CONGRESS,
               'count': len(items), 'items': items[:24]}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(os.path.join(out_dir, 'congress.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'congress.js'), 'w').write('window.NIT_CONGRESS = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f'congress: {len(items)} bills ({sum(1 for i in items if i["tracked"])} tracked)')
    for i in items[:6]:
        print(f"   {i['billno']:14} {i['status_date']}  {i['title'][:56]}")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
