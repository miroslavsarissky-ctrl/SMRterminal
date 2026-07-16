#!/usr/bin/env python3
"""Listed Peers collector: vital signs for every listed company in the Investors universe.

Sources, all free and primary: SEC XBRL companyfacts (cash, operating cash flow with proper
year-to-date quarter-differencing, revenue, shares outstanding -> dilution), EDGAR Form 4
filings netted over 90 days (open-market P buys vs S sells), and Yahoo's chart endpoint for
price, 30/90-day performance and a sparkline. Market cap = price x shares outstanding.

Caveats carried in the payload: Cameco is an IFRS/40-F filer so some US-GAAP tags are absent;
NewHold is a SPAC so 'cash' is trust assets and burn/runway do not apply; short interest is
deferred pending a FINRA API key (column reserved). Weekly refresh; vitals move on filings.
"""
import os, re, sys, json, time, datetime as dt
import requests

UA = {'User-Agent': 'newcleo-nuclear-intel-terminal/1.0 (market intelligence; contact: comms@newcleo.com)'}
YUA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
SLEEP = 0.13
NOW = dt.datetime.now(dt.timezone.utc)
INSIDER_CAP = int(os.environ.get('PEER_FORM4_CAP', '20'))

PEERS = [
    ('OKLO', '0001849056', 'Oklo'), ('SMR', '0001822966', 'NuScale Power'),
    ('LEU', '0001065059', 'Centrus Energy'), ('NNE', '0001923891', 'NANO Nuclear'),
    ('LTBR', '0001084554', 'Lightbridge'), ('ASPI', '0001921865', 'ASP Isotopes'),
    ('BWXT', '0001486957', 'BWX Technologies'), ('CCJ', '0001009001', 'Cameco'),
    ('IMSR', '0002019804', 'Terrestrial Energy'), ('NKLR', '0002067627', 'Terra Innovatum'),
    ('UEC', '0001334933', 'Uranium Energy'), ('XE', '0002088896', 'X-energy'),
    ('FRMI', '0002071778', 'Fermi'), ('FISN', '0001918102', 'Deep Fission'),
    ('HDRN', '0002023730', 'Hadron Energy'), ('NHIC', '0002043699', 'NewHold Investment III'),
]

def get(url, params=None, headers=UA, tries=3, timeout=45):
    for a in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503):
                time.sleep(0.7 * (a + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(0.7 * (a + 1))
    return None

def dur_rows(facts, ns, tag):
    """USD duration rows for a tag, deduped by (start,end), latest filing wins, sorted by end."""
    try:
        rows = facts['facts'][ns][tag]['units']['USD']
    except Exception:
        return []
    best = {}
    for r in rows:
        if not r.get('start') or not r.get('end'):
            continue
        k = (r['start'], r['end'])
        if k not in best or (r.get('filed') or '') > (best[k].get('filed') or ''):
            best[k] = r
    return sorted(best.values(), key=lambda r: (r['end'], r['start']))

def latest_quarter(rows):
    """Latest quarterly value from a duration series, differencing YTD figures when needed."""
    if not rows:
        return None, None
    last = rows[-1]
    span = (dt.date.fromisoformat(last['end']) - dt.date.fromisoformat(last['start'])).days
    if span <= 120:
        return last['val'], last['end']
    prevs = [r for r in rows if r['start'] == last['start'] and r['end'] < last['end']]
    if prevs:
        p = max(prevs, key=lambda r: r['end'])
        pspan = (dt.date.fromisoformat(last['end']) - dt.date.fromisoformat(p['end'])).days
        if pspan <= 120:
            return last['val'] - p['val'], last['end']
    return None, last['end']

def inst_latest(facts, ns, tag, unit='USD'):
    try:
        rows = [r for r in facts['facts'][ns][tag]['units'][unit] if r.get('end')]
        return sorted(rows, key=lambda r: (r['end'], r.get('filed') or ''))[-1]
    except Exception:
        return None

def vitals(cik):
    r = get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json')
    if r is None:
        return {}
    f = r.json()
    out = {}
    for ns in ('us-gaap', 'ifrs-full'):
        for tag in ('CashAndCashEquivalentsAtCarryingValue', 'CashAndCashEquivalents',
                    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'):
            c = inst_latest(f, ns, tag)
            if c:
                out['cash'], out['cash_date'] = c['val'], c['end']; break
        if 'cash' in out:
            break
    if 'cash' in out:                                   # add short-term investments to liquidity
        for tag in ('ShortTermInvestments', 'MarketableSecuritiesCurrent',
                    'AvailableForSaleSecuritiesDebtSecuritiesCurrent'):
            sti = inst_latest(f, 'us-gaap', tag)
            if sti and sti['end'] == out['cash_date']:
                out['cash'] += sti['val']; break
    if 'cash' not in out:
        t = inst_latest(f, 'us-gaap', 'AssetsHeldInTrustNoncurrent')
        if t:
            out['cash'], out['cash_date'], out['trust'] = t['val'], t['end'], True
    for ns in ('us-gaap', 'ifrs-full'):
        tag = 'NetCashProvidedByUsedInOperatingActivities' if ns == 'us-gaap' else 'CashFlowsFromUsedInOperatingActivities'
        q, qe = latest_quarter(dur_rows(f, ns, tag))
        if q is not None:
            out['ocf_q'], out['ocf_end'] = q, qe; break
    for tag in ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues',
                'RevenueFromContractWithCustomerIncludingAssessedTax', 'Revenue'):
        for ns in ('us-gaap', 'ifrs-full'):
            q, qe = latest_quarter(dur_rows(f, ns, tag))
            if q is not None:
                out['rev_q'] = q; break
        if 'rev_q' in out:
            break
    try:
        sh = sorted(f['facts']['dei']['EntityCommonStockSharesOutstanding']['units']['shares'],
                    key=lambda r: (r['end'], r.get('filed') or ''))
        out['sh'] = sh[-1]['val']
        prior = [r for r in sh if r['end'] < sh[-1]['end']]
        if prior:
            pv = prior[-1]['val']
            if pv:
                out['dil'] = (sh[-1]['val'] / pv - 1) * 100
    except Exception:
        pass
    return out

FORM4_ROW = re.compile(r'<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>', re.S)
def insiders(cik):
    """Net open-market insider flow over ~90 days: sum(P buys $) - sum(S sells $)."""
    cache = f'/tmp/peer_ins_{cik}.json'
    if os.path.exists(cache):
        return json.load(open(cache))
    r = get(f'https://data.sec.gov/submissions/CIK{cik}.json')
    if r is None:
        return {}
    rec = r.json()['filings']['recent']
    cutoff = (NOW - dt.timedelta(days=90)).strftime('%Y-%m-%d')
    idxs = [i for i, fm in enumerate(rec['form']) if fm == '4' and rec['filingDate'][i] >= cutoff][:INSIDER_CAP]
    buys = sells = nb = ns_ = 0
    for i in idxs:
        adsh = rec['accessionNumber'][i].replace('-', '')
        time.sleep(SLEEP)
        ix = get(f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh}/index.json')
        if ix is None:
            continue
        try:
            xmls = [it['name'] for it in ix.json()['directory']['item'] if it['name'].lower().endswith('.xml')]
        except Exception:
            continue
        if not xmls:
            continue
        time.sleep(SLEEP)
        doc = get(f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh}/{xmls[0]}')
        if doc is None:
            continue
        for row in FORM4_ROW.findall(doc.text):
            code = re.search(r'<transactionCode>([A-Z])</transactionCode>', row)
            shm = re.search(r'<transactionShares>\s*<value>([\d.]+)', row)
            prm = re.search(r'<transactionPricePerShare>\s*<value>([\d.]+)', row)
            if not code or not shm:
                continue
            usd = float(shm.group(1)) * (float(prm.group(1)) if prm else 0)
            if code.group(1) == 'P':
                buys += usd; nb += 1
            elif code.group(1) == 'S':
                sells += usd; ns_ += 1
    out = {'net': round(buys - sells), 'buys': nb, 'sells': ns_}
    json.dump(out, open(cache, 'w'))
    return out

def short_interest(t):
    """FINRA consolidated short interest (anonymous endpoint): latest settlement for ticker."""
    try:
        r = requests.post('https://api.finra.org/data/group/otcmarket/name/consolidatedshortinterest',
                          json={'limit': 2, 'compareFilters': [{'compareType': 'EQUAL',
                                'fieldName': 'symbolCode', 'fieldValue': t}]},
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                          timeout=30)
        if r.status_code != 200 or not r.json():
            return {}
        rows = sorted(r.json(), key=lambda x: x.get('settlementDate') or '')
        x = rows[-1]
        si = x.get('currentShortPositionQuantity')
        adv = x.get('averageDailyVolumeQuantity') or 0
        return {'si': si, 'si_prev': x.get('previousShortPositionQuantity'),
                'si_date': x.get('settlementDate'),
                'dtc': round(si / adv, 1) if si and adv else None}
    except Exception:
        return {}

EVENT_FORMS = {'8-K', '424B5', 'S-3', 'S-3/A', 'S-1', 'S-1/A', '10-Q', '10-K', 'DEF 14A', '425'}
def events(cik, days=150, cap=12):
    """Recent corporate events from EDGAR submissions: dilution instruments and disclosures."""
    r = get(f'https://data.sec.gov/submissions/CIK{cik}.json')
    if r is None:
        return []
    rec = r.json()['filings']['recent']
    cutoff = (NOW - dt.timedelta(days=days)).strftime('%Y-%m-%d')
    out = []
    for i in range(len(rec['form'])):
        fm, fd = rec['form'][i], rec['filingDate'][i]
        if fd < cutoff:
            break
        if fm not in EVENT_FORMS:
            continue
        adsh = rec['accessionNumber'][i].replace('-', '')
        out.append({'d': fd, 'f': fm,
                    'u': f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh}',
                    'dil': fm in ('424B5', 'S-3', 'S-3/A', 'S-1', 'S-1/A')})
        if len(out) >= cap:
            break
    return out

def chart(t):
    r = get('https://query1.finance.yahoo.com/v8/finance/chart/' + t,
            {'range': '3mo', 'interval': '1d'}, headers=YUA)
    if r is None:
        return {}
    try:
        d = r.json()['chart']['result'][0]
        closes = [c for c in d['indicators']['quote'][0]['close'] if c]
        px = d['meta'].get('regularMarketPrice') or closes[-1]
        step = max(1, len(closes) // 30)
        return {'px': round(px, 2),
                'chg30': round((closes[-1] / closes[-min(22, len(closes))] - 1) * 100, 1),
                'chg90': round((closes[-1] / closes[0] - 1) * 100, 1),
                'spark': [round(c, 2) for c in closes[::step]][-30:]}
    except Exception:
        return {}

def main(out_dir=DATA):
    items = []
    for t, cik, name in PEERS:
        v = vitals(cik); time.sleep(SLEEP)
        ins = insiders(cik)
        ch = chart(t); time.sleep(SLEEP)
        sh_i = short_interest(t); time.sleep(SLEEP)
        evs = events(cik); time.sleep(SLEEP)
        burn = -v['ocf_q'] if v.get('ocf_q') is not None and v['ocf_q'] < 0 else 0
        runway = round(v['cash'] / burn, 1) if v.get('cash') and burn > 0 else None
        mcap = round(ch['px'] * v['sh']) if ch.get('px') and v.get('sh') else None
        items.append({'t': t, 'name': name, 'cik': cik, 'px': ch.get('px'),
                      'chg30': ch.get('chg30'), 'chg90': ch.get('chg90'), 'spark': ch.get('spark', []),
                      'mcap': mcap, 'cash': v.get('cash'), 'cash_date': v.get('cash_date'),
                      'trust': v.get('trust', False), 'burn_q': round(burn) if burn else 0,
                      'runway_q': runway, 'rev_q': v.get('rev_q'), 'sh': v.get('sh'),
                      'dil_qoq': round(v['dil'], 1) if v.get('dil') is not None else None,
                      'ins_net': ins.get('net'), 'ins_b': ins.get('buys', 0), 'ins_s': ins.get('sells', 0),
                      'si': sh_i.get('si'), 'si_prev': sh_i.get('si_prev'), 'si_date': sh_i.get('si_date'),
                      'dtc': sh_i.get('dtc'),
                      'si_pct': round(sh_i['si'] / v['sh'] * 100, 1) if sh_i.get('si') and v.get('sh') else None,
                      'ocf_q': v.get('ocf_q'), 'events': evs,
                      'url': f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-&dateb=&owner=include&count=10'})
        print(f"  {t:5} px={ch.get('px')} cash={v.get('cash') and round(v['cash']/1e6)}M burn={round(burn/1e6)}M rw={runway} ins_net={ins.get('net')}")
    items.sort(key=lambda i: -(i['mcap'] or 0))
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'count': len(items), 'items': items,
               'caveats': 'SEC XBRL vitals (quarterly, filing-lagged) + EDGAR Form 4 net open-market flow 90d + '
                          'Yahoo prices. Cameco: IFRS/40-F filer, some fields absent. NHIC: SPAC — cash shown is '
                          'trust assets, burn/runway not applicable. Short interest: FINRA consolidated (bi-monthly settlement), %% is of shares outstanding, not float.'}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(os.path.join(out_dir, 'peers.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'peers.js'), 'w').write('window.NIT_PEERS = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f'peers: {len(items)} -> peers.json/js')

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
