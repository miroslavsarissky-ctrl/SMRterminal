#!/usr/bin/env python3
"""Seed the Capital tab: a curated map of US federal opportunities newcleo can actually use.

This is a hand-maintained reference layer, deliberately separate from the live funding notices
the collector pulls. Its whole point is to separate three things that all get called "funding"
but are not the same: real competitive cash (R&D grants), material/access that carries no cash
(vouchers, CRADAs, material allocation, resource-access FOAs), and money that is already spoken
for (ARDP). Every entry is tagged by what it actually provides so nobody reads the list as cash
on the table. Edit the PROGRAMMES list and re-run to regenerate data/programmes.{json,js}.

register: 'cash' (competitive, cost-shared money) | 'inkind' (access/material/partnership, no cash)
          | 'allocated' (reference only, already awarded)
status:   'open' | 'awarded' | 'selected' | 'rolling' | 'allocated'
"""
import os, json, datetime as dt

PROGRAMMES = [
    # ---- CASH: competitive, cost-shared R&D (funds research, not a plant) ----
    {
        'name': 'ARPA-E HORNIG', 'sponsor': 'ARPA-E', 'kind': 'R&D grant (NOFO)',
        'register': 'cash', 'provides': 'Up to $50M program, cost-shared', 'status': 'open',
        'deadline': '', 'deadline_label': 'Concept papers (confirm on eXCHANGE)',
        'fit': 'Transuranic and recycled fuels: the closest federal cash line to newcleo\u2019s MOX and closed-cycle positioning.',
        'funds': 'Design, fabrication and qualification of TRU fuels from used fuel; targets qualification in under seven years.',
        'link': 'https://arpa-e.energy.gov/programs-and-initiatives/view-all-programs/hornig',
        'note': 'Competitive cost-shared R&D. Funds research, not build-out.',
    },
    {
        'name': 'ARPA-E Advanced Reactor Fuels', 'sponsor': 'ARPA-E', 'kind': 'Teaming / partner list',
        'register': 'cash', 'provides': 'Team formation for a potential NOFO', 'status': 'open',
        'deadline': '', 'deadline_label': 'Teaming list open',
        'fit': 'Registering signals capability and finds US partners ahead of a possible TRU-fuels funding call.',
        'funds': 'Not funding yet: a teaming partner list for a potential future NOFO on domestic nuclear fuels.',
        'link': 'https://arpa-e-foa.energy.gov/',
        'note': 'Pipeline to cash, not cash. Teaming Partner List announced Jan 2026.',
    },
    {
        'name': 'NEUP', 'sponsor': 'DOE-NE', 'kind': 'University R&D grant',
        'register': 'cash', 'provides': 'Grants to US universities', 'status': 'rolling',
        'deadline': '', 'deadline_label': 'Annual cycle',
        'fit': 'Accessible only through a US university partner; fuel, materials and licensing R&D.',
        'funds': 'University-led nuclear energy R&D awards (Nuclear Energy University Program).',
        'link': 'https://neup.inl.gov/',
        'note': 'Requires a US university PI. A partnership route, not a direct newcleo award.',
    },
    # ---- IN-KIND: access, material, partnership (no cash to the applicant) ----
    {
        'name': 'GAIN NE Vouchers', 'sponsor': 'DOE-NE / INL', 'kind': 'Lab-access voucher (CRADA)',
        'register': 'inkind', 'provides': 'National-lab access, no direct cash', 'status': 'open',
        'deadline': '2026-07-31', 'deadline_label': 'FY-2026 Round 4',
        'fit': 'Direct access to national-lab capability for fuel fabrication, lead-cooled materials testing and licensing analysis.',
        'funds': 'Access to DOE national-lab expertise and facilities at no cost; recipient provides 20% cost-share via a CRADA.',
        'link': 'https://gain.inl.gov/industry-support/gain-ne-vouchers/',
        'note': 'Recipients get lab access, not money. Eligibility ties resulting IP to US manufacturing: confirm newcleo Americas fit.',
    },
    {
        'name': 'SPUR', 'sponsor': 'DOE-NE', 'kind': 'Material allocation (OTA)',
        'register': 'inkind', 'provides': 'Surplus plutonium material, applicant-funded', 'status': 'selected',
        'deadline': '', 'deadline_label': 'Selections made; negotiating',
        'fit': 'newcleo is already engaged via the Oklo partnership and the $2B US fuel-fabrication plan.',
        'funds': 'Surplus plutonium (up to ~20 MT) for conversion to advanced-reactor fuel. Applicant carries all facility costs.',
        'link': 'https://www.energy.gov/ne/articles/department-energy-seeks-transform-surplus-plutonium-nuclear-fuel',
        'note': 'Five companies selected Oct 2025 (incl. Oklo); negotiations ongoing. Allocation of material, not cash.',
    },
    {
        'name': 'Fuel-cycle resource FOAs', 'sponsor': 'DOE-NE', 'kind': 'Resource-access FOA',
        'register': 'inkind', 'provides': 'Site access, used-fuel material, lab expertise', 'status': 'open',
        'deadline': '', 'deadline_label': 'Per internal tracking',
        'fit': 'Access to DOE sites, used-fuel material and national-lab expertise for fuel-cycle work.',
        'funds': 'DE-FOA-0003611 and DE-FOA-0000001: provide site access, used-fuel material and lab expertise. Applicant carries every dollar.',
        'link': '',
        'note': 'Not capital. These are resource-access, applicant self-funds entirely. Confirm current status before acting.',
    },
    {
        'name': 'CRADA', 'sponsor': 'DOE national labs', 'kind': 'Cooperative R&D Agreement',
        'register': 'inkind', 'provides': 'In-kind lab partnership', 'status': 'rolling',
        'deadline': '', 'deadline_label': 'Initiate any time',
        'fit': 'Direct R&D partnership with a national lab (INL, ANL, ORNL, SRNL) on fuel and lead-cooled materials.',
        'funds': 'Collaborative R&D: the lab brings expertise and facilities, the partner funds its own share. No cash to newcleo.',
        'link': 'https://www.energy.gov/technologytransitions/cooperative-research-and-development-agreements-cradas',
        'note': 'A mechanism, not a dated call. Can be initiated at any time, including under a GAIN voucher.',
    },
    # ---- ALLOCATED: reference only ----
    {
        'name': 'ARDP', 'sponsor': 'DOE-NE', 'kind': 'Demonstration cost-share',
        'register': 'allocated', 'provides': 'Largely allocated', 'status': 'allocated',
        'deadline': '', 'deadline_label': 'Spoken for',
        'fit': 'The large demonstration money, effectively committed to the X-energy and TerraPower cohort.',
        'funds': 'Advanced Reactor Demonstration Program cost-share for first-of-a-kind builds.',
        'link': 'https://www.energy.gov/ne/advanced-reactor-demonstration-program',
        'note': 'Reference line, not a live opportunity for newcleo.',
    },
    {
        'name': 'ARPA-E NEWTON', 'sponsor': 'ARPA-E', 'kind': 'R&D grant (awarded)',
        'register': 'allocated', 'provides': '$40M, awarded across 11 projects', 'status': 'awarded',
        'deadline': '', 'deadline_label': 'Closed',
        'fit': 'Used-fuel transmutation R&D, lab-led and already awarded.',
        'funds': 'Transmutation technology R&D. Awards made in 2025 (Argonne, Fermilab and others).',
        'link': 'https://arpa-e.energy.gov/programs-and-initiatives/view-all-programs/newton',
        'note': 'Already awarded; reference only.',
    },
]

REG_ORDER = {'cash': 0, 'inkind': 1, 'allocated': 2}
STAT_ORDER = {'open': 0, 'selected': 1, 'rolling': 2, 'awarded': 3, 'allocated': 4}


def build(out_dir):
    items = sorted(PROGRAMMES, key=lambda p: (REG_ORDER.get(p['register'], 9),
                                              STAT_ORDER.get(p['status'], 9),
                                              p.get('deadline') or '9999'))
    payload = {
        'generated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00'),
        'count': len(items),
        'open': sum(1 for p in items if p['status'] == 'open'),
        'items': items,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'programmes.json'), 'w') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, 'programmes.js'), 'w') as f:
        f.write('window.NIT_PROG = ' + json.dumps(payload, ensure_ascii=False) + ';')
    by = {}
    for p in items:
        by[p['register']] = by.get(p['register'], 0) + 1
    print(f'programmes: {len(items)} ({payload["open"]} open) -> {out_dir} | by register: {by}')
    return payload


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'data')
    build(out)
