#!/usr/bin/env python3
"""Investors tab collector: institutional ownership across the SMR + fuel-cycle universe,
built from EDGAR 13F-HR filings (primary source).

Two tiers, because EDGAR's full-text index does not cover the giant info tables of the largest
managers, so they never appear in content searches:
  Tier A (whales): known mega-filers fetched directly via the submissions API; each hardcoded
  CIK is verified against the entity name at runtime and skipped on mismatch, never guessed.
  Tier B (long tail): EDGAR full-text search per company finds every indexed 13F whose
  information table mentions the issuer.
Every fetched table is parsed in a single pass against ALL universe issuers, so cross-holdings
are captured at no extra request cost. Option rows (putCall) are excluded so shares mean shares.
Values are in dollars (post-2023 13F rule), verified empirically against implied share price.

Honesty notes carried into the output: 13F reports long US-listed equity only (no shorts, no
private/PIPE stakes), values are as of quarter end and filed up to 45 days later, and
per-company coverage (parsed vs total filers) is recorded and shown rather than implying
completeness.

Optional Claude enrichment: with ANTHROPIC_API_KEY set, unclassified tail filers are sent to
Sonnet 5 with investors_brief.md to classify and profile.

Env knobs: INV_PAGES (FTS pages/company, default 12), INV_TABLES (tables fetched/company,
default 60), INV_START / INV_END (season window), INV_SEASON label, RESEARCH_MODEL.
"""
import os, re, sys, json, time, datetime as dt
import requests

UA = {'User-Agent': 'newcleo-nuclear-intel-terminal/1.0 (market intelligence; contact: comms@newcleo.com)'}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
PAGES = int(os.environ.get('INV_PAGES', '12'))
TABLES = int(os.environ.get('INV_TABLES', '60'))
START = os.environ.get('INV_START', '2026-04-01')
END = os.environ.get('INV_END', '2026-06-30')
SEASON = os.environ.get('INV_SEASON', 'Q1 2026')
SLEEP = 0.13
NOW = dt.datetime.now(dt.timezone.utc)

# INV_AUTO=1: compute the most recent complete 13F season from today's date
# (13F deadline is 45 days after quarter end: Feb 14, May 15, Aug 14, Nov 14).
if os.environ.get('INV_AUTO'):
    m, y = NOW.month, NOW.year
    if m in (2, 3, 4):
        SEASON, START = f'Q4 {y-1}', f'{y}-01-01'
    elif m in (5, 6, 7):
        SEASON, START = f'Q1 {y}', f'{y}-04-01'
    elif m in (8, 9, 10):
        SEASON, START = f'Q2 {y}', f'{y}-07-01'
    elif m in (11, 12):
        SEASON, START = f'Q3 {y}', f'{y}-10-01'
    else:                                  # January: Q3 of the prior year
        SEASON, START = f'Q3 {y-1}', f'{y-1}-10-01'
    END = NOW.strftime('%Y-%m-%d')

UNIVERSE = [
    ('OKLO', 'Oklo', 'OKLO', r'^OKLO'),
    ('SMR',  'NuScale Power', 'NUSCALE', r'NUSCALE'),
    ('LEU',  'Centrus Energy', 'CENTRUS', r'CENTRUS'),
    ('NNE',  'NANO Nuclear Energy', '"NANO NUCLEAR"', r'NANO\s*NUCLEAR'),
    ('LTBR', 'Lightbridge', 'LIGHTBRIDGE', r'LIGHTBRIDGE'),
    ('ASPI', 'ASP Isotopes', '"ASP ISOTOPES"', r'ASP\s*ISOTOP'),
    ('BWXT', 'BWX Technologies', '"BWX TECHNOLOGIES"', r'BWX\s*TECH'),
    ('CCJ',  'Cameco', 'CAMECO', r'CAMECO'),
    ('IMSR', 'Terrestrial Energy', '"TERRESTRIAL ENERGY"', r'TERRESTRIAL\s*ENERGY'),
    ('NKLR', 'Terra Innovatum', '"TERRA INNOVATUM"', r'TERRA\s*INNOVATUM'),
    ('UEC',  'Uranium Energy Corp', '"URANIUM ENERGY"', r'URANIUM\s+ENERGY'),
    ('NHIC', 'NewHold Investment Corp III (newcleo de-SPAC)', 'NEWHOLD', r'NEWHOLD'),
]

# Tier-A whales: (CIK, expected name substring). Verified at runtime; mismatch => skipped.
WHALES = [
    ('933478',  'VANGUARD'), ('2012383', 'BLACKROCK'), ('93751', 'STATE STREET'),
    ('315066',  'FMR'), ('1214717', 'GEODE'), ('1697748', 'ARK'),
    ('886982',  'GOLDMAN'), ('895421', 'MORGAN STANLEY'), ('19617', 'JPMORGAN'),
    ('70858',   'BANK OF AMER'), ('72971', 'WELLS FARGO'), ('73124', 'NORTHERN TRUST'),
    ('914208',  'INVESCO'), ('1273087', 'MILLENNIUM'), ('1423053', 'CITADEL'),
    ('80255',   'ROWE'), ('1607512', 'SEGRA'), ('1512920', 'SPROTT'),
]

MAJOR = re.compile(r'VANGUARD|BLACKROCK|STATE STREET|FMR|FIDELITY|GEODE|NORTHERN TRUST|INVESCO|'
                   r'ARK INVEST|SEGRA|ENCOMPASS|SPROTT|GLOBAL X|MIRAE|T\.?\s*ROWE|WELLINGTON|'
                   r'CAPITAL RESEARCH|BAILLIE|ALLIANCEBERNSTEIN|NUVEEN|MILLENNIUM|CITADEL|POINT72|'
                   r'TWO SIGMA|D\.?\s*E\.?\s*SHAW|RENAISSANCE|JANE STREET|SUSQUEHANNA|GOLDMAN|'
                   r'MORGAN STANLEY|JPMORGAN|BANK OF AMERICA|UBS|WELLS FARGO|SCHWAB|SABA|POLAR ASSET|'
                   r'MAGNETAR|BOOTHBAY|ARISTEIA|RIVERNORTH|GLAZER|FIR TREE|HIGHBRIDGE|VIRTU|HUDSON RIVER', re.I)

TYPE_RULES = [
    ('Nuclear / energy specialist', r'SEGRA|ENCOMPASS|SPROTT|ELECTRON CAPITAL|MASSIF|GOEHRING'),
    ('Passive / index',             r'VANGUARD|BLACKROCK|STATE STREET|GEODE|NORTHERN TRUST|DIMENSIONAL|'
                                    r'LEGAL \& GENERAL|GLOBAL X|MIRAE|CHARLES SCHWAB INVESTMENT|AMUNDI|XTRACKERS|DWS'),
    ('Hedge fund / multi-strategy', r'MILLENNIUM|CITADEL|POINT72|BALYASNY|TWO SIGMA|D\.?\s*E\.?\s*SHAW|'
                                    r'RENAISSANCE|MARSHALL WACE|SCHONFELD|EXODUSPOINT|WALLEYE|SQUAREPOINT|'
                                    r'SABA|POLAR ASSET|MAGNETAR|BOOTHBAY|ARISTEIA|RIVERNORTH|GLAZER|FIR TREE|HIGHBRIDGE'),
    ('Quant / market-maker',        r'JANE STREET|SUSQUEHANNA|VIRTU|HUDSON RIVER|FLOW TRADERS|OPTIVER|IMC |'
                                    r'WOLVERINE|SIMPLEX|GTS SECURITIES|PEAK6|CTC |JUMP '),
    ('Broker / wealth platform',    r'BANK OF AMERICA|JPMORGAN|GOLDMAN SACHS|MORGAN STANLEY|UBS|WELLS FARGO|'
                                    r'RAYMOND JAMES|AMERIPRISE|LPL FIN|STIFEL|ROYAL BANK|RBC|CIBC|CITIGROUP|'
                                    r'BARCLAYS|DEUTSCHE BANK|NATIONAL BANK|COMMONWEALTH EQUITY|CETERA|OSAIC|'
                                    r'BANK OF MONTREAL|TORONTO.?DOMINION|TD ASSET|SCOTIA|CANADIAN IMPERIAL|MANULIFE|SUN LIFE|MACKENZIE|IGM |ROYAL BANK OF CANADA'),
    ('Active manager',              r'FMR|FIDELITY|T\.?\s*ROWE|PRICE T ROWE|WELLINGTON|CAPITAL RESEARCH|BAILLIE|'
                                    r'ALLIANCEBERNSTEIN|NUVEEN|ARK INVEST|JANUS|FRANKLIN|INVESCO|NEUBERGER'),
]

PROFILES = {
    'VANGUARD': 'Index giant; positions track index inclusion rather than a nuclear view.',
    'BLACKROCK': 'Largest asset manager; predominantly index-driven exposure via iShares.',
    'STATE STREET': 'Index/ETF platform (SPDR); passive exposure.',
    'GEODE': 'Sub-adviser running Fidelity index funds; passive.',
    'FMR': 'Fidelity active complex; a real-money allocator worth direct IR engagement.',
    'ARK INVEST': 'Thematic active manager; public advocate of nuclear-adjacent innovation themes.',
    'SEGRA': 'Dedicated nuclear/uranium specialist fund; highest-signal holder in the sector.',
    'ENCOMPASS': 'Energy-focused hedge fund with a stated nuclear thesis.',
    'SPROTT': 'Uranium and critical-materials specialist (manages the physical uranium trust).',
    'GLOBAL X': 'ETF issuer; the URA uranium ETF makes it a structural sector holder.',
    'MIRAE': 'Parent of Global X; exposure largely via the URA ETF complex.',
    'MILLENNIUM': 'Multi-strategy pod shop; positions are trading books, not a house view.',
    'CITADEL': 'Multi-strategy and market-making complex; low IR signal.',
    'JANE STREET': 'Market-maker; inventory positions, not investment conviction.',
    'SUSQUEHANNA': 'Options market-maker; positions largely hedging flow.',
    'TWO SIGMA': 'Quant manager; systematic, not fundamental conviction.',
    'RENAISSANCE': 'Quant manager; systematic positions.',
    'GOLDMAN': 'Bank platform; mix of wealth, index and trading inventory.',
    'MORGAN STANLEY': 'Wealth platform aggregate; retail adviser flow.',
    'JPMORGAN': 'Bank platform; asset management plus trading inventory.',
    'BANK OF AMER': 'Merrill wealth platform aggregate.',
    'UBS': 'Wealth platform aggregate.',
    'WELLS FARGO': 'Wealth platform aggregate.',
    'SABA': 'Noted SPAC-arbitrage and credit fund; SPAC positions typically arb, likely to exit at close.',
    'POLAR ASSET': 'Toronto multi-strategy; prominent SPAC-arbitrage book.',
    'MAGNETAR': 'Multi-strategy with a large structured/SPAC book.',
    'BOOTHBAY': 'Fund-of-pods allocator; SPAC arb common.',
    'ARISTEIA': 'Relative-value credit/SPAC arbitrage.',
    'RIVERNORTH': 'Closed-end fund and SPAC arbitrage specialist.',
    'GLAZER': 'SPAC-arbitrage specialist.',
    'ROWE': 'Active growth manager; genuine fundamental allocator.',
    'WELLINGTON': 'Large active manager; fundamental allocator.',
    'BAILLIE': 'Long-horizon growth investor; high-conviction fundamental style.',
    'INVESCO': 'Asset manager; mix of active funds and passive ETFs (QQQ complex).',
    'NORTHERN TRUST': 'Custody and index platform; passive exposure.',
}

def classify(name):
    for t, pat in TYPE_RULES:
        if re.search(pat, name, re.I):
            return t
    return ''

def profile_for(name):
    up = name.upper()
    for key, p in PROFILES.items():
        if key in up:
            return p
    return ''

def get(url, params=None, timeout=60, tries=3):
    """SEC-polite GET with backoff; EDGAR FTS 500s transiently under load."""
    for a in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503):
                time.sleep(0.7 * (a + 1)); continue
            return r
        except requests.RequestException:
            time.sleep(0.7 * (a + 1))
    return None

def fts(query, page):
    r = get('https://efts.sec.gov/LATEST/search-index',
            {'q': query, 'forms': '13F-HR', 'startdt': START, 'enddt': END, 'from': page * 10})
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None

HAS_TABLE = re.compile(r'<(?:\w+:)?(?:infoTable|informationTable)\b', re.I)
ROW = re.compile(r'<(?:\w+:)?infoTable\b[^>]*>(.*?)</(?:\w+:)?infoTable>', re.S | re.I)
ISSUERS = [(t, re.compile(rx, re.I)) for t, _, _, rx in UNIVERSE]

def fetch_table(cik, adsh, filename):
    base = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace("-", "")}'
    if filename.lower().endswith('.xml'):
        r = get(f'{base}/{filename}')
        if r is not None and r.status_code == 200 and HAS_TABLE.search(r.text):
            return r.text
    r = get(f'{base}/index.json')
    if r is None or r.status_code != 200:
        return ''
    try:
        items = r.json()['directory']['item']
    except Exception:
        return ''
    xmls = sorted([i for i in items if i['name'].lower().endswith('.xml')
                   and 'primary_doc' not in i['name'].lower()],
                  key=lambda i: -int(i.get('size') or 0))
    for c in xmls[:2]:
        time.sleep(SLEEP)
        r = get(f"{base}/{c['name']}")
        if r is not None and r.status_code == 200 and HAS_TABLE.search(r.text):
            return r.text
    return ''

def parse_positions(xml):
    """Single pass over the filing: sum non-option rows for EVERY universe issuer."""
    out = {}
    for row in ROW.findall(xml):
        m = re.search(r'<(?:\w+:)?nameOfIssuer>([^<]+)', row)
        if not m:
            continue
        nm = m.group(1).strip()
        tick = next((t for t, rx in ISSUERS if rx.search(nm)), None)
        if not tick or re.search(r'<(?:\w+:)?putCall>', row):
            continue
        mv = re.search(r'<(?:\w+:)?value>(\d+)', row)
        ms = re.search(r'<(?:\w+:)?sshPrnamt>(\d+)', row)
        v, s = out.get(tick, (0, 0))
        out[tick] = (v + (int(mv.group(1)) if mv else 0), s + (int(ms.group(1)) if ms else 0))
    return {t: vs for t, vs in out.items() if vs[0] > 0}

def add_positions(investors, cik, name, fdate, posmap):
    inv = investors.setdefault(cik, {'cik': cik, 'name': name, 'positions': {}, 'filed': fdate})
    for t, (v, sh) in posmap.items():
        cur = inv['positions'].get(t)
        if cur is None or fdate >= cur['filed']:
            inv['positions'][t] = {'v': v, 'sh': sh, 'filed': fdate}
    inv['filed'] = max(inv['filed'], fdate)

def whale_pass(investors):
    for cik, expect in WHALES:
        cik10 = cik.zfill(10)
        r = get(f'https://data.sec.gov/submissions/CIK{cik10}.json')
        if r is None or r.status_code != 200:
            print(f'  ! whale {expect}: submissions unavailable'); continue
        sub = r.json()
        if expect not in (sub.get('name') or '').upper():
            print(f'  ! whale CIK {cik}: {sub.get("name")!r} != expected {expect}; skipped'); continue
        rec = sub['filings']['recent']
        cand = [i for i, f in enumerate(rec['form']) if f in ('13F-HR', '13F-HR/A')
                and START <= rec['filingDate'][i] <= END]
        if not cand:
            print(f'  - whale {sub["name"][:30]}: no 13F in window'); continue
        best, bdate = {}, ''
        for ix in cand[:2]:                       # some whales file twice in a window; keep the richer book
            time.sleep(SLEEP)
            xml = fetch_table(int(cik), rec['accessionNumber'][ix], 'x.nomatch')
            p = parse_positions(xml) if xml else {}
            if sum(v for v, _ in p.values()) > sum(v for v, _ in best.values()):
                best, bdate = p, rec['filingDate'][ix]
        pos, ix = best, cand[0]
        if bdate:
            rec['filingDate'][ix] = bdate
        if pos:
            add_positions(investors, cik10, sub['name'].title(), rec['filingDate'][ix], pos)
            print(f'  whale {sub["name"][:30]:32} ' +
                  ', '.join(f'{t} ${v/1e6:.0f}M' for t, (v, s) in sorted(pos.items(), key=lambda kv: -kv[1][0])))
        else:
            print(f'  - whale {sub["name"][:30]}: no universe holdings')
        time.sleep(SLEEP)

def collect():
    investors, meta = {}, []
    print('-- tier A: whales (submissions API) --')
    whale_pass(investors)
    print('-- tier B: long tail (full-text search) --')
    for ticker, cname, q, issuer_re in UNIVERSE:
        found, total = {}, 0
        for p in range(PAGES):
            d = fts(q, p)
            if d is None:
                print(f'  ! fts {ticker} p{p} failed'); break
            total = d['hits']['total']['value']
            hits = d['hits']['hits']
            if not hits:
                break
            for h in hits:
                s = h['_source']
                cik = (s.get('ciks') or [None])[0]
                if not cik:
                    continue
                rec = found.get(cik)
                if rec is None or s['file_date'] > rec['file_date']:
                    found[cik] = {'cik': cik,
                                  'name': re.sub(r'\s*\(CIK.*?\)\s*', '', (s.get('display_names') or ['?'])[0]).strip(),
                                  'adsh': s['adsh'], 'file_date': s['file_date'], 'fn': h['_id'].split(':')[1]}
            time.sleep(SLEEP)
        fresh = [f for f in found.values() if f['cik'].zfill(10) not in investors and f['cik'] not in investors]
        order = sorted(fresh, key=lambda f: (0 if MAJOR.search(f['name']) else 1, f['name']))
        parsed = matched = 0
        for f in order[:TABLES]:
            time.sleep(SLEEP)
            xml = fetch_table(f['cik'], f['adsh'], f['fn'])
            if not xml:
                continue
            parsed += 1
            pos = parse_positions(xml)
            if not pos:
                continue
            matched += 1
            add_positions(investors, f['cik'], f['name'], f['file_date'], pos)
        meta.append({'ticker': ticker, 'company': cname, 'fts_filers': total, 'parsed': parsed, 'holders_found': matched})
        print(f'  {ticker:5} tail-filers={total:4} parsed={parsed:3} matched={matched:3}')
    return investors, meta

def normalise_units(investors):
    """Some filers still report 13F values in thousands despite the post-2023 dollar rule
    (T Rowe, for one). Self-calibrating fix: per ticker, take the median implied $/share across
    all filings; any filing whose implied price is ~1000x below the median gets scaled up."""
    import statistics
    med = {}
    for t, _, _, _ in UNIVERSE:
        prices = [p['v'] / p['sh'] for inv in investors.values()
                  for tk, p in inv['positions'].items() if tk == t and p['sh'] > 0 and p['v'] > 0]
        if len(prices) >= 5:
            med[t] = statistics.median(prices)
    fixed = 0
    for inv in investors.values():
        for t, p in inv['positions'].items():
            if t in med and p['sh'] > 0 and p['v'] > 0:
                ratio = med[t] / (p['v'] / p['sh'])
                if 300 < ratio < 3000:
                    p['v'] *= 1000; fixed += 1
    if fixed:
        print(f'  unit-normalised {fixed} positions (thousands -> dollars)')

def enrich_via_claude(items):
    key = os.environ.get('ANTHROPIC_API_KEY')
    todo = [i['name'] for i in items if not i['type']][:120]
    if not key or not todo:
        return 0
    try:
        import anthropic
        brief = open(os.path.join(HERE, 'investors_brief.md')).read()
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=os.environ.get('RESEARCH_MODEL', 'claude-sonnet-5'), max_tokens=8000,
            system=brief,
            messages=[{'role': 'user', 'content':
                       'Classify these 13F filers. Return ONLY a JSON object mapping each name to '
                       '{"type": one of ["Nuclear / energy specialist","Passive / index",'
                       '"Hedge fund / multi-strategy","Quant / market-maker","Broker / wealth platform",'
                       '"Active manager","Strategic / corporate","Sovereign / pension"], '
                       '"profile": one short sentence}. Names:\n' + json.dumps(todo)}],
            tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 8}])
        txt = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
        m = re.search(r'\{.*\}', txt, re.S)
        mapping = json.loads(m.group(0)) if m else {}
        n = 0
        for i in items:
            got = mapping.get(i['name'])
            if got and not i['type']:
                i['type'] = got.get('type', '')
                i['profile'] = i['profile'] or got.get('profile', '')
                n += 1
        return n
    except Exception as ex:
        print('  ! enrich:', ex)
        return 0

def main(out_dir=DATA):
    print(f'collect_investors: season {SEASON} ({START}..{END}) pages={PAGES} tables={TABLES}')
    investors, meta = collect()
    normalise_units(investors)
    items = []
    for inv in investors.values():
        total = sum(p['v'] for p in inv['positions'].values())
        items.append({'cik': inv['cik'], 'name': inv['name'], 'type': classify(inv['name']),
                      'profile': profile_for(inv['name']), 'total': total,
                      'names_held': len(inv['positions']), 'nhic': 'NHIC' in inv['positions'],
                      'positions': [{'t': t, 'v': p['v'], 'sh': p['sh']}
                                    for t, p in sorted(inv['positions'].items(), key=lambda kv: -kv[1]['v'])],
                      'filed': inv['filed'],
                      'url': f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={inv["cik"]}&type=13F&dateb=&owner=include&count=10'})
    n = enrich_via_claude(items)
    if n:
        print(f'  enriched via Claude: {n}')
    items.sort(key=lambda i: -i['total'])
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'season': SEASON,
               'window': {'start': START, 'end': END}, 'count': len(items),
               'universe': meta, 'items': items,
               'caveats': '13F-HR long US-listed equity only; excludes shorts, option rows, private and PIPE '
                          'stakes; values as of quarter end, filed up to 45 days later. Vanguard restructured its 13F reporting across multiple entities in Q1 2026; Vanguard Fiduciary Trust is included, full complex coverage is being tracked.'}
    os.makedirs(out_dir, exist_ok=True)
    prev_p = os.path.join(out_dir, 'investors.json')
    if os.path.exists(prev_p):
        try:
            prev_n = json.load(open(prev_p)).get('count', 0)
        except Exception:
            prev_n = 0
        if prev_n and len(items) < 0.4 * prev_n:
            print(f'  !! refusing to write: {len(items)} investors is under 40% of the existing '
                  f'{prev_n} (looks like a partial/failed run, not real churn); keeping current data')
            return
    json.dump(payload, open(os.path.join(out_dir, 'investors.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'investors.js'), 'w').write('window.NIT_INV = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f'investors: {len(items)} across {len(meta)} companies -> investors.json/js')

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
