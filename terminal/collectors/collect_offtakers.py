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
    'STEEL':           ['331110', '324199'],                        # mills + merchant coke
    'CEMENT':          ['327310'],
    'ALUMINIUM':       ['331313'],
    'CHEMICALS':       ['325311', '325110', '325120', '325199', '325180', '325211'],
    'REFINING':        ['324110'],
    'PULP & PAPER':    ['322110', '322120', '322121', '322122', '322130'],
    'FOOD & BIOFUELS': ['325193', '311221', '311313', '311224'],
    'MINERALS':        ['327410', '327211', '327212', '327213'],    # lime + glass
    'SEMICONDUCTORS':  ['334413'],
}
SECTOR_COLOR = {'STEEL': '#9DB2C4', 'CEMENT': '#C9A26B', 'CHEMICALS': '#5CB88A',
                'ALUMINIUM': '#7FB3D5', 'REFINING': '#C7625C', 'PULP & PAPER': '#7D9B4E',
                'FOOD & BIOFUELS': '#D9B23F', 'MINERALS': '#B08FC9',
                'SEMICONDUCTORS': '#D98AC2', 'DATA CENTERS': '#F0782E'}

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
    if sector == 'REFINING':
        return co2e * 0.05 / 1000, (co2e / 0.070) / 3600
    if sector == 'PULP & PAPER':                       # fossil share only; biomass steam invisible
        return co2e * 0.15 / 1000, (co2e / 0.0561) / 3600
    if sector == 'FOOD & BIOFUELS':
        return co2e * 0.08 / 1000, (co2e / 0.0561) / 3600
    if sector == 'MINERALS':                           # lime kilns + glass furnaces, blended
        return co2e * 0.06 / 1000, (co2e * 0.45 / 0.085) / 3600
    return 0.0, 0.0                                    # SEMICONDUCTORS: handled post-aggregation

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
    ('Amazon (AWS)', 960, [{'t': 'Susquehanna existing-plant campus, up to 960 MW ISA', 'cls': 'EF', 'gw': 0.96},
                           {'t': 'X-energy SMR development agreement (fleet framework)', 'cls': 'NB', 'tier': 5, 'gw': None}]),
    ('Microsoft', None, [{'t': '20-yr PPA to restart Three Mile Island Unit 1 (Crane)', 'cls': 'EF', 'gw': 0.835}]),
    ('Google', None, [{'t': 'Kairos Power master agreement — 500 MW by 2035', 'cls': 'NB', 'tier': 5, 'gw': 0.5}]),
    ('Meta', None, [{'t': '20-yr PPA with Constellation for Clinton, from 2027', 'cls': 'EF', 'gw': 1.1}]),
    ('OpenAI / Stargate (Crusoe)', 1200, [{'t': 'Abilene TX campus announced — load only, no nuclear procurement stated', 'cls': 'LOAD', 'gw': 1.2}]),
    ('Oracle', None, [{'t': 'Gigawatt-scale AI campus announced, SMR-powered design stated', 'cls': 'NB', 'tier': 5, 'gw': 1.0}]),
    ('xAI', 300, ''),
    ('Switch', None, [{'t': 'Oklo master agreement framework — long-term ceiling', 'cls': 'NB', 'tier': 5, 'gw': 12, 'cap': True}]),
    ('Equinix', None, [{'t': 'Oklo pre-order agreement (500 MW); Radiant microreactor order', 'cls': 'NB', 'tier': 5, 'gw': 0.5}]),
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

FUEL_EF = {'gas': 0.0561, 'coal': 0.0946, 'pet': 0.0733, 'oth': 0.080}   # tCO2 per GJ
BIO_EF = 0.112
FUEL_BUCKET = {'Natural Gas': 'gas', 'Fuel Gas': 'gas', 'Coal': 'coal',
               'Petroleum Products': 'pet'}
UNITS_ZIP = 'https://www.epa.gov/system/files/other-files/2024-10/emissions_by_unit_and_fuel_type_c_d_aa.zip'

def load_units(year):
    """Per-facility unit layer from EPA's unit/fuel file: rated heat capacity (MWth),
    fossil combustion CO2 split by fuel bucket, and biogenic CO2. Cached as JSON after
    the first parse because the xlsb read is slow."""
    cache = f'/tmp/off_units_{year}.json'
    if os.path.exists(cache):
        return {int(k): v for k, v in json.load(open(cache)).items()}
    import io, zipfile
    import pandas as pd
    xp = f'/tmp/off_units_{year}.xlsb'
    if not os.path.exists(xp):
        r = requests.get(UNITS_ZIP, headers=UA, timeout=240)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        open(xp, 'wb').write(z.read([n for n in z.namelist() if n.endswith('.xlsb')][0]))
    def sheet(name):
        raw = pd.read_excel(xp, sheet_name=name, engine='pyxlsb', header=None, nrows=10)
        hdr = [i for i in range(10) if raw.iloc[i].astype(str).str.contains('Facility Id', na=False).any()][0]
        df = pd.read_excel(xp, sheet_name=name, engine='pyxlsb', header=hdr)
        df.columns = [str(c).strip() for c in df.columns]
        return df[df['Reporting Year'] == int(year)]
    fu = sheet('FUEL_DATA')
    fmap = {}
    for _, r in fu.iterrows():
        b = FUEL_BUCKET.get(str(r['General Fuel Type']).strip(), 'oth')
        fmap.setdefault((int(r['Facility Id']), str(r['Unit Name'])), set()).add(b)
    un = sheet('UNIT_DATA')
    capc = [c for c in un.columns if 'Heat Input' in c][0]
    co2c = [c for c in un.columns if 'non-biogenic' in c][0]
    bioc = [c for c in un.columns if 'Biogenic' in c][0]
    out = {}
    for _, r in un.iterrows():
        fid = int(r['Facility Id'])
        d = out.setdefault(fid, {'mwth': 0.0, 'f': {'gas': 0, 'coal': 0, 'pet': 0, 'oth': 0}, 'bio': 0.0})
        cap = pd.to_numeric(pd.Series([r[capc]]), errors='coerce').iloc[0]
        if cap == cap:
            d['mwth'] += float(cap) * 0.293
        co2 = pd.to_numeric(pd.Series([r[co2c]]), errors='coerce').iloc[0]
        if co2 == co2 and co2 > 0:
            buckets = fmap.get((fid, str(r['Unit Name']))) or {'oth'}
            for b in buckets:
                d['f'][b] += float(co2) / len(buckets)
        bio = pd.to_numeric(pd.Series([r[bioc]]), errors='coerce').iloc[0]
        if bio == bio and bio > 0:
            d['bio'] += float(bio)
    json.dump(out, open(cache, 'w'))
    print(f'  unit layer: {len(out)} facilities cached')
    return out

def parent_map(naics):
    """facility_id -> parent string, from the (fast) Envirofacts dimension endpoint."""
    out = {}
    for f in get(f'{B}/pub_dim_facility/naics_code/{naics}/year/{YEAR}/JSON'):
        if f.get('facility_id'):
            out[int(f['facility_id'])] = f.get('parent_company') or ''
    return out

# Signals are structured against the Breakthrough Institute order-book framing (Jul-2026):
# cls NB = new-build pipeline with BTI maturity tier (1 build … 5 fleet-level signal);
# cls EF = existing-fleet deal (PPA / restart / uprate — BTI tracks 8.3 GW of these separately);
# cls LOAD = announced campus load with no nuclear procurement stated. gw is announced size,
# '≤' ceilings marked with cap=True. Source: thebreakthrough.org, "America's Nuclear Order
# Book Is Large and Growing" (74 GW pipeline; ~580 TWh/yr if fully built at 90% CF).
INDUSTRIAL_SIGNALS = [
    ('DOW',   {'t': 'X-energy Xe-100 at Seadrift, TX — NRC construction permit application docketed',
               'cls': 'NB', 'tier': 2, 'gw': 0.32}),
    ('NUCOR', {'t': 'NuScale investor; Helion fusion PPA; exploring advanced nuclear for steel mills',
               'cls': 'NB', 'tier': 5, 'gw': None}),
]

def collect_industrial():
    rows = load_bulk(YEAR)
    units = load_units(YEAR)
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
            if pname == 'Independent / undisclosed':
                pname = str(f['name'] or '?').title()[:44]      # orphan facility stands as its own entry
            p = parents.setdefault((pname, sector), {
                'name': pname, 'sector': sector, 'co2e': 0.0, 'gwh_e': 0.0, 'gwh_th': 0.0,
                'sites': [], 'states': set()})
            p['co2e'] += co2e; p['gwh_e'] += e; p['gwh_th'] += th
            uu = units.get(int(f['fid']))
            smw = 0
            if uu:
                smw = uu['mwth']
                p['mwth'] = p.get('mwth', 0) + uu['mwth']
                fu = p.setdefault('fuels', {'gas': 0, 'coal': 0, 'pet': 0, 'oth': 0})
                for b, t in uu['f'].items():
                    fu[b] += (t / FUEL_EF[b]) / 3600          # tCO2 -> GJ -> GWh_th
                p['gwh_bio'] = p.get('gwh_bio', 0) + (uu['bio'] / BIO_EF) / 3600
            st = f['st'] or ''
            p['states'].add(st)
            p['sites'].append({'n': str(f['name'] or '?').title()[:48], 'st': st,
                               'lat': round(float(f['lat'] or 0), 3), 'lon': round(float(f['lon'] or 0), 3),
                               'co2e': round(co2e), 'gwh': round(e + th, 1), 'mwth': round(smw)})
        meta.append({'sector': sector, 'facilities': fac_count})
        print(f'  {sector:10} facilities={fac_count}')
    for p in parents.values():
        up = p['name'].upper()
        for key, sig in INDUSTRIAL_SIGNALS:
            if key in up:
                p.setdefault('signals', []).append(sig if isinstance(sig, dict) else {'t': sig})
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
                      'mwth': round(p.get('mwth', 0)),
                      'fuels': {k: round(v, 1) for k, v in p.get('fuels', {}).items() if v > 0.05},
                      'gwh_bio': round(p.get('gwh_bio', 0), 1),
                      'sites': p['sites'][:8], 'note': '', 'signals': p.get('signals', [])})
    for name, mw, signal in DC_SEED:
        gwh = round(mw * 8.76 * 0.80, 0) if mw else None
        items.append({'name': name, 'sector': 'DATA CENTERS', 'gwh': gwh, 'gwh_e': gwh, 'gwh_th': 0,
                      'co2e': None, 'n_sites': None, 'states': [], 'sites': [], 'mw': mw,
                      'note': ('curated; announced campus MW' if mw else 'curated; MW not disclosed'),
                      'signals': (signal if isinstance(signal, list) else ([{'t': signal}] if signal else []))})
    # semiconductors: GHGRP shows process gases only; electricity is scope 2, so no GWh claim
    for i in items:
        if i['sector'] == 'SEMICONDUCTORS':
            i['gwh'] = i['gwh_e'] = i['gwh_th'] = None
            i['note'] = 'process-gas CO2e only; electricity is scope 2 — true load far exceeds this'
            for sx in i['sites']:
                sx['gwh'] = None
    industrial = sorted([i for i in items if i['sector'] not in ('DATA CENTERS', 'SEMICONDUCTORS')],
                        key=lambda i: -(i['gwh'] or 0))[:100]
    semis = sorted([i for i in items if i['sector'] == 'SEMICONDUCTORS'], key=lambda i: -(i['co2e'] or 0))
    dcs = sorted([i for i in items if i['sector'] == 'DATA CENTERS'], key=lambda i: -(i['gwh'] or 0))
    items = sorted(industrial + dcs, key=lambda i: -(i['gwh'] or 0)) + semis
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'year': YEAR,
               'sector_colors': SECTOR_COLOR, 'universe': meta,
               'count': len(items), 'items': items,
               'method': ('Industrial demand estimated from EPA GHGRP reported CO2e (RY' + YEAR + '). Factors: cement '
                          '3.4 GJ/t clinker + 0.105 MWh/t; steel (incl. coke) 0.080 tCO2/GJ + 0.25 MWh/tCO2e; '
                          'aluminium 14.2 MWh/t Al; chemicals (incl. chlor-alkali, resins) gas-EF heat + 0.10 '
                          'MWh/tCO2e; refining 0.070 tCO2/GJ + 0.05 MWh/tCO2e; pulp & paper gas-EF + 0.15 '
                          'MWh/tCO2e (fossil share only — biomass steam not visible); food & biofuels gas-EF '
                          '+ 0.08; minerals (lime + glass) blended kiln/furnace heat + 0.06. Semiconductors: '
                          'no GWh claimed — GHGRP sees process gases only, electricity is scope 2. Unit layer: rated heat capacity (MWth) and heat-by-fuel from EPA unit/fuel reporting '
                          '(fuel-specific EFs: gas .0561, coal .0946, petroleum .0733 tCO2/GJ); biomass steam '
                          'from biogenic CO2 at .112, shown separately and excluded from ranking. Data '
                          'centres: curated announced MW x 8.76 x 0.80. Screening estimates, not metered '
                          'data; reported CO2e shown alongside.'),
               'framing': 'Order-book context (BTI, Jul-2026): ~74 GW of announced/prospective US new-build — ~580 TWh/yr if fully built — plus 8.3 GW of existing-fleet hyperscaler deals; this layer maps the 2,077 TWh/yr of industrial demand behind that pipeline. Signal tiers follow the BTI maturity funnel.'}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(payload, open(os.path.join(out_dir, 'offtakers.json'), 'w'), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, 'offtakers.js'), 'w').write('window.NIT_OFF = ' + json.dumps(payload, ensure_ascii=False) + ';')
    print(f'offtakers: {len(items)} entries -> offtakers.json/js')
    for i in items[:8]:
        print(f"   {i['name'][:34]:36} {i['sector']:12} {i['gwh'] or '—'} GWh/yr")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DATA)
