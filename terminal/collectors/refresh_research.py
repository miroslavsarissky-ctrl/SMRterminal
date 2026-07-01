#!/usr/bin/env python3
"""Automatic weekly refresh of the Events and Capital tabs, powered by Claude with web search.

Runs in GitHub Actions on a schedule (see .github/workflows/refresh-research.yml). For each
dataset it hands Claude a research brief plus the CURRENT list as the existing tracker, asks
for net-new and changed items only, lets Claude search and verify on official/organiser sites,
then merges the result and commits the JSON. No human curates the list.

Trust model (this is the automatic-but-honest part): anything Claude adds or changes is written
back flagged unconfirmed (source='auto', verified=False) with the confidence Claude assigned, so
the board updates on its own while still marking what is solid and what is not. Rows you have
manually promoted to verified are preserved and never overwritten by the refresh; if Claude
reports a change to one, it is attached as a note instead of clobbering your confirmed data.

Model, cadence and search budget are all env/CLI configurable. Set MOCK_CONF / MOCK_PROG to a
JSON file path to test the merge offline without calling the API.
"""
import os, re, sys, json, datetime as dt

HERE = os.path.dirname(__file__)
DATA = os.path.join(HERE, '..', 'data')
MODEL = os.environ.get('RESEARCH_MODEL', 'claude-sonnet-5')
MAX_SEARCHES = int(os.environ.get('RESEARCH_MAX_SEARCHES', '12'))
WINDOW = os.environ.get('RESEARCH_WINDOW', 'next 6 months')
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()

CONF_SCHEMA = ("Return ONLY a JSON array, no prose and no markdown fences. Each element must have: "
    "flag (country emoji), name, organizer, dates (as written), start (YYYY-MM-DD or null), "
    "end (YYYY-MM-DD or null), city, country, region ('International' for EU/UK/rest, 'USA' for US), "
    "rating ('HIGH'|'MEDIUM'|'LOW'), participation, speaker, relevance, link (official page), "
    "type, confidence ('High'|'Medium'|'Low'). Include net-new events and any event ALREADY in the "
    "tracker whose date or venue has changed; for a changed event add a \"change\" field naming what "
    "changed. Do NOT return unchanged events already in the tracker.")

PROG_SCHEMA = ("Return ONLY a JSON array, no prose and no markdown fences. Each element must have: "
    "name, sponsor, kind, register ('cash' for competitive money | 'inkind' for access/material/"
    "partnership with no cash to the applicant | 'allocated' for already-committed reference), "
    "provides (one line on what it actually gives), status ('open'|'awarded'|'selected'|'rolling'|"
    "'allocated'), deadline (YYYY-MM-DD or ''), deadline_label, fit (relevance to newcleo), funds "
    "(what it funds/provides), link, note (caveats), confidence ('High'|'Medium'|'Low'). Include "
    "net-new opportunities and any tracked one whose status or deadline changed (add a \"change\" "
    "field). Do NOT return unchanged entries.")

TIER = {'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())[:60]


def build_prompt(brief_path, tracker_items):
    with open(brief_path) as f:
        brief = f.read()
    tracker = json.dumps([{k: it.get(k) for k in ('name', 'dates', 'start', 'end', 'city', 'status', 'deadline')}
                          for it in tracker_items], ensure_ascii=False)
    return (f"{brief}\n\n---\nRUNTIME INPUTS\nCurrent date: {TODAY}\nTime window: {WINDOW} from the current date.\n"
            f"Existing tracker (return ONLY net-new items plus flagged changes to these):\n{tracker}\n")


def call_claude(system_brief, schema):
    """Single call; web_search runs server-side so no client tool loop is needed."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    resp = client.messages.create(
        model=MODEL, max_tokens=16000,
        system="You are a meticulous research analyst. Verify every date and status on the official "
               "or organiser site, never carry forward last year's dates, and label anything you cannot "
               "confirm as Low confidence. " + schema,
        messages=[{'role': 'user', 'content': system_brief}],
        tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': MAX_SEARCHES}],
    )
    return ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')


def parse_json_array(text):
    t = text.strip()
    if '```' in t:
        t = re.sub(r'^```(?:json)?', '', t.split('```')[1].strip())
    i = t.find('[')
    if i < 0:
        return []
    return json.loads(t[i:t.rfind(']') + 1])


def get_items(task, brief, schema, current):
    mock = os.environ.get('MOCK_' + task.upper())
    if mock:
        with open(mock) as f:
            return json.load(f)
    if not os.environ.get('ANTHROPIC_API_KEY'):
        print(f'  [{task}] no ANTHROPIC_API_KEY and no mock; skipping'); return []
    return parse_json_array(call_claude(build_prompt(brief, current), schema))


def norm_conf(r):
    rating = (r.get('rating') or 'LOW').upper()
    it = {'flag': r.get('flag', ''), 'name': r.get('name', ''), 'organizer': r.get('organizer', ''),
          'dates': r.get('dates', ''), 'start': r.get('start'), 'end': r.get('end'),
          'city': r.get('city', ''), 'country': r.get('country', ''),
          'region': r.get('region', 'International'), 'rating': rating if rating in TIER else 'LOW',
          'tier': TIER.get(rating, 3), 'participation': r.get('participation', ''),
          'speaker': r.get('speaker', ''), 'relevance': r.get('relevance', ''), 'link': r.get('link', ''),
          'type': r.get('type', ''), 'access_route': '', 'deadline': r.get('deadline', ''),
          'confidence': r.get('confidence', ''), 'verified': False, 'source': 'auto', 'status': 'candidate'}
    if r.get('change'):
        it['change_note'] = r['change']
    return it


def norm_prog(r):
    it = {'name': r.get('name', ''), 'sponsor': r.get('sponsor', ''), 'kind': r.get('kind', ''),
          'register': r.get('register', 'inkind'), 'provides': r.get('provides', ''),
          'status': r.get('status', 'open'), 'deadline': r.get('deadline', ''),
          'deadline_label': r.get('deadline_label', ''), 'fit': r.get('fit', ''),
          'funds': r.get('funds', ''), 'link': r.get('link', ''), 'note': r.get('note', ''),
          'confidence': r.get('confidence', ''), 'verified': False, 'source': 'auto'}
    if r.get('change'):
        it['change_note'] = r['change']
    return it


def merge(current, returned, normfn):
    """Add net-new (flagged auto/unconfirmed); update unverified matches; never overwrite a row the
    user has promoted to verified (attach a change note instead)."""
    idx = {_norm(it.get('name')): it for it in current}
    added = updated = protected = 0
    for r in returned:
        n = normfn(r)
        k = _norm(n['name'])
        if not k:
            continue
        cur = idx.get(k)
        if cur is None:
            idx[k] = n; added += 1
        elif cur.get('verified'):
            if r.get('change'):
                cur['change_note'] = r['change']; protected += 1
        else:
            idx[k] = n; updated += 1
    return list(idx.values()), (added, updated, protected)


def write(name, payload_extra, items, out_dir):
    payload = {'generated': NOW.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'count': len(items)}
    payload.update(payload_extra)
    payload['items'] = items
    with open(os.path.join(out_dir, name + '.json'), 'w') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    var = 'NIT_CONF' if name == 'conferences' else 'NIT_PROG'
    with open(os.path.join(out_dir, name + '.js'), 'w') as f:
        f.write(f'window.{var} = ' + json.dumps(payload, ensure_ascii=False) + ';')


def load(name, out_dir):
    p = os.path.join(out_dir, name + '.json')
    return json.load(open(p))['items'] if os.path.exists(p) else []


def refresh_conferences(out_dir):
    cur = load('conferences', out_dir)
    got = get_items('conf', os.path.join(HERE, 'conference_brief.md'), CONF_SCHEMA, cur)
    items, stats = merge(cur, got, norm_conf)
    items = [i for i in items if not (i.get('end') and i['end'] < TODAY)]          # drop finished events
    items.sort(key=lambda x: (x.get('start') or '9999', x.get('tier', 3)))
    hi = sum(1 for i in items if i.get('rating') == 'HIGH')
    write('conferences', {'regions': sorted({i.get('region', '') for i in items}), 'high': hi}, items, out_dir)
    print(f'  conferences: +{stats[0]} new, ~{stats[1]} updated, {stats[2]} verified-protected -> {len(items)} total')


def refresh_programmes(out_dir):
    cur = load('programmes', out_dir)
    got = get_items('prog', os.path.join(HERE, 'programmes_brief.md'), PROG_SCHEMA, cur)
    items, stats = merge(cur, got, norm_prog)
    order = {'cash': 0, 'inkind': 1, 'allocated': 2}
    items.sort(key=lambda p: (order.get(p.get('register'), 9), 0 if p.get('status') == 'open' else 1, p.get('deadline') or '9999'))
    write('programmes', {'open': sum(1 for p in items if p.get('status') == 'open')}, items, out_dir)
    print(f'  programmes: +{stats[0]} new, ~{stats[1]} updated, {stats[2]} verified-protected -> {len(items)} total')


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else DATA
    which = sys.argv[2] if len(sys.argv) > 2 else 'all'
    print(f'refresh_research {TODAY} model={MODEL} window="{WINDOW}"')
    if which in ('all', 'conf'):
        refresh_conferences(out)
    if which in ('all', 'prog'):
        refresh_programmes(out)
