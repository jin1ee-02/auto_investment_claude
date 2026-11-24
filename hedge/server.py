"""Local web app: python -m hedge.server  ->  http://127.0.0.1:8765

Bound to localhost only. State-changing requests must be JSON sent from the app's own
origin, so another website open in the same browser cannot trigger a refresh or an order.
"""
import datetime as dt
import threading
import time
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import analysis, config, db, funds_seed, research, sources, toss, trading

app = FastAPI(title='Hedge Insight', docs_url=None, redoc_url=None, openapi_url=None)
AUTO_REFRESH_HOURS = 12


@app.middleware('http')
async def local_only(request: Request, call_next):
    host = (request.headers.get('host') or '').split(':')[0]
    if host not in ('127.0.0.1', 'localhost'):
        return JSONResponse({'detail': 'forbidden host'}, status_code=403)
    if request.method not in ('GET', 'HEAD'):
        origin = request.headers.get('origin')
        if origin and origin.split('://')[-1].split(':')[0] not in ('127.0.0.1', 'localhost'):
            return JSONResponse({'detail': 'forbidden origin'}, status_code=403)
        if 'application/json' not in (request.headers.get('content-type') or ''):
            return JSONResponse({'detail': 'JSON 요청만 허용됩니다'}, status_code=415)
    response = await call_next(request)
    if not request.url.path.startswith('/vendor/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(trading.TradeError)
async def trade_error(_request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


@app.exception_handler(toss.BrokerError)
async def broker_error(_request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=502)


# ───────────────────────────── refresh job ─────────────────────────────

_refresh = {'running': False, 'done': 0, 'total': 0, 'current': '', 'errors': [], 'finishedAt': None, 'newPeriods': []}
_refresh_lock = threading.Lock()


def start_refresh(ciks=None, force=False):
    with _refresh_lock:
        if _refresh['running']:
            return False
        funds = db.rows('SELECT cik,name FROM funds ORDER BY featured DESC, name')
        if ciks:
            funds = [f for f in funds if f['cik'] in set(ciks)]
        _refresh.update(running=True, done=0, total=len(funds), current='', errors=[], newPeriods=[])
    threading.Thread(target=_refresh_worker, args=(funds, force), daemon=True).start()
    return True


def _refresh_worker(funds, force):
    before = set(analysis.periods())
    try:
        for fund in funds:
            _refresh['current'] = fund['name']
            try:
                # Clear cached analysis only when something was stored: a routine refresh that finds
                # nothing new must not make every page recompute from scratch while it runs.
                if sources.refresh_fund(fund['cik'], force=force)[1]:
                    analysis.invalidate()
            except sources.SourceError as exc:
                _refresh['errors'].append({'cik': fund['cik'], 'name': fund['name'], 'error': str(exc)})
            except Exception as exc:
                _refresh['errors'].append({'cik': fund['cik'], 'name': fund['name'], 'error': f'{type(exc).__name__}: {exc}'})
            _refresh['done'] += 1
    finally:
        db.kv_set('last_refresh', time.time())
        _refresh.update(running=False, current='', finishedAt=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                        newPeriods=sorted(set(analysis.periods()) - before))
        start_backfill()


# ───────────────────────────── long-history backfill ─────────────────────────────
# Cost basis needs every quarter since a position was opened, not only the recent ones. Each fund's
# older 13F tables are fetched once; later quarters arrive through the normal refresh. Then splits
# are confirmed from market data wherever an implied price jumped (new jumps only, after the first run).

_backfill = {'running': False, 'done': 0, 'total': 0, 'current': '', 'added': 0, 'splitsChecked': 0, 'errors': []}
_backfill_lock = threading.Lock()


def start_backfill():
    with _backfill_lock:
        if _backfill['running']:
            return False
        funds = [f for f in db.rows('SELECT cik,name FROM funds ORDER BY featured DESC, name')
                 if not db.kv_get(f"history_done:{f['cik']}")]
        _backfill.update(running=True, done=0, total=len(funds), current='', added=0, splitsChecked=0, errors=[])
    threading.Thread(target=_backfill_worker, args=(funds,), name='backfill', daemon=True).start()
    return True


def _backfill_worker(funds):
    try:
        universe = sources.history_universe()
        known = {r['cusip']: r['ticker'] for r in db.rows('SELECT cusip, ticker FROM securities WHERE ticker IS NOT NULL')}
        for fund in funds:
            _backfill['current'] = fund['name']
            try:
                _backfill['added'] += sources.backfill_history(fund['cik'], universe, known)
                db.kv_set(f"history_done:{fund['cik']}", True)
            except Exception as exc:     # retried on the next run; the recent quarters still work meanwhile
                _backfill['errors'].append({'cik': fund['cik'], 'name': fund['name'], 'error': f'{type(exc).__name__}: {exc}'})
            _backfill['done'] += 1
            # Cost bases change with every fund's past (prices are pooled), so the cache is cleared
            # every few funds rather than after each one: pages stay fast while this runs.
            if _backfill['done'] % 5 == 0:
                analysis.invalidate()
        _backfill['current'] = '주식 분할 확인 (Yahoo Finance)'
        try:
            _backfill['splitsChecked'] = sources.check_splits(
                progress=lambda i, n: _backfill.update(current=f'주식 분할 확인 (Yahoo Finance) {i}/{n}종목'))
        except Exception as exc:         # market data is a nicety: the 13F rule still covers splits
            _backfill['errors'].append({'cik': '', 'name': '주식 분할 확인', 'error': f'{type(exc).__name__}: {exc}'})
    finally:
        analysis.invalidate()
        _backfill.update(running=False, current='')


def _scheduler():
    backfill_at = time.time()
    while True:
        last = db.kv_get('last_refresh') or 0
        if time.time() - last > AUTO_REFRESH_HOURS * 3600:
            start_refresh()                  # ends by starting the backfill
        elif time.time() - backfill_at > 3600:
            start_backfill()                 # finishes split lookups a rate limit cut short; quick when nothing is left
            backfill_at = time.time()
        time.sleep(600)


# ───────────────────────────── helpers ─────────────────────────────

def resolve_period(period):
    known = analysis.periods()
    if period and period in known:
        return period
    return analysis.default_period()


def fund_or_404(cik):
    fund = db.one('SELECT * FROM funds WHERE cik=?', (cik,))
    if not fund:
        raise HTTPException(404, '없는 기관입니다')
    return fund


def attach_research(items):
    """Give each item with a ticker the newest research job on it, for the badge next to it."""
    marks = research.latest_marks()
    for item in items:
        item['research'] = marks.get(item['ticker']) if item['ticker'] else None
    return items


def fresh_days():
    return int(trading.settings()['freshDays'])


def selection():
    known = {r['cik'] for r in db.rows('SELECT cik FROM funds')}
    return [c for c in (db.kv_get('selection') or []) if c in known]


# ───────────────────────────── API: status & funds ─────────────────────────────

@app.get('/api/status')
def status():
    known = analysis.periods()
    return {
        'periods': [{'period': p, 'label': sources.period_label(p)} for p in known],
        'defaultPeriod': analysis.default_period(),
        'source': sources.source_status(),
        'refresh': _refresh,
        'backfill': _backfill,
        'lastRefresh': db.kv_get('last_refresh'),
        'research': {k: v for k, v in config.research_config().items()},
        'toss': {'configured': config.toss_configured(), 'liveEnabled': config.live_enabled()},
        'selection': selection(),
    }


@app.get('/api/funds')
def list_funds(period: str | None = None):
    period = resolve_period(period)
    funds = [analysis.fund_summary(f['cik'], period) for f in db.rows('SELECT cik FROM funds ORDER BY featured DESC, name')]
    newest = max((f['latestPeriod'] for f in funds if f['latestPeriod']), default=None)
    filed = sorted((f for f in funds if f['latestFiledAt']), key=lambda f: f['latestFiledAt'], reverse=True)
    return {'period': period, 'periodLabel': sources.period_label(period) if period else None, 'funds': funds,
            'newest': newest and {'period': newest, 'label': sources.period_label(newest),
                                  'filed': sum(1 for f in funds if f['latestPeriod'] == newest)},
            'recent': [{'cik': f['cik'], 'name': f['name'], 'period': f['latestPeriod'], 'label': f['latestLabel'],
                        'filedAt': f['latestFiledAt']} for f in filed[:8]]}


@app.get('/api/funds/search')
def search_funds(q: str):
    if len(q.strip()) < 2:
        return {'results': []}
    try:
        results = sources.INFO.search(q.strip())
    except Exception:
        raise HTTPException(502, '기관 검색에 실패했습니다')
    known = {r['cik'] for r in db.rows('SELECT cik FROM funds')}
    return {'results': [{**r, 'added': r['cik'] in known} for r in results[:12]]}


@app.post('/api/funds')
async def add_fund(request: Request):
    body = await request.json()
    cik = str(body.get('cik', '')).strip()
    if not cik.isdigit() or len(cik) > 10:
        raise HTTPException(400, 'CIK 는 숫자여야 합니다')
    cik = str(int(cik))
    if db.one('SELECT 1 FROM funds WHERE cik=?', (cik,)):
        raise HTTPException(409, '이미 추가된 기관입니다')
    try:
        periods, _changed = sources.refresh_fund(cik)     # verifies the CIK and stores its filings
    except sources.SourceError as exc:
        db.run('DELETE FROM funds WHERE cik=? AND last_refresh IS NULL', (cik,))
        raise HTTPException(404, f'13F 공시를 가져오지 못했습니다: {exc}')
    analysis.invalidate()
    start_backfill()
    return {'cik': cik, 'periods': periods}


@app.delete('/api/funds/{cik}')
def remove_fund(cik: str):
    with db.tx() as c:
        c.execute('DELETE FROM holdings WHERE cik=?', (cik,))
        c.execute('DELETE FROM filings WHERE cik=?', (cik,))
        c.execute('DELETE FROM funds WHERE cik=?', (cik,))
        c.execute('DELETE FROM history WHERE cik=?', (cik,))
        c.execute('DELETE FROM history_filings WHERE cik=?', (cik,))
        c.execute('DELETE FROM kv WHERE key=?', (f'history_done:{cik}',))
    db.kv_set('selection', [c for c in (db.kv_get('selection') or []) if c != cik])
    analysis.invalidate()
    return {'ok': True}


@app.get('/api/funds/{cik}')
def fund_detail(cik: str, period: str | None = None, limit: int = 600):
    fund = fund_or_404(cik)
    available = [r['period'] for r in db.rows('SELECT period FROM filings WHERE cik=? ORDER BY period DESC', (cik,))]
    if not available:
        return {'fund': fund, 'periods': [], 'summary': None, 'positions': []}
    if period not in available:
        default = resolve_period(None)
        period = default if default in available else available[0]
    diff = analysis.fund_diff(cik, period, 1.0)
    positions = attach_research(diff['positions'])
    held = analysis.position_history(cik, period)
    for p in positions:
        p['hist'] = held['positions'].get(p['key']) if p['kind'] == '' else None
        if p['hist'] is None and p['kind'] == '' and p['action'] == 'exit':
            step = analysis.price_step(p['key'], period)
            p['hist'] = {'quarterPricePct': (step - 1) * 100 if step else None}
    ranked = sorted(positions, key=lambda p: -max(p['value'], p['prevValue']))
    return {'fund': fund, 'period': period, 'periodLabel': sources.period_label(period),
            'prevPeriodLabel': sources.period_label(diff['prevPeriod']), 'hasPrev': bool(diff['prevFiling']),
            'periods': [{'period': p, 'label': sources.period_label(p)} for p in available],
            'summary': analysis.fund_summary(cik, period), 'positions': ranked[:limit], 'totalPositions': len(positions),
            'history': analysis.fund_history(cik), 'histPeriods': [sources.period_label(p) for p in held['periods']]}


@app.get('/api/funds/{cik}/track/{key}')
def fund_track(cik: str, key: str, period: str | None = None):
    fund_or_404(cik)
    return analysis.position_track(cik, key, resolve_period(period))


@app.post('/api/refresh')
async def refresh(request: Request):
    body = await request.json()
    started = start_refresh(body.get('ciks'), bool(body.get('force')))
    return {'started': started, 'refresh': _refresh}


@app.put('/api/selection')
async def set_selection(request: Request):
    body = await request.json()
    ciks = [str(c) for c in body.get('ciks', []) if str(c).isdigit()]
    db.kv_set('selection', list(dict.fromkeys(ciks)))
    return {'selection': selection()}


# ───────────────────────────── API: consensus ─────────────────────────────

@app.get('/api/consensus')
def consensus(period: str | None = None, minChange: float = 5.0, limit: int = 150):
    period = resolve_period(period)
    ciks = selection()
    if not period or not ciks:
        return {'period': period, 'funds': [], 'missing': [], 'buys': [], 'sells': [], 'minChange': minChange}
    result = analysis.consensus(ciks, period, max(0.0, min(minChange, 100.0)))
    rows = attach_research(result.pop('rows'))
    for row in rows:
        row['researchable'] = research.is_us_ticker(row['ticker'])
    buys = sorted((r for r in rows if r['buyCount']), key=lambda r: (-r['buyCount'], -r['newCount'], -r['buyWeightDelta']))
    sells = sorted((r for r in rows if r['sellCount']), key=lambda r: (-r['sellCount'], -r['exitCount'], r['sellWeightDelta']))
    return {**result, 'buys': buys[:limit], 'sells': sells[:limit], 'buyTotal': len(buys), 'sellTotal': len(sells)}


# ───────────────────────────── API: research ─────────────────────────────

@app.get('/api/research')
def research_list():
    return {'jobs': research.list_jobs(), 'config': {**config.research_config(), 'freshDays': fresh_days()}}


@app.post('/api/research')
async def research_enqueue(request: Request):
    body = await request.json()
    tickers = body.get('tickers') or []
    if not isinstance(tickers, list) or not 1 <= len(tickers) <= 20:
        raise HTTPException(400, '한 번에 1~20개 종목을 요청할 수 있습니다')
    try:
        return research.enqueue(tickers, body.get('context') or {}, bool(body.get('force')), fresh_days())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get('/api/research/{job_id}')
def research_get(job_id: int):
    job = research.get(job_id)
    if not job:
        raise HTTPException(404, '없는 리서치입니다')
    return job


@app.post('/api/research/{job_id}/cancel')
async def research_cancel(job_id: int):
    return {'ok': research.cancel(job_id)}


@app.delete('/api/research/{job_id}')
def research_delete(job_id: int):
    research.delete(job_id)
    return {'ok': True}


# ───────────────────────────── API: trading ─────────────────────────────

@app.get('/api/plan')
def plan(mode: str = 'paper'):
    return trading.build_plan(mode)


@app.get('/api/orders')
def orders():
    return {'orders': trading.history()}


@app.post('/api/orders')
async def execute(request: Request):
    body = await request.json()
    results = trading.execute(body.get('mode'), body.get('orders') or [], body.get('confirm') or '')
    return {'results': results}


@app.put('/api/settings')
async def put_settings(request: Request):
    return {'settings': trading.update_settings(await request.json())}


@app.post('/api/paper/reset')
async def paper_reset(request: Request):
    trading.reset_paper()
    return {'ok': True}


app.mount('/', StaticFiles(directory=config.WEB, html=True), name='web')


def main():
    config.load_env()
    funds_seed.ensure_seed()
    sources.sync_history()
    research.start()
    start_backfill()
    threading.Thread(target=_scheduler, name='refresh', daemon=True).start()
    port = int(config.env('PORT', '8765'))
    print(f'Hedge Insight → http://127.0.0.1:{port}')
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    main()
