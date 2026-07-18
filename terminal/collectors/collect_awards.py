#!/usr/bin/env python3
"""Federal award history per peer -> data/awards.json/js (USASpending, keyless).

For every listed peer: grants (02-05) and contracts (A-D) since 2015 across all federal
agencies, matched by recipient-name variants with per-peer accept-regexes to kill noise
(e.g. FERMI ENERGY vs Fermi Research Alliance / Fermilab). Per peer: top rows by award
amount plus a summed total. Federal only — state instruments live in the Capital register.
Weekly refresh alongside the peers vitals. Sums are award amounts (obligations to date on
the award), not necessarily fully outlaid.
"""
import os, re, sys, json, time, datetime as dt
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
SINCE = '2015-01-01'
NOW = dt.datetime.now(dt.timezone.utc)
CAP_ROWS = 12

# ticker: (search variants, accept-regex on recipient name)
PEER_Q = {
 'OKLO': (['Oklo'], r'^OKLO'),
 'SMR':  (['NuScale'], r'NUSCALE'),
 'LEU':  (['Centrus', 'American Centrifuge'], r'CENTRUS|AMERICAN CENTRIFUGE'),
 'NNE':  (['Nano Nuclear'], r'NANO NUCLEAR'),
 'LTBR': (['Lightbridge'], r'^LIGHTBRIDGE'),
 'ASPI': (['ASP Isotopes'], r'ASP ISOTOPES'),
 'BWXT': (['BWXT', 'BWX Technologies', 'Nuclear Fuel Services'], r'BWXT|BWX TECH|NUCLEAR FUEL SERVICES'),
 'CCJ':  (['Cameco'], r'CAMECO'),
 'IMSR': (['Terrestrial Energy'], r'TERRESTRIAL ENERGY'),
 'NKLR': (['Terra Innovatum'], r'TERRA INNOVATUM'),
 'UEC':  (['Uranium Energy Corp'], r'URANIUM ENERGY'),
 'XE':   (['X-Energy', 'X Energy', 'TRISO-X'], r'^X[\s\-]?ENERGY|TRISO-X'),
 'FRMI': (['Fermi Energy', 'Fermi Inc'], r'^FERMI ENERGY|^FERMI,? INC'),
 'FISN': (['Deep Fission'], r'DEEP FISSION'),
 'HDRN': (['Hadron Energy'], r'^HADRON ENERGY'),
 'STDN': (['Standard Nuclear'], r'^STANDARD NUCLEAR'),
 'NHIC': ([], r'$^'),
}
GRANTS = ["02", "03", "04", "05"]
CONTRACTS = ["A", "B", "C", "D"]

def search(text, codes, sub=False):
    fields = (["Sub-Award ID", "Sub-Awardee Name", "Sub-Award Amount", "Sub-Award Date",
               "Prime Recipient Name", "Awarding Agency", "Awarding Sub Agency",
               "prime_award_generated_internal_id"] if sub else
              ["Award ID", "Recipient Name", "Start Date", "End Date",
               "Award Amount", "Awarding Agency", "Awarding Sub Agency",
               "Description", "generated_internal_id"])
    r = requests.post('https://api.usaspending.gov/api/v2/search/spending_by_award/',
        json={"filters": {"recipient_search_text": [text], "award_type_codes": codes,
                          "time_period": [{"start_date": SINCE, "end_date": "2030-12-31"}]},
              "fields": fields, "subawards": sub,
              "limit": 60, "order": "desc",
              "sort": "Sub-Award Amount" if sub else "Award Amount"},
        timeout=45)
    return r.json().get('results', []) if r.status_code == 200 else []

def main(out_dir=DATA):
    items, totals = {}, {}
    for t, (variants, acc) in PEER_Q.items():
        rx = re.compile(acc, re.I)
        seen, rows = set(), []
        for v in variants:
            for codes, fam in ((GRANTS, 'grant'), (CONTRACTS, 'contract')):
                time.sleep(0.35)
                for x in search(v, codes):
                    rid = x.get('generated_internal_id') or x.get('Award ID')
                    if rid in seen or not rx.search(x.get('Recipient Name') or ''):
                        continue
                    seen.add(rid)
                    amt = float(x.get('Award Amount') or 0)
                    if amt <= 0:
                        continue
                    rows.append({'id': x.get('Award ID'), 'fam': fam,
                                 'recip': (x.get('Recipient Name') or '')[:44],
                                 'agency': (x.get('Awarding Agency') or '')[:40],
                                 'sub': (x.get('Awarding Sub Agency') or '')[:44],
                                 'amt': round(amt),
                                 'start': x.get('Start Date') or '', 'end': x.get('End Date') or '',
                                 'desc': re.sub(r'\s+', ' ', (x.get('Description') or ''))[:150],
                                 'doe': 'ENERGY' in (x.get('Awarding Agency') or '').upper(),
                                 'url': f"https://www.usaspending.gov/award/{x.get('generated_internal_id')}" if x.get('generated_internal_id') else ''})
        # Subaward pass: money reaching the company as SUBRECIPIENT under someone else's
        # prime (e.g. NuScale under CFPP LLC). FSRS subaward reporting is patchy — treat
        # as a floor, not a census.
        for v in variants:
            for codes in (GRANTS, CONTRACTS):
                time.sleep(0.35)
                for x in search(v, codes, sub=True):
                    rid = 'sub:' + str(x.get('Sub-Award ID') or '') + str(x.get('Sub-Award Date') or '')
                    nm = x.get('Sub-Awardee Name') or ''
                    if rid in seen or not rx.search(nm):
                        continue
                    seen.add(rid)
                    amt = float(x.get('Sub-Award Amount') or 0)
                    if amt <= 0:
                        continue
                    pid = x.get('prime_award_generated_internal_id')
                    rows.append({'id': x.get('Sub-Award ID'), 'fam': 'sub',
                                 'recip': nm[:44],
                                 'agency': (x.get('Awarding Agency') or '')[:40],
                                 'sub': (x.get('Awarding Sub Agency') or '')[:44],
                                 'amt': round(amt),
                                 'start': x.get('Sub-Award Date') or '', 'end': '',
                                 'desc': 'Subaward under prime: ' + (x.get('Prime Recipient Name') or '?')[:80],
                                 'prime': (x.get('Prime Recipient Name') or '')[:44],
                                 'doe': 'ENERGY' in (x.get('Awarding Agency') or '').upper(),
                                 'url': f"https://www.usaspending.gov/award/{pid}" if pid else ''})
        rows.sort(key=lambda r: -r['amt'])
        totals[t] = {'sum': sum(r['amt'] for r in rows), 'n': len(rows),
                     'doe': sum(r['amt'] for r in rows if r['doe']),
                     'subs': sum(r['amt'] for r in rows if r['fam'] == 'sub')}
        items[t] = rows[:CAP_ROWS]
        print(f"  {t:5} {len(rows):3} awards  ${totals[t]['sum']/1e6:,.0f}M  (DOE ${totals[t]['doe']/1e6:,.0f}M)")
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'since': SINCE,
               'items': items, 'totals': totals,
               'caveats': 'USASpending federal awards since 2015: prime grants + contracts plus reported subawards, all agencies, matched by '
                          'recipient-name variants with per-peer accept filters. Amounts are award obligations to '
                          'date, not outlays. Federal only — state instruments live in the Capital register. '
                          'Subaward (FSRS) reporting is incomplete and lags — subaward sums are a floor. Money obligated to project vehicles (e.g. CFPP LLC) appears only via reported subawards, not under the company name. Subsidiary names may add or miss awards; totals are indicative.'}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(os.path.join(out_dir, 'awards.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'awards.js'), 'w').write('window.NIT_AWARDS = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f"awards -> awards.json/js")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
