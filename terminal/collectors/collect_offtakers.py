#!/usr/bin/env python3
"""Offtakers collector: the demand-side mirror of the Investors tab.

Industrial layer (primary source): EPA Greenhouse Gas Reporting Program. The annual bulk
summary spreadsheet (epa.gov/ghgreporting/data-sets, 2023_data_summary_spreadsheets.zip)
supplies every reporting facility with name, NAICS, coordinates and total CO2e in one file;
parent companies are joined per NAICS from the fast Envirofacts dimension endpoint. Latest
complete reporting year RY2023; RY2024 publishes ~Oct 2026 -> annual refresh re-downloads.

Energy estimation (order-of-magnitude, factors editable below, shown in the UI method note):
reported CO2e is converted to estimated electricity and heat demand per sector:
  cement     clinker t = CO2e*0.55/0.51 (process share / calcination factor);
             heat = 3.4 GJ/t clinker; electricity = 0.105 MWh/t cement (cement = clinker/0.9)
  steel      heat: fuel GJ = CO2e/0.080 t-per-GJ (blended coal/gas);
             electricity = 0.25 MWh per tCO2e (blended integrated/EAF heuristic)
  aluminium  Al t = CO2e/1.7 (anode+process); electricity = 14.2 MWh/t Al; heat minor (gas EF)
  chemicals  heat: fuel GJ = CO2e/0.0561 (natural-gas EF); electricity = 0.10 MWh per tCO2e
  data centres  GWh = announced MW * 8.76 * 0.80 load factor (curated layer; no federal registry)
These are screening estimates for ranking and sizing, not metered data; reported CO2e is
always shown alongside.

Data-centre layer: curated seed of hyperscalers/colos with announced campus MW and known
nuclear procurement signals; grid interconnection queues (ERCOT monthly large-load report,
NYISO Gold Book) are the enrichment path.
"""
import os, re, sys, json, time, datetime as dt
import requests

UA = {'User-Agent': 'newcleo-nuclear-intel-terminal/1.0 (market intelligence; contact: comms@newcleo.com)'}
B = 'https://data.epa.gov/efservice'
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
YEAR = os.environ.get('OFF_YEAR', '2023')
SLEEP = 0.12
NOW = dt.datetime.now(dt.timezone.utc)

SECTORS = {                       # equal weighting: ranking is purely by estimated GWh
    'STEEL':     ['331110'],
    'CEMENT':    ['327310'],
    'ALUMINIUM': ['331313'],
    'CHEMICALS': ['325311', '325110', '325120', '325199'],
}
SECTOR_COLOR = {'STEEL': '#9DB2C4', 'CEMENT': '#C9A26B', 'CHEMICALS': '#5CB88A',
                'ALUMINIUM': '#7FB3D5', 'DATA CENTERS': '#F0782E'}

def est_gwh(sector, co2e):
    """(gwh_electric, gwh_thermal) from tCO2e. Factors documented in the module docstring."""
    if sector == 'CEMENT':
        clinker = co2e * 0.55 / 0.51
        return (clinker / 0.9) * 0.105 / 1000, clinker * 3.4 / 3600
    if sector == 'STEEL':
        return co2e * 0.25 / 1000, (co2e / 0.080) / 3600
    if sector == 'ALUMINIUM':
        t_al = co2e / 1.7
        return t_al * 14.2 / 1000, (co2e / 0.0946) / 3600 * 0.15
    if sector == 'CHEMICALS':
        return co2e * 0.10 / 1000, (co2e / 0.0561) / 3600
    return 0.0, 0.0

def get(url, tries=3):
    for a in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                d = r.json()
                return d if isinstance(d, list) else []
        except Exception:
            pass
        time.sleep(0.6 * (a + 1))
    return []

def parent_of(raw):
    """'HOLCIM US INC (100%); X (0%)' -> 'Holcim Us Inc' (primary owner)."""
    if not raw:
        return 'Independent / undisclosed'
    first = re.split(r';', raw)[0]
    first = re.sub(r'\s*\([^)]*%?\)\s*', '', first).strip()
    return first.title() if first else 'Independent / undisclosed'

# Curated data-centre layer: announced campus MW + nuclear procurement signals (public, pre-build).
DC_SEED = [
    ('Amazon (AWS)', 960, 'Susquehanna nuclear-adjacent campus (up to 960 MW ISA); X-energy SMR development agreement'),
    ('Microsoft', None, '20-yr PPA to restart Three Mile Island Unit 1 (Crane, 835 MW)'),
    ('Google', None, 'Kairos Power master agreement (500 MW by 2035); nuclear PPAs'),
    ('Meta', None, '20-yr PPA with Constellation for Clinton (1.1 GW) from 2027'),
    ('OpenAI / Stargate (Crusoe)', 1200, 'Abilene TX campus, ~1.2 GW announced'),
    ('Oracle', None, 'Gigawatt-scale AI campus announced, SMR-powered design stated'),
    ('xAI', 300, ''),
    ('Switch', None, 'Oklo master agreement framework (up to 12 GW long-term)'),
    ('Equinix', None, 'Oklo pre-order agreement (500 MW); Radiant microreactor order'),
    ('CoreWeave', None, ''),
    ('Vantage Data Centers', None, ''),
    ('QTS (Blackstone)', None, ''),
    ('Digital Realty', None, ''),
    ('Aligned Data Centers', None, ''),
    ('Compass Datacenters', None, ''),
    ('STACK Infrastructure', None, ''),
    ('CyrusOne', None, ''),
    ('EdgeConneX', None, ''),
    ('NTT Global Data Centers', None, ''),
    ('Iron Mountain Data Centers', None, ''),
]

ZIP_URL = 'https://www.epa.gov/system/files/other-files/2024-10/2023_data_summary_spreadsheets.zip'

def load_bulk(year):
    """Download (once) and parse the GHGRP bulk sheet -> list of facility dicts."""
    import io, zipfile
    import pandas as pd
    cache = f'/tmp/ghgp_{year}.xlsx'
    if not os.path.exists(cache):
        r = requests.get(ZIP_URL, headers=UA, timeout=180)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = [n for n in z.namelist() if f'ghgp_data_{year}' in n][0]
        open(cache, 'wb').write(z.read(name))
    df = pd.read_excel(cache, sheet_name='Direct Point Emitters', header=3)
    df = df.rename(columns={'Facility Id': 'fid', 'Facility Name': 'name', 'State': 'st',
                            'Latitude': 'lat', 'Longitude': 'lon',
                            'Primary NAICS Code': 'naics',
                            'Total reported direct emissions': 'co2e'})
    df['naics'] = df['naics'].astype('Int64').astype(str)
    return df[['fid', 'name', 'st', 'lat', 'lon', 'naics', 'co2e']].to_dict('records')

def parent_map(naics):
    """facility_id -> parent string, from the (fast) Envirofacts dimension endpoint."""
    out = {}
    for f in get(f'{B}/pub_dim_facility/naics_code/{naics}/year/{YEAR}/JSON'):
        if f.get('facility_id'):
            out[int(f['facility_id'])] = f.get('parent_company') or ''
    return out

def collect_industrial():
    rows = load_bulk(YEAR)
    parents, meta = {}, []
    for sector, naics_list in SECTORS.items():
        fac_count = 0
        pmap = {}
        for n in naics_list:
            time.sleep(SLEEP)
            pmap.update(parent_map(n))
        for f in rows:
            if f['naics'] not in naics_list:
                continue
            co2e = float(f['co2e'] or 0)
            if co2e <= 0:
                continue
            fac_count += 1
            e, th = est_gwh(sector, co2e)
            pname = parent_of(pmap.get(int(f['fid']), ''))
            p = parents.setdefault((pname, sector), {
                'name': pname, 'sector': sector, 'co2e': 0.0, 'gwh_e': 0.0, 'gwh_th': 0.0,
                'sites': [], 'states': set()})
            p['co2e'] += co2e; p['gwh_e'] += e; p['gwh_th'] += th
            st = f['st'] or ''
            p['states'].add(st)
            p['sites'].append({'n': str(f['name'] or '?').title()[:48], 'st': st,
                               'lat': round(float(f['lat'] or 0), 3), 'lon': round(float(f['lon'] or 0), 3),
                               'co2e': round(co2e), 'gwh': round(e + th, 1)})
        meta.append({'sector': sector, 'facilities': fac_count})
        print(f'  {sector:10} facilities={fac_count}')
    return parents, meta

def main(out_dir=DATA):
    print(f'collect_offtakers: GHGRP RY{YEAR}')
    parents, meta = collect_industrial()
    items = []
    for p in parents.values():
        p['sites'].sort(key=lambda s: -s['gwh'])
        items.append({'name': p['name'], 'sector': p['sector'],
                      'gwh': round(p['gwh_e'] + p['gwh_th'], 1),
                      'gwh_e': round(p['gwh_e'], 1), 'gwh_th': round(p['gwh_th'], 1),
                      'co2e': round(p['co2e']), 'n_sites': len(p['sites']),
                      'states': sorted(x for x in p['states'] if x),
                      'sites': p['sites'][:8], 'note': '', 'signals': []})
    for name, mw, signal in DC_SEED:
        gwh = round(mw * 8.76 * 0.80, 0) if mw else None
        items.append({'name': name, 'sector': 'DATA CENTERS', 'gwh': gwh, 'gwh_e': gwh, 'gwh_th': 0,
                      'co2e': None, 'n_sites': None, 'states': [], 'sites': [], 'mw': mw,
                      'note': ('curated; announced campus MW' if mw else 'curated; MW not disclosed'),
                      'signals': [signal] if signal else []})
    industrial = sorted([i for i in items if i['sector'] != 'DATA CENTERS'],
                        key=lambda i: -(i['gwh'] or 0))[:100]
    dcs = sorted([i for i in items if i['sector'] == 'DATA CENTERS'],
                 key=lambda i: -(i['gwh'] or 0))
    items = sorted(industrial + dcs, key=lambda i: -(i['gwh'] or 0))
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'year': YEAR,
               'sector_colors': SECTOR_COLOR, 'universe': meta,
               'count': len(items), 'items': items,
               'method': ('Industrial demand estimated from EPA GHGRP reported CO2e (RY' + YEAR + ') using '
                          'sector factors: cement 3.4 GJ/t clinker heat + 0.105 MWh/t power; steel blended '
                          '0.080 tCO2/GJ fuel + 0.25 MWh/tCO2e power; aluminium 14.2 MWh/t Al; chemicals '
                          'gas-EF heat + 0.10 MWh/tCO2e power. Data centres: curated, announced MW x 8.76 x '
                          '0.80. Screening estimates, not metered data; reported CO2e shown alongside.')}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(os.path.join(out_dir, 'offtakers.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'offtakers.js'), 'w').write('window.NIT_OFF = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f'offtakers: {len(items)} entries -> offtakers.json/js')
    for i in items[:8]:
        print(f"   {i['name'][:34]:36} {i['sector']:12} {i['gwh'] or '—'} GWh/yr")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
