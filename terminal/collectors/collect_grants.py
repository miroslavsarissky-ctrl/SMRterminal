#!/usr/bin/env python3
"""Grants.gov deterministic FOA candidates -> data/grants.json.

Hardens the Capital tab's discovery: instead of relying on the weekly Claude research to
find new funding opportunities, this pulls posted/forecasted opportunities matching nuclear
keywords straight from the Grants.gov Search2 API (no key), dedupes, and writes a candidate
list. The weekly research job injects these into the programmes prompt so Claude verifies
and classifies (cash / in-kind / allocated) rather than discovers. Keyless, primary-source.
"""
import os, re, sys, json, time, datetime as dt
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
KWS = ['nuclear', 'reactor', 'uranium', 'HALEU', 'isotope', 'fusion energy']
NOW = dt.datetime.now(dt.timezone.utc)
NUKE_CTX = re.compile(r'uranium|nuclear|HALEU|isotop|fission|fusion|fuel cycle|spent fuel|NRC|MOX|TRISO|plutonium|'
                      r'small modular|SMR|microreactor|atomic|radiat|radiolog|tritium|deuterium|'
                      r'(?:research|test|power|advanced|fast) reactor|reactor (?:fuel|core|pressure|vessel)', re.I)
NUKE_TITLE = NUKE_CTX
AMBIG_KW = re.compile(r'^(reactor|enrichment|fuel)$', re.I)
NOISE_KW = re.compile(r'primate|animal|veterinar|classroom|curricul|after-?school|bio-?reactor|photobio|algae', re.I)

def nuclear_relevant(kw, text):
    t = text or ''
    if NOISE_KW.search(t) and not NUKE_CTX.search(t):
        return False
    if AMBIG_KW.match(kw) and not NUKE_CTX.search(t):
        return False
    return True

def search(kw, rows=40):
    try:
        r = requests.post('https://api.grants.gov/v1/api/search2',
                          json={'keyword': kw, 'oppStatuses': 'forecasted|posted', 'rows': rows},
                          timeout=40)
        if r.status_code == 200:
            return r.json().get('data', {}).get('oppHits', []) or []
    except requests.RequestException:
        pass
    return []

def main(out_dir=DATA):
    seen, items = set(), []
    for kw in KWS:
        for h in search(kw):
            num = h.get('number') or ''
            if not num or num in seen:
                continue
            seen.add(num)
            agency = h.get('agencyCode') or h.get('agency') or ''
            title_ = h.get('title') or ''
            doe_ = str(agency).upper().startswith(('DOE', 'PAMS', 'DE-')) or num.upper().startswith('DE-')
            if not doe_ and not NUKE_TITLE.search(title_):
                continue                                     # nuclear in the title, or DOE-family — description-only matches are noise
            if not nuclear_relevant(kw, title_ + ' ' + str(agency)):
                continue
            items.append({'number': num,
                          'title': re.sub(r'&[a-z]+;', ' ', h.get('title') or '')[:150],
                          'agency': agency,
                          'status': h.get('oppStatus') or '',
                          'open': h.get('openDate') or '', 'close': h.get('closeDate') or '',
                          'doe': agency.upper().startswith(('DOE', 'PAMS', 'DE-')) or num.upper().startswith('DE-'),
                          'url': f'https://grants.gov/search-results-detail/{h.get("id")}' if h.get('id') else ''})
        time.sleep(0.4)
    items.sort(key=lambda i: (not i['doe'], i['close'] or '9999'))
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'count': len(items),
               'keywords': KWS, 'items': items}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(os.path.join(out_dir, 'grants.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'grants.js'), 'w').write('window.NIT_GRANTS = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f'grants: {len(items)} candidate opportunities ({sum(1 for i in items if i["doe"])} DOE-family)')
    for i in items[:6]:
        print(f"   {i['number']:22} close={i['close'] or '—':10} {i['title'][:52]}")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
