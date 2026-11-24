"""13F filing sources.

Two interchangeable providers return the same shape:

  list_filings(cik) -> (fund_name, [Filing])           newest first
  holdings(filing)  -> [row]   row = dict(ticker, cusip, name, cls, kind, value, shares)

* EDGAR   - the official source. SEC rejects automated requests from some networks
            (HTTP 403/429); a rejection opens a cooldown so we never hammer it.
* 13f.info - a free third-party mirror of the same filings that already carries tickers.
            Used automatically when EDGAR is unavailable.

Values are US dollars. Rows are aggregated per (key, kind) where key is the ticker
when known, otherwise the CUSIP.
"""
import datetime as dt
import html as html_lib
import json
import re
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from . import config, db, funds_seed

EDGAR_COOLDOWN_SECONDS = 6 * 3600
APP_UA = 'hedge-insight-claude/1.0 (personal research tool)'


class SourceError(RuntimeError):
    pass


class SourceBlocked(SourceError):
    pass


@dataclass
class Filing:
    cik: str
    period: str          # report date, YYYY-MM-DD (quarter end)
    accession: str       # 18 digits, no dashes ('+'-joined in storage when several filings were merged)
    form: str
    filed_at: str
    source: str
    url: str
    count: int | None = None
    partial: bool = False    # a "new holdings" amendment: rows to add to the period's table, not a table


class _Throttle:
    """Serial, spaced requests per host."""

    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            delay = self.interval - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


_client = httpx.Client(timeout=60, follow_redirects=True)
_throttles = {'sec': _Throttle(0.5), '13f': _Throttle(1.0), 'figi': _Throttle(2.6)}


def quarter_end(date_text):
    """Snap any date to its calendar quarter end (13F report dates are quarter ends)."""
    d = dt.date.fromisoformat(date_text[:10])
    month = ((d.month - 1) // 3 + 1) * 3
    last = (dt.date(d.year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))
    return last.isoformat()


def previous_period(period):
    d = dt.date.fromisoformat(quarter_end(period))
    return (dt.date(d.year, d.month - 2, 1) - dt.timedelta(days=1)).isoformat()


def period_label(period):
    d = dt.date.fromisoformat(period)
    return f'{d.year} Q{(d.month - 1) // 3 + 1}'


def pick_filings(filings, quarters):
    """One filing per period: the latest-filed one that looks like a full table.

    Amendments are either full restatements or small "new holdings" additions. Where the
    source does not say which (EDGAR's filing list), a restatement has roughly the
    original's row count, so anything under half of the period's largest filing is
    treated as a partial addition and skipped. A period with only additions is skipped.
    """
    by_period = {}
    for f in filings:
        if not f.partial:
            by_period.setdefault(f.period, []).append(f)
    chosen = []
    for period in sorted(by_period, reverse=True)[:quarters]:
        group = by_period[period]
        largest = max((f.count or 0) for f in group)
        full = [f for f in group if f.count is None or f.count >= largest * 0.5]
        chosen.append(max(full or group, key=lambda f: (f.filed_at, f.accession)))
    return chosen


def additions(filings, base):
    """Marked "new holdings" amendments that add rows to `base` (filed with or after it)."""
    return sorted((f for f in filings if f.partial and f.period == base.period and f.filed_at >= base.filed_at),
                  key=lambda f: (f.filed_at, f.accession))


def aggregate(rows):
    merged = {}
    for r in rows:
        key = (r['ticker'] or r['cusip'], r['kind'])
        m = merged.get(key)
        if m is None:
            merged[key] = dict(r, key=key[0])
        else:
            m['value'] += r['value']
            m['shares'] += r['shares']
    return [m for m in merged.values() if m['value'] > 0 or m['shares'] > 0]


# ───────────────────────────── 13f.info ─────────────────────────────

class ThirteenFInfo:
    name = '13f.info'
    base = 'https://13f.info'

    def _get(self, path):
        _throttles['13f'].wait()
        try:
            r = _client.get(self.base + path, headers={'User-Agent': APP_UA})
        except httpx.HTTPError as exc:
            raise SourceError(f'13f.info 연결 실패: {type(exc).__name__}') from None
        if r.status_code == 404:
            raise SourceError('13f.info 에 해당 기관이 없습니다')
        if r.status_code in (403, 429):
            raise SourceBlocked(f'13f.info 가 요청을 거부했습니다 (HTTP {r.status_code})')
        if r.status_code != 200:
            raise SourceError(f'13f.info HTTP {r.status_code}')
        return r

    # Form column: the original is "13F-HR"; amendments are labelled by what they do.
    FORMS = {'RESTATEMENT': ('13F-HR/A', False), 'NEW HOLDINGS': ('13F-HR/A', True)}

    def list_filings(self, cik):
        return self.parse_manager(cik, self._get(f'/manager/{int(cik):010d}').text)

    def parse_manager(self, cik, html):
        name = re.search(r'<h1[^>]*>\s*(.*?)\s*</h1>', html, re.S)
        table = html.split('id="managerFilings"', 1)
        if len(table) < 2:
            raise SourceError('13f.info 페이지 형식이 바뀌었습니다')
        filings = []
        for block in table[1].split('<tr')[2:]:
            head = re.search(r'data-order="(\d{4}-\d{2}-\d{2})">\s*<a href="(/13f/(\d{18})[^"]*)"', block)
            form = re.search(r'text-center truncate" title="([^"]+)"', block)
            filed = re.findall(r'data-order="(\d{4}-\d{2}-\d{2})"', block)
            count = re.search(r'text-right">\s*([\d,]+)\s*</td>', block)
            if not head or not form or len(filed) < 2:
                continue
            label = form.group(1).strip().upper()
            if not label.startswith('13F-HR') and label not in self.FORMS:
                continue
            form, partial = self.FORMS.get(label, (label, False))
            filings.append(Filing(str(int(cik)), quarter_end(head.group(1)), head.group(3), form,
                                  filed[-1], self.name, self.base + head.group(2),
                                  int(count.group(1).replace(',', '')) if count else None, partial))
        return (re.sub(r'\s+', ' ', name.group(1)) if name else ''), filings

    def holdings(self, filing):
        data = self._get(f'/data/13f/{filing.accession}').json().get('data')
        if not isinstance(data, list):
            raise SourceError('13f.info 데이터 형식이 바뀌었습니다')
        rows = []
        for sym, issuer, cls, cusip, value, _pct, shares, principal, option in data:
            kind = (option or '').upper()
            if kind not in ('PUT', 'CALL'):
                kind = 'PRN' if shares is None and principal is not None else ''
            rows.append({'ticker': normalize_ticker(sym), 'cusip': (cusip or '').upper(), 'name': issuer or '',
                         'cls': cls or '', 'kind': kind, 'value': float(value or 0) * 1000,
                         'shares': float(shares if shares is not None else principal or 0)})
        return rows

    def search(self, query):
        _throttles['13f'].wait()
        r = _client.get(self.base + '/search/autocomplete', params={'q': query}, headers={'User-Agent': APP_UA})
        r.raise_for_status()
        return self.parse_search(r.text)

    @staticmethod
    def parse_search(html):
        """Managers from the site's search box, which answers with an HTML list (managers, then investments)."""
        managers = html.split('>Investments<', 1)[0]
        out = []
        for cik, body in re.findall(r'<a href="/manager/(\d{10})[^"]*">(.*?)</a>', managers, re.S):
            text = [html_lib.unescape(re.sub(r'\s+', ' ', t).strip()) for t in re.findall(r'<div[^>]*>([^<]*)', body)]
            text = [t for t in text if t]
            out.append({'cik': str(int(cik)), 'name': text[0] if text else '', 'location': text[1] if len(text) > 1 else ''})
        return out


# ───────────────────────────── SEC EDGAR ─────────────────────────────

def parse_information_table(xml_bytes, values_in_thousands=False):
    """Parse a 13F information table, ignoring XML namespaces."""
    rows = []
    root = ET.fromstring(xml_bytes)
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] != 'infoTable':
            continue
        f = {child.tag.rsplit('}', 1)[-1]: child for child in node.iter()}
        text = lambda tag: (f[tag].text or '').strip() if tag in f else ''
        try:
            value = float(text('value').replace(',', '') or 0)
            amount = float(text('sshPrnamt').replace(',', '') or 0)
        except ValueError:
            continue
        kind = text('putCall').upper()
        if kind not in ('PUT', 'CALL'):
            kind = 'PRN' if text('sshPrnamtType').upper() == 'PRN' else ''
        rows.append({'ticker': None, 'cusip': text('cusip').upper(), 'name': text('nameOfIssuer'),
                     'cls': text('titleOfClass'), 'kind': kind,
                     'value': value * (1000 if values_in_thousands else 1), 'shares': amount})
    return rows


class Edgar:
    name = 'SEC EDGAR'

    def available(self):
        return time.time() >= (db.kv_get('edgar_cooldown_until') or 0)

    def _get(self, url):
        ua = config.env('SEC_USER_AGENT')
        if not ua or 'example.com' in ua:
            raise SourceError('SEC_USER_AGENT 가 설정되지 않았습니다')
        if not self.available():
            raise SourceBlocked('SEC 차단 대기 중')
        _throttles['sec'].wait()
        try:
            r = _client.get(url, headers={'User-Agent': ua, 'Accept-Encoding': 'gzip, deflate'})
        except httpx.HTTPError as exc:
            raise SourceError(f'SEC 연결 실패: {type(exc).__name__}') from None
        if r.status_code in (403, 429):
            db.kv_set('edgar_cooldown_until', time.time() + EDGAR_COOLDOWN_SECONDS)
            raise SourceBlocked(f'SEC 가 이 네트워크의 자동 요청을 거부했습니다 (HTTP {r.status_code})')
        if r.status_code == 404:
            raise SourceError('SEC 에 해당 CIK 가 없습니다')
        if r.status_code != 200:
            raise SourceError(f'SEC HTTP {r.status_code}')
        return r

    def list_filings(self, cik):
        data = self._get(f'https://data.sec.gov/submissions/CIK{int(cik):010d}.json').json()
        return data.get('name', ''), self.filings_from_submissions(cik, data)

    @staticmethod
    def filings_from_submissions(cik, data):
        recent = data['filings']['recent']
        filings = []
        for form, report, filed, accession in zip(recent['form'], recent['reportDate'],
                                                  recent['filingDate'], recent['accessionNumber']):
            if form in ('13F-HR', '13F-HR/A') and report:
                plain = accession.replace('-', '')
                filings.append(Filing(str(int(cik)), quarter_end(report), plain, form, filed, 'SEC EDGAR',
                                      f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{plain}/'))
        return filings

    @staticmethod
    def table_name(index):
        items = [i for i in index['directory']['item'] if i['name'].lower().endswith('.xml')
                 and i['name'].lower() != 'primary_doc.xml']
        if not items:
            raise SourceError('공시에 보유 내역 XML 이 없습니다')
        return max(items, key=lambda i: int(i.get('size') or 0))['name']

    def holdings(self, filing):
        index = self._get(filing.url + 'index.json').json()
        xml = self._get(filing.url + self.table_name(index)).content
        return parse_information_table(xml, values_in_thousands=filing.filed_at < '2023-01-03')


# ───────────────────────────── CUSIP → ticker ─────────────────────────────

def normalize_ticker(symbol):
    if not symbol:
        return None
    symbol = symbol.strip().upper().replace('/', '.')
    return symbol if re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,9}', symbol) else None


def finalize(rows, lookup_limit=300):
    """Give every CUSIP one canonical ticker, then aggregate.

    The first ticker ever stored for a CUSIP wins, whichever source it came from, so the
    same security always lands on the same key and quarter-over-quarter diffs line up
    even when two quarters were fetched from different sources.
    """
    cusips = sorted({r['cusip'] for r in rows if r['cusip']})
    known = {}
    for i in range(0, len(cusips), 500):
        chunk = cusips[i:i + 500]
        for row in db.rows(f'SELECT cusip,ticker FROM securities WHERE cusip IN ({",".join("?" * len(chunk))})', chunk):
            known[row['cusip']] = row['ticker']
    fresh = {}
    for r in rows:
        if r['cusip'] and r['ticker'] and not known.get(r['cusip']):
            fresh.setdefault(r['cusip'], (r['ticker'], r['name']))
    if fresh:
        with db.tx() as c:
            c.executemany('INSERT INTO securities(cusip,ticker,name) VALUES(?,?,?) '
                          'ON CONFLICT(cusip) DO UPDATE SET ticker=excluded.ticker,name=excluded.name',
                          [(cusip, t, n) for cusip, (t, n) in fresh.items()])
        known.update({cusip: t for cusip, (t, _n) in fresh.items()})
    # Unknown CUSIPs (EDGAR tables carry no tickers): ask OpenFIGI for the largest ones, once.
    by_value = {}
    for r in rows:
        if r['cusip'] and r['cusip'] not in known and r['kind'] != 'PRN':
            by_value[r['cusip']] = by_value.get(r['cusip'], 0) + r['value']
    missing = sorted(by_value, key=by_value.get, reverse=True)[:lookup_limit]
    if missing:
        known.update(openfigi_lookup(missing))
    for r in rows:
        r['ticker'] = known.get(r['cusip']) or None
    return aggregate(rows)


def openfigi_lookup(cusips):
    key = config.env('OPENFIGI_API_KEY')
    batch = 100 if key else 10
    headers = {'Content-Type': 'application/json', **({'X-OPENFIGI-APIKEY': key} if key else {})}
    found = {}
    for i in range(0, len(cusips), batch):
        chunk = cusips[i:i + batch]
        if not key:
            _throttles['figi'].wait()
        try:
            r = _client.post('https://api.openfigi.com/v3/mapping', headers=headers,
                             content=json.dumps([{'idType': 'ID_CUSIP', 'idValue': c, 'exchCode': 'US'} for c in chunk]))
            if r.status_code != 200:
                break
            results = r.json()
        except (httpx.HTTPError, ValueError):
            break
        with db.tx() as c:
            for cusip, item in zip(chunk, results):
                data = item.get('data') or []
                ticker = normalize_ticker(data[0].get('ticker')) if data else None
                found[cusip] = ticker
                c.execute('INSERT INTO securities(cusip,ticker,name) VALUES(?,?,?) '
                          'ON CONFLICT(cusip) DO UPDATE SET ticker=excluded.ticker',
                          (cusip, ticker, data[0].get('name', '') if data else ''))
    return found


# ───────────────────────────── orchestration ─────────────────────────────

EDGAR = Edgar()
INFO = ThirteenFInfo()


def providers():
    mode = config.env('FILINGS_SOURCE', 'auto').lower()
    if mode == 'edgar':
        return [EDGAR]
    if mode == '13finfo':
        return [INFO]
    return [EDGAR, INFO] if EDGAR.available() else [INFO]


def source_status():
    until = db.kv_get('edgar_cooldown_until') or 0
    return {'mode': config.env('FILINGS_SOURCE', 'auto').lower(),
            'edgarBlockedUntil': dt.datetime.fromtimestamp(until, dt.timezone.utc).isoformat() if until > time.time() else None,
            'active': providers()[0].name}


HISTORY_QUARTERS = 8     # two years: enough for performance and AUM history


def refresh_fund(cik, quarters=HISTORY_QUARTERS, force=False):
    """Fetch the newest `quarters` filings of one fund.

    Returns (periods stored, changed). `changed` is False when every filing was already
    stored as is and the fund's name did not move, so cached analysis can be kept.
    """
    cik = str(int(cik))
    before = db.one('SELECT name FROM funds WHERE cik=?', (cik,))
    last_error = None
    for provider in providers():
        try:
            name, filings = provider.list_filings(cik)
            chosen = pick_filings(filings, quarters)
            if not chosen:
                raise SourceError('13F-HR 공시가 없습니다')
            former = [f for old in funds_seed.PREDECESSORS.get(cik, ())
                      for f in pick_filings(provider.list_filings(old)[1], 99)]
            stored, wrote = [], False
            for filing in chosen:
                # One period's table = the chosen filing + later "new holdings" amendments
                # + what a predecessor filer reported for the same period.
                parts = [filing] + additions(filings, filing) + [f for f in former if f.period == filing.period]
                merged = '+'.join(p.accession for p in parts)
                existing = db.one('SELECT accession FROM filings WHERE cik=? AND period=?', (cik, filing.period))
                if existing and existing['accession'] == merged and not force:
                    stored.append(filing.period)
                    continue
                rows = finalize([r for p in parts for r in provider.holdings(p)])
                filing.accession = merged
                store_filing(filing, rows)
                stored.append(filing.period)
                wrote = True
            with db.tx() as c:
                c.execute('INSERT INTO funds(cik,name) VALUES(?,?) ON CONFLICT(cik) DO NOTHING', (cik, name or cik))
                c.execute("UPDATE funds SET last_refresh=?, last_error=NULL, name=CASE WHEN featured=1 THEN name ELSE ? END "
                          'WHERE cik=?', (dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), name or cik, cik))
                if name:
                    db.kv_set(f'filer_name:{cik}', name, cursor=c)
            after = db.one('SELECT name FROM funds WHERE cik=?', (cik,))
            return stored, wrote or before is None or before['name'] != after['name']
        except SourceError as exc:
            last_error = exc
            continue
    db.run('UPDATE funds SET last_error=? WHERE cik=?', (str(last_error), cik))
    raise last_error


def store_filing(filing, rows):
    equity = [r for r in rows if r['kind'] != 'PRN']
    with db.tx() as c:
        c.execute('DELETE FROM holdings WHERE cik=? AND period=?', (filing.cik, filing.period))
        c.executemany('INSERT INTO holdings(cik,period,key,kind,ticker,cusip,name,cls,value,shares) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      [(filing.cik, filing.period, r['key'], r['kind'], r['ticker'], r['cusip'], r['name'], r['cls'],
                        r['value'], r['shares']) for r in rows])
        c.execute('INSERT INTO filings(cik,period,accession,form,filed_at,total_value,positions,source,source_url,fetched_at) '
                  'VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(cik,period) DO UPDATE SET '
                  'accession=excluded.accession,form=excluded.form,filed_at=excluded.filed_at,total_value=excluded.total_value,'
                  'positions=excluded.positions,source=excluded.source,source_url=excluded.source_url,fetched_at=CURRENT_TIMESTAMP',
                  (filing.cik, filing.period, filing.accession, filing.form, filing.filed_at,
                   sum(r['value'] for r in rows), len(equity), filing.source, filing.url))
        _write_history(c, filing.cik, filing.period, filing.accession,
                       [(r['key'], r['value'], r['shares']) for r in rows if r['kind'] == '' and r['shares'] > 0])


# ───────────────────────────── long history (cost basis) ─────────────────────────────

HISTORY_START = '2013-06-30'     # 13F information tables are XML from 2013 Q2; older ones are free text


def _write_history(c, cik, period, accession, rows):
    """Replace one fund-quarter in `history`. rows: [(key, value, shares)]."""
    c.execute('DELETE FROM history WHERE cik=? AND period=?', (cik, period))
    c.executemany('INSERT INTO history(key,period,cik,value,shares) VALUES(?,?,?,?,?)',
                  [(key, period, cik, value, shares) for key, value, shares in rows])
    c.execute('INSERT INTO history_filings(cik,period,accession) VALUES(?,?,?) '
              'ON CONFLICT(cik,period) DO UPDATE SET accession=excluded.accession', (cik, period, accession))


def sync_history():
    """Copy recent quarters already in `holdings` into `history` (databases made before it existed)."""
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO history(key,period,cik,value,shares) "
                  "SELECT key, period, cik, value, shares FROM holdings WHERE kind='' AND shares>0")
        c.execute('INSERT OR IGNORE INTO history_filings(cik,period,accession) SELECT cik, period, accession FROM filings')


def history_universe():
    """Securities some tracked fund holds in the recent quarters: the only ones whose past matters."""
    return {key for (key,) in db.run("SELECT DISTINCT key FROM holdings WHERE kind=''")}


def history_rows(rows, known, universe):
    """Raw 13F rows -> [(key, value, shares)] for share positions in `universe`.

    A CUSIP gets the canonical ticker it already has in `securities`; a CUSIP never seen
    before falls back to the filing's own ticker (Alphabet kept GOOGL through its CUSIP
    change). Nothing is written to `securities`, so an old ticker such as FB can never
    become the canonical key of a security that is still traded today.
    """
    merged = {}
    for r in rows:
        if r['kind'] != '':
            continue
        key = known.get(r['cusip']) or normalize_ticker(r['ticker']) or r['cusip']
        if key in universe:
            value, shares = merged.get(key, (0.0, 0.0))
            merged[key] = (value + r['value'], shares + r['shares'])
    return [(key, value, shares) for key, (value, shares) in merged.items() if shares > 0]


SPLIT_TICKER = re.compile(r'[A-Z]{1,5}(\.[A-Z])?')


def split_candidates():
    """{key: latest quarter} for tickers whose implied price fell under 0.6x or rose over 1.8x in a quarter.

    Those jumps are where a split may hide; ordinary quarters never need a lookup.
    """
    prices = {}
    for key, period, value, shares in db.run('SELECT key, period, value, shares FROM history WHERE value>0 AND shares>0'):
        prices.setdefault(key, {}).setdefault(period, []).append(value / shares)
    out = {}
    for key, by_period in prices.items():
        if not SPLIT_TICKER.fullmatch(key):
            continue
        median = {q: statistics.median(v) for q, v in by_period.items()}
        for q, price in median.items():
            before = median.get(previous_period(q))
            if before and not 0.6 < price / before < 1.8:
                out[key] = max(out.get(key, ''), q)
    return out


def yahoo_splits(keys):
    """{key: {quarter end: factor}} from Yahoo Finance; keys Yahoo has no prices for are left out."""
    import yfinance as yf
    symbols = {key: re.sub(r'\.([A-Z])$', r'-\1', key) for key in keys}    # BRK.B -> BRK-B
    wanted = sorted(set(symbols.values()) | {'SPY'})                     # two or more keep the column layout fixed
    frame = yf.download(wanted, start=HISTORY_START[:4] + '-01-01', interval='3mo', actions=True,
                        auto_adjust=False, progress=False, threads=True, group_by='column')
    out = {}
    for key, symbol in symbols.items():
        if symbol not in frame['Close'] or frame['Close'][symbol].dropna().empty:
            continue
        events = {}
        for day, factor in frame['Stock Splits'][symbol].items():
            if factor == factor and factor > 0 and factor != 1:             # NaN-safe
                quarter = quarter_end(day.date().isoformat())
                events[quarter] = events.get(quarter, 1.0) * float(factor)
        out[key] = events
    return out


def check_splits(progress=None, batch=100, pause=8.0):
    """Look up splits for candidate tickers not yet checked through their latest jump. Returns keys checked.

    Biggest positions go first. Tickers Yahoo has no prices for are recorded in `split_unknown`
    and not asked again. Yahoo also rate-limits bulk downloads, which looks the same (nothing
    comes back), so an empty batch is told apart by asking for AAPL alone: if that is empty too,
    the run stops and the rest waits for the next run (the scheduler retries hourly).
    """
    upto = (db.one('SELECT MAX(period) p FROM history') or {}).get('p')
    if not upto:
        return 0
    seen = {r['key']: r['upto'] for r in db.rows('SELECT key, upto FROM split_checks UNION ALL '
                                                  'SELECT key, upto FROM split_unknown')}
    size = {k: v for k, v in db.run('SELECT key, MAX(value) FROM history GROUP BY key')}
    todo = sorted((k for k, q in split_candidates().items() if seen.get(k, '') < q), key=lambda k: -size.get(k, 0))
    done = 0
    for i in range(0, len(todo), batch):
        if progress:
            progress(i, len(todo))
        if i:
            time.sleep(pause)
        chunk = todo[i:i + batch]
        found = yahoo_splits(chunk)
        if not found and not yahoo_splits(['AAPL']):
            break                                   # rate-limited (or Yahoo is down): try again later
        with db.tx() as c:
            c.executemany('INSERT INTO split_unknown(key,upto) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET upto=excluded.upto',
                          [(key, upto) for key in chunk if key not in found])
            for key, events in found.items():
                c.execute('DELETE FROM splits WHERE key=?', (key,))
                c.executemany('INSERT INTO splits(key,period,factor) VALUES(?,?,?)',
                              [(key, q, f) for q, f in events.items()])
                c.execute('INSERT INTO split_checks(key,upto) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET upto=excluded.upto',
                          (key, upto))
        done += len(found)
    return done


def backfill_history(cik, universe, known):
    """Fetch every older quarter of one fund from 13f.info into `history`. Returns quarters added.

    Quarters already on record are skipped, so a second run only asks for the filing list.
    """
    cik = str(int(cik))
    have = {r['period'] for r in db.rows('SELECT period FROM history_filings WHERE cik=?', (cik,))}
    _name, filings = INFO.list_filings(cik)
    former = [f for old in funds_seed.PREDECESSORS.get(cik, ()) for f in INFO.list_filings(old)[1]]
    chosen = {f.period: [f] + additions(filings, f) for f in pick_filings(filings, 999)}
    for f in pick_filings(former, 999):           # a predecessor filer's quarters, merged as refresh_fund does
        chosen.setdefault(f.period, []).extend([f] + additions(former, f))
    added = 0
    for period in sorted(chosen, reverse=True):
        if period in have or period < HISTORY_START:
            continue
        parts = chosen[period]
        rows = history_rows([r for p in parts for r in INFO.holdings(p)], known, universe)
        with db.tx() as c:
            _write_history(c, cik, period, '+'.join(p.accession for p in parts), rows)
        added += 1
    return added


def import_snapshot(path):
    """Fill missing (fund, period) pairs from a filings.json written by the Codex
    edition of this project (SEC originals collected earlier). Never overwrites."""
    data = json.loads(open(path, encoding='utf-8').read())
    added = []
    for fund in data.get('funds', []):
        cik = str(int(fund['cik']))
        if not db.one('SELECT 1 FROM funds WHERE cik=?', (cik,)):
            continue
        for snap in fund.get('snapshots', []):
            period = quarter_end(snap['period'])
            if not snap.get('complete', True) or db.one('SELECT 1 FROM filings WHERE cik=? AND period=?', (cik, period)):
                continue
            rows = []
            for p in snap.get('positions', []):
                kind = (p.get('kind') or '').upper()
                if kind not in ('PUT', 'CALL', 'PRN'):
                    # The snapshot does not mark debt principal rows; the class title does.
                    kind = 'PRN' if re.search(r'BOND|NOTE|DEBT|DBCV|CONV', p.get('class', ''), re.I) else ''
                rows.append({'ticker': normalize_ticker(p.get('ticker')), 'cusip': (p.get('cusip') or '').upper(),
                             'name': p.get('name', ''), 'cls': p.get('class', ''), 'kind': kind,
                             'value': float(p.get('value') or 0), 'shares': float(p.get('shares') or 0)})
            rows = finalize(rows, lookup_limit=0)
            store_filing(Filing(cik, period, (snap.get('accession') or '').replace('-', ''), '13F-HR',
                                snap.get('filedAt', ''), 'SEC EDGAR (가져온 스냅샷)', snap.get('sourceUrl', '')), rows)
            added.append((cik, period))
    return added
