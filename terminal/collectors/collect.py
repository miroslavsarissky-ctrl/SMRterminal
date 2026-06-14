#!/usr/bin/env python3
"""
newcleo Nuclear Intel Terminal - collector
Pulls public primary sources + news, tags items against the dashboard watchlist,
and writes data/feed.json + data/feed.js for the terminal UI.

Sources (v1):
  federal_register  - NRC + DOE documents, with structured comment deadlines (no key)
  grants_gov        - nuclear funding opportunities / RFAs (no key)
  sam_gov           - RFIs / RFPs / sources-sought  (needs SAM_GOV_API_KEY)
  nrc_adams         - NRC ADAMS public docket documents (no key)
  edgar             - SEC filings for tickered watchlist companies (no key)
  google_news       - rotating per-company + standing topic queries (no key)
  x_api             - posts from watchlist X handles (needs X_BEARER_TOKEN, pay-per-use)
"""
import os, re, json, hashlib, datetime, email.utils, html as html_mod
import requests, feedparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
UA = {'User-Agent': 'newcleo-nuclear-intel-terminal/1.0 (market intelligence; contact: comms@newcleo.com)'}
NOW = datetime.datetime.now(datetime.timezone.utc)
WINDOW_DAYS = 120
MAX_ITEMS = 600

TOPIC_QUERIES = ['HALEU', '"advanced nuclear" funding', '"nuclear fuel" supply chain', 'SMR deployment']

# Trusted publishers pulled from their OWN chronological feeds — reliable dates,
# so old articles can never resurface as "today". This is the news backbone.
PUBLISHER_FEEDS = [
    ('World Nuclear News',    'https://www.world-nuclear-news.org/rss'),
    ('ANS Nuclear Newswire',  'https://www.ans.org/news/feed/'),
    ('Utility Dive',          'https://www.utilitydive.com/feeds/news/'),
    ('POWER Magazine',        'https://www.powermag.com/feed/'),
    ('Neutron Bytes',         'https://neutronbytes.com/feed/'),
]
# Domains already pulled directly above — skip in Google News so Google's
# unreliable re-surfacing dates can't reintroduce stale items (the TerraPower bug).
DIRECT_DOMAINS = {'world-nuclear-news.org', 'ans.org', 'utilitydive.com', 'powermag.com', 'neutronbytes.com'}
# Content-mills / stock-tip SEO that recycle old news — dropped everywhere.
NEWS_BLOCKLIST = {'indexbox.io', 'mugglehead.com', 'simplywall.st', 'marketbeat.com',
                  'tipranks.com', 'zacks.com', 'barchart.com', 'fool.com', 'benzinga.com',
                  'stocktwits.com', 'investorplace.com', 'stocktitan.net'}
YEAR_RX = re.compile(r'\b(20\d{2})\b')
TOPIC_TAGS = {'haleu': 'HALEU', 'mox': 'MOX', 'plutonium': 'Pu disposition', 'lead-cooled': 'LFR',
              'molten salt': 'MSR', 'microreactor': 'Microreactor', 'enrichment': 'Enrichment',
              'reprocessing': 'Recycling', 'recycl': 'Recycling', 'savannah river': 'SRS'}

# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def iid(url, title=''):
    return hashlib.sha1((url or title).encode()).hexdigest()[:16]

def iso(dt):
    if isinstance(dt, str): return dt
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec='seconds')

def parse_date(s):
    """Best-effort to UTC datetime from RSS/API date strings."""
    if not s: return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception: pass
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            d = datetime.datetime.strptime(s[:len(fmt)+6], fmt)
            return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
        except Exception: continue
    return None

def strip_html(s, n=280):
    s = html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    s = re.sub(r'\s+', ' ', s).strip()
    return (s[:n] + '…') if len(s) > n else s

# ---------------------------------------------------------------- watchlist matcher
WL = load_json(os.path.join(DATA, 'watchlist.json'), {'entities': []})
MATCHERS = []
for e in WL['entities']:
    for a in e['aliases']:
        if len(a) < 3: continue
        # short/upper aliases (GLE, UEC, NRC) must match case-exactly on word boundary
        if len(a) <= 4 and a.isupper():
            MATCHERS.append((re.compile(r'\b' + re.escape(a) + r'\b'), e['id']))
        else:
            MATCHERS.append((re.compile(r'\b' + re.escape(a) + r'\b', re.I), e['id']))

def tag_entities(text):
    found = []
    for rx, eid in MATCHERS:
        if eid not in found and rx.search(text): found.append(eid)
    return found

def tag_topics(text):
    t = text.lower()
    return sorted({label for k, label in TOPIC_TAGS.items() if k in t})

def make_item(title, url, ts, source, bucket, summary='', deadline=None, deadline_label=''):
    title = strip_html(title, 220)
    txt = title + ' ' + (summary or '')
    if deadline:
        d = parse_date(str(deadline))
        deadline = d.strftime('%Y-%m-%d') if d else None
    return {'id': iid(url, title), 'ts': iso(ts), 'title': title, 'url': url,
            'source': source, 'bucket': bucket, 'summary': strip_html(summary),
            'entities': tag_entities(txt), 'topics': tag_topics(txt),
            'deadline': deadline, 'deadline_label': deadline_label, 'verified': False}

# ---------------------------------------------------------------- sources
def federal_register():
    items = []
    base = 'https://www.federalregister.gov/api/v1/documents.json'
    for agency in ('nuclear-regulatory-commission', 'energy-department'):
        try:
            r = requests.get(base, params={
                'per_page': 40, 'order': 'newest',
                'conditions[agencies][]': agency,
                'fields[]': ['title', 'type', 'abstract', 'html_url',
                             'publication_date', 'comments_close_on', 'agencies'],
            }, headers=UA, timeout=30)
            NUC = re.compile(r'nuclear|uranium|reactor|isotope|radioact|HALEU|tritium|plutonium|NNSA|spent fuel|radiolog|fission|enrich|NRC\b', re.I)
            for d in r.json().get('results', []):
                if agency == 'energy-department' and not NUC.search((d.get('title','') or '') + ' ' + (d.get('abstract') or '')):
                    continue
                ts = parse_date(d.get('publication_date')) or NOW
                dl = d.get('comments_close_on')
                items.append(make_item(
                    f"[{d.get('type','Notice')}] {d['title']}", d['html_url'], ts,
                    'Federal Register · ' + ('NRC' if 'nuclear' in agency else 'DOE'),
                    'regulatory', d.get('abstract') or '',
                    deadline=dl, deadline_label='Comments close' if dl else ''))
        except Exception as ex:
            print('  ! federal_register', agency, ex)
    return items

def grants_gov():
    items = []
    try:
        r = requests.post('https://api.grants.gov/v1/api/search2',
                          json={'keyword': 'nuclear', 'oppStatuses': 'forecasted|posted', 'rows': 60},
                          headers=UA, timeout=30)
        REL = re.compile(r'nuclear|uranium|reactor|isotope|radiolog|fission|enrich', re.I)
        for o in r.json().get('data', {}).get('oppHits', []):
            blob = (o.get('title','') + ' ' + (o.get('agency') or '') + ' ' + (o.get('agencyCode') or ''))
            if not REL.search(blob): continue
            url = f"https://www.grants.gov/search-results-detail/{o.get('id')}"
            ts = parse_date(o.get('openDate')) or NOW
            dl = o.get('closeDate')
            items.append(make_item(
                f"[{o.get('oppStatus','posted').title()} FOA] {o.get('title','')} ({o.get('number','')})",
                url, ts, 'Grants.gov · ' + (o.get('agencyCode') or 'Fed'), 'funding',
                f"Agency: {o.get('agency','')}",
                deadline=dl, deadline_label='Applications close' if dl else ''))
    except Exception as ex:
        print('  ! grants_gov', ex)
    return items

def sam_gov():
    key = os.environ.get('SAM_GOV_API_KEY')
    if not key:
        print('  - sam_gov skipped (set SAM_GOV_API_KEY)')
        return []
    # SAM opportunities update daily and personal keys have low daily caps — throttle to ~3h.
    # Previously-collected SAM items persist in history (bucket 'funding'), so nothing is lost.
    sstate = load_json(os.path.join(DATA, 'sam_state.json'), {})
    last = parse_date(sstate.get('last', ''))
    if last and (NOW - last).total_seconds() < 3 * 3600:
        print('  - sam_gov throttled (ran <3h ago)')
        return []
    items, seen = [], set()
    frm = (NOW - datetime.timedelta(days=45)).strftime('%m/%d/%Y')
    to = NOW.strftime('%m/%d/%Y')
    # GSA docs say /prod/, many integrations use the bare path — try both, use whichever returns 200.
    endpoints = ['https://api.sam.gov/opportunities/v2/search',
                 'https://api.sam.gov/prod/opportunities/v2/search']
    hdr = {'Accept': 'application/json', **UA}
    auth_failed, got_200 = False, False
    for kw in ('nuclear', 'HALEU', 'reactor'):           # title sweep (API has no body full-text)
        data = None
        for ep in endpoints:
            try:
                r = requests.get(ep, params={'api_key': key, 'postedFrom': frm, 'postedTo': to,
                                             'title': kw, 'limit': 100}, headers=hdr, timeout=45)
                if r.status_code == 200:
                    data = r.json(); got_200 = True; break
                if r.status_code in (401, 403):
                    auth_failed = True; break
                # 404/400 here usually means wrong endpoint or rejected key — try the other endpoint
            except Exception as ex:
                print('  ! sam_gov', kw, ex)
        if auth_failed:
            print('  ! sam_gov auth failed — verify SAM_GOV_API_KEY is a SAM.gov Account-Details public key'); break
        if data is None:
            print(f'  ! sam_gov no 200 for "{kw}" (endpoint or key issue)'); continue
        for o in data.get('opportunitiesData', []):
            nid = o.get('noticeId') or o.get('uiLink') or o.get('title', '')
            if nid in seen: continue
            seen.add(nid)
            ts = parse_date(o.get('postedDate')) or NOW
            dl = o.get('responseDeadLine')
            ptype = o.get('type') or o.get('baseType') or 'Opportunity'
            items.append(make_item(
                f"[{ptype}] {o.get('title','')}",
                o.get('uiLink') or ('https://sam.gov/opp/' + (o.get('noticeId', '') or '')),
                ts, 'SAM.gov · ' + ((o.get('fullParentPathName', '') or 'Fed').split('.')[0]),
                'funding', f"Solicitation {o.get('solicitationNumber','—')}",
                deadline=dl, deadline_label='Responses due' if dl else ''))
    if got_200:                                          # only mark success when SAM actually answered
        sstate['last'] = iso(NOW)
        json.dump(sstate, open(os.path.join(DATA, 'sam_state.json'), 'w'))
    return items

def nrc_adams():
    items = []
    frm = (NOW - datetime.timedelta(days=7)).strftime('%m/%d/%Y')
    q = ("(mode:sections,sections:(filters:(public-library:!t),"
         f"properties_search_all:!(!(DocumentDate,gt,'{frm}','')),"
         "options:(within-folder:(enable:!f,insubfolder:!f,path:''))))")
    try:
        r = requests.get('https://adams.nrc.gov/wba/services/search/advanced/nrc',
                         params={'q': q, 'qn': 'New', 'tab': 'content-search-pars',
                                 's': 'DocumentDate', 'so': 'DESC'},
                         headers=UA, timeout=45)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        kept = 0
        for res in root.iter('result'):
            f = {c.tag: (c.text or '') for c in res}
            title = f.get('DocumentTitle') or f.get('title') or ''
            acc = f.get('AccessionNumber') or ''
            if not title or not acc: continue
            txt = title + ' ' + f.get('DocketNumber', '')
            ents = tag_entities(txt)
            tops = tag_topics(txt)
            if not ents and not tops: continue          # keep only watchlist-relevant docs
            url = f'https://adamswebsearch2.nrc.gov/webSearch2/main.jsp?AccessionNumber={acc}'
            ts = parse_date(f.get('DocumentDate')) or NOW
            it = make_item(title, url, ts, 'NRC ADAMS', 'regulatory',
                           f"Docket {f.get('DocketNumber','—')} · {f.get('DocumentType','')}")
            items.append(it); kept += 1
            if kept >= 40: break
    except Exception as ex:
        print('  ! nrc_adams', ex)
    return items

def edgar():
    items = []
    try:
        r = requests.get('https://www.sec.gov/files/company_tickers.json', headers=UA, timeout=30)
        cikmap = {v['ticker']: str(v['cik_str']).zfill(10) for v in r.json().values()}
    except Exception as ex:
        print('  ! edgar ticker map', ex); return items
    MATERIAL = re.compile(r'^(8-K|10-K|10-Q|S-1|S-4|F-4|425|DEF 14A|DEFM14A|6-K|SC 13|S-3|424B)', re.I)
    CLEAN = {'BWXT': 'BWXT', 'LEU': 'Centrus Energy', 'SMR': 'NuScale Power'}
    done_tk = set()
    for e in WL['entities']:
        tk = e.get('ticker')
        if not tk or tk not in cikmap or tk in done_tk: continue
        done_tk.add(tk)
        disp = CLEAN.get(tk) or re.sub(r'\s*[—–(].*$', '', e['name']).strip()
        try:
            r = requests.get('https://www.sec.gov/cgi-bin/browse-edgar',
                             params={'action': 'getcompany', 'CIK': cikmap[tk],
                                     'type': '', 'dateb': '', 'owner': 'include',
                                     'count': 40, 'output': 'atom'},
                             headers=UA, timeout=30)
            fp = feedparser.parse(r.text)
            for en in fp.entries[:40]:
                ts = parse_date(en.get('updated') or en.get('published'))
                if not ts or (NOW - ts).days > 30: continue
                ftype = (en.get('category') or en.get('title', '').split(' - ')[0]).strip()
                if not MATERIAL.match(ftype): continue
                it = make_item(f"{disp} files {ftype} ({ts.strftime('%d %b')})", en.link, ts,
                               'SEC EDGAR', 'filings', en.get('title', ''))
                if e['id'] not in it['entities']: it['entities'].insert(0, e['id'])
                items.append(it)
        except Exception as ex:
            print('  ! edgar', tk, ex)
    return items

def _source_domain(en):
    src = en.get('source')
    href = ''
    if isinstance(src, dict): href = src.get('href', '')
    elif hasattr(src, 'href'): href = getattr(src, 'href', '')
    if not href: href = en.get('link', '')
    m = re.search(r'https?://([^/]+)', href or '')
    return (m.group(1) if m else '').lower().replace('www.', '')

def _title_stale(title):
    """True only if the title names a year 2+ years old and no current year — a cheap
    guard against re-surfaced articles whose date string carries the year."""
    yrs = [int(y) for y in YEAR_RX.findall(title)]
    return bool(yrs) and (NOW.year not in yrs) and (max(yrs) <= NOW.year - 2)

def publisher_news():
    """News backbone: trusted outlets' own chronological feeds. Dates are reliable,
    so stale items structurally cannot appear. Filtered to watchlist/topic relevance."""
    items = []
    for name, url in PUBLISHER_FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            fp = feedparser.parse(r.content)
            kept = 0
            for en in fp.entries[:45]:
                ts = parse_date(en.get('published') or en.get('updated'))
                if not ts or (NOW - ts).days > 30: continue
                title = en.get('title', '')
                summary = strip_html(en.get('summary', ''), 300)
                txt = title + ' ' + summary
                if not tag_entities(txt) and not tag_topics(txt): continue   # relevance gate
                items.append(make_item(title, en.get('link', ''), ts, name, 'news', summary))
                kept += 1
        except Exception as ex:
            print('  ! publisher_news', name, ex)
    return items

def google_news():
    """Supplement: per-company Google News, hardened. Tight window; drop content-mills
    and any domain already covered by a direct feed (whose dates are trustworthy)."""
    items = []
    rot = load_json(os.path.join(DATA, 'rotation.json'), {'i': 0})
    qents = [e for e in WL['entities'] if e.get('query')]
    take = 12
    batch = [qents[(rot['i'] + k) % len(qents)] for k in range(take)] if qents else []
    rot['i'] = (rot['i'] + take) % max(len(qents), 1)
    json.dump(rot, open(os.path.join(DATA, 'rotation.json'), 'w'))
    queries = [(f'"{e["aliases"][-1]}" nuclear', e) for e in batch] + [(t, None) for t in TOPIC_QUERIES]
    for q, ent in queries:
        try:
            url = 'https://news.google.com/rss/search?q=' + requests.utils.quote(q) + '&hl=en-US&gl=US&ceid=US:en'
            fp = feedparser.parse(requests.get(url, headers=UA, timeout=25).text)
            for en in fp.entries[:5]:
                ts = parse_date(en.get('published'))
                if not ts or (NOW - ts).days > 10: continue          # tightened 21 -> 10
                dom = _source_domain(en)
                if dom in DIRECT_DOMAINS or dom in NEWS_BLOCKLIST: continue
                if _title_stale(en.get('title', '')): continue
                src = en.get('source', {}).get('title', 'News') if hasattr(en.get('source', {}), 'get') else 'News'
                it = make_item(en.title, en.link, ts, src, 'news', '')
                if ent and ent['id'] not in it['entities']: it['entities'].insert(0, ent['id'])
                items.append(it)
        except Exception as ex:
            print('  ! google_news', q, ex)
    return items

def x_api():
    tok = os.environ.get('X_BEARER_TOKEN')
    if not tok:
        print('  - x_api skipped (set X_BEARER_TOKEN; pay-per-use reads)')
        return []
    items = []
    handles = [(e['x']['handle'], e['id']) for e in WL['entities'] if e.get('x')]
    state = load_json(os.path.join(DATA, 'x_state.json'), {})
    hmap = {h.lower(): eid for h, eid in handles}
    # chunk from: queries to stay under the 512-char query limit
    chunks, cur = [], []
    for h, _ in handles:
        if len(' OR '.join(f'from:{x}' for x in cur + [h])) > 460:
            chunks.append(cur); cur = []
        cur.append(h)
    if cur: chunks.append(cur)
    for ch in chunks:
        q = '(' + ' OR '.join(f'from:{h}' for h in ch) + ') -is:retweet'
        params = {'query': q, 'max_results': 25,
                  'tweet.fields': 'created_at,author_id', 'expansions': 'author_id',
                  'user.fields': 'username'}
        sid = state.get(ch[0])
        if sid: params['since_id'] = sid
        try:
            r = requests.get('https://api.x.com/2/tweets/search/recent', params=params,
                             headers={'Authorization': f'Bearer {tok}', **UA}, timeout=30)
            j = r.json()
            users = {u['id']: u['username'] for u in j.get('includes', {}).get('users', [])}
            tweets = j.get('data', [])
            if tweets: state[ch[0]] = tweets[0]['id']
            for t in tweets:
                un = users.get(t['author_id'], '')
                ts = parse_date(t.get('created_at')) or NOW
                it = make_item(t['text'][:200], f"https://x.com/{un}/status/{t['id']}",
                               ts, f'X · @{un}', 'social', t['text'])
                eid = hmap.get(un.lower())
                if eid and eid not in it['entities']: it['entities'].insert(0, eid)
                items.append(it)
        except Exception as ex:
            print('  ! x_api', ex)
    json.dump(state, open(os.path.join(DATA, 'x_state.json'), 'w'))
    return items

# ---------------------------------------------------------------- dedup
def _norm_title(t):
    t = re.sub(r'\s*[-|–—:]\s*[^-|–—:]+$', '', t)   # strip trailing " - Source"
    t = re.sub(r'#\d+\s*$', '', t)
    t = re.sub(r'[^a-z0-9 ]', '', t.lower())
    return re.sub(r'\s+', ' ', t).strip()[:80]

_PRIMARY_PREFIX = ('Federal Register', 'NRC ADAMS', 'SEC EDGAR', 'Grants.gov', 'SAM.gov')
_FEED_NAMES = {n for n, _ in PUBLISHER_FEEDS}
def _src_rank(it):
    s = it.get('source', '')
    if any(s.startswith(p) for p in _PRIMARY_PREFIX): return 3   # official sources
    if s in _FEED_NAMES: return 2                                # trusted publisher feed
    return 1                                                     # google-news / misc

def dedup(items):
    """Collapse near-identical headlines (same bucket + normalized title). Keeps the
    most authoritative / earliest copy, unions entity & topic tags, counts the rest."""
    ranked = sorted(items, key=lambda it: (-_src_rank(it), it['ts']))
    seen = {}
    for it in ranked:
        # SEC filings are each distinct documents (exact-URL dupes already merged upstream) —
        # never fuzzy-collapse them, or distinct 425s/8-Ks would vanish.
        if it['bucket'] == 'filings':
            seen[it['id']] = it; continue
        key = (it['bucket'], _norm_title(it['title']))
        if not key[1]:                       # untitled — never collapse
            seen[it['id']] = it; continue
        if key in seen:
            k = seen[key]
            k['dupes'] = k.get('dupes', 0) + 1
            for e in it['entities']:
                if e not in k['entities']: k['entities'].append(e)
            for t in it['topics']:
                if t not in k['topics']: k['topics'].append(t)
            k['verified'] = k['verified'] or it['verified']
        else:
            it.setdefault('dupes', 0)
            seen[key] = it
    return list(seen.values())

# ---------------------------------------------------------------- main
def main():
    os.makedirs(DATA, exist_ok=True)
    prev = load_json(os.path.join(DATA, 'feed.json'), {'items': []})
    verified_ids = {i['id'] for i in prev['items'] if i.get('verified')}
    known = {i['id']: i for i in prev['items']}
    # Self-heal: news from Google (dates not fully trustworthy) is re-collected fresh
    # every run, so a stale item stored by an earlier run cannot linger. We retain
    # verified items, all non-news history, and news from trusted publisher feeds
    # (whose chronological dates are reliable).
    known = {k: it for k, it in known.items()
             if it.get('verified') or it.get('bucket') != 'news' or it.get('source') in _FEED_NAMES}

    collected = []
    for name, fn in [('federal_register', federal_register), ('grants_gov', grants_gov),
                     ('sam_gov', sam_gov), ('nrc_adams', nrc_adams), ('edgar', edgar),
                     ('publisher_news', publisher_news), ('google_news', google_news),
                     ('x_api', x_api)]:
        got = fn()
        print(f'  {name}: {len(got)}')
        collected += got

    merged = dict(known)                      # keep history
    for it in collected:
        if it['id'] in verified_ids: it['verified'] = True
        if it['id'] not in merged: merged[it['id']] = it

    cutoff = NOW - datetime.timedelta(days=WINDOW_DAYS)
    def keep(i):
        if (parse_date(i['ts']) or NOW) >= cutoff: return True
        d = parse_date(i.get('deadline') or '')
        return bool(d and d >= NOW - datetime.timedelta(days=1))
    items = [i for i in merged.values() if keep(i)]
    items = dedup(items)                       # collapse near-duplicate headlines
    items.sort(key=lambda i: i['ts'], reverse=True)
    items = items[:MAX_ITEMS]

    payload = {'generated': iso(NOW), 'count': len(items),
               'sources': {'sam_gov': bool(os.environ.get('SAM_GOV_API_KEY')),
                           'x_api': bool(os.environ.get('X_BEARER_TOKEN'))},
               'items': items}
    json.dump(payload, open(os.path.join(DATA, 'feed.json'), 'w'))
    with open(os.path.join(DATA, 'feed.js'), 'w') as f:
        f.write('window.NIT_FEED=' + json.dumps(payload) + ';')
    with open(os.path.join(DATA, 'watchlist.js'), 'w') as f:
        f.write('window.NIT_WATCHLIST=' + json.dumps(WL) + ';')
    print(f'feed: {len(items)} items · generated {payload["generated"]}')

if __name__ == '__main__':
    main()
