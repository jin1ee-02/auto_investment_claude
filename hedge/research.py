"""Research queue: one TradingAgents run at a time, each in a child process.

Research never places orders. Its only output is a 5-tier rating plus the reports,
which the trading module later turns into a plan the user has to confirm.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

from . import config, db

RATINGS = ('Buy', 'Overweight', 'Hold', 'Underweight', 'Sell')
FRESH_DAYS = 14
RUN_TIMEOUT_SECONDS = 60 * 60

_wake = threading.Event()
_lock = threading.Lock()
_current = {'id': None, 'proc': None}
_started = False


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def us_today():
    """Current US market date, so the analysis date is never in the future over there."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    except Exception:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).date().isoformat()


def research_symbol(ticker):
    """13F tickers use '.', the market-data vendor uses '-' for share classes (BRK.B -> BRK-B)."""
    return re.sub(r'\.([A-Z])$', r'-\1', ticker.upper())


def is_us_ticker(ticker):
    return bool(ticker and re.fullmatch(r'[A-Z]{1,5}([.\-][A-Z])?', ticker))


def public(row, full=False):
    out = {k: row[k] for k in ('id', 'ticker', 'trade_date', 'status', 'stage', 'rating', 'error', 'provider',
                               'deep_model', 'quick_model', 'created_at', 'started_at', 'finished_at')}
    out['context'] = json.loads(row['context']) if row['context'] else None
    if full:
        out['reports'] = json.loads(row['reports']) if row['reports'] else None
    return out


def list_jobs():
    return [public(r) for r in db.rows('SELECT * FROM research ORDER BY id DESC LIMIT 300')]


def get(job_id):
    row = db.one('SELECT * FROM research WHERE id=?', (job_id,))
    return public(row, full=True) if row else None


def latest_by_ticker(fresh_days=FRESH_DAYS):
    """Newest finished, still-fresh research per ticker."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=fresh_days)).isoformat(timespec='seconds')
    out = {}
    for r in db.rows("SELECT * FROM research WHERE status='done' AND finished_at>=? ORDER BY id", (cutoff,)):
        out[r['ticker']] = public(r)
    return out


def enqueue(tickers, context=None, force=False):
    """Queue tickers. Fresh finished research and already queued/running jobs are reused."""
    rc = config.research_config()
    if not rc['ready']:
        raise ValueError('리서치 모델 설정이 없습니다 (.env 의 RESEARCH_* 와 API 키)')
    fresh = {} if force else latest_by_ticker()
    queued, reused, rejected = [], [], []
    for raw in tickers:
        ticker = (raw or '').strip().upper()
        if not is_us_ticker(ticker):
            rejected.append({'ticker': raw, 'reason': '미국 상장 티커가 아닙니다'})
            continue
        active = db.one("SELECT id FROM research WHERE ticker=? AND status IN ('queued','running')", (ticker,))
        if active:
            reused.append({'ticker': ticker, 'id': active['id'], 'reason': '이미 대기·진행 중'})
            continue
        if ticker in fresh:
            reused.append({'ticker': ticker, 'id': fresh[ticker]['id'], 'reason': f'최근 {FRESH_DAYS}일 내 결과 재사용'})
            continue
        cur = db.run('INSERT INTO research(ticker,trade_date,status,stage,context,provider,deep_model,quick_model) '
                     "VALUES(?,?,'queued','대기',?,?,?,?)",
                     (ticker, us_today(), json.dumps((context or {}).get(ticker), ensure_ascii=False),
                      rc['provider'], rc['deepModel'], rc['quickModel']))
        queued.append({'ticker': ticker, 'id': cur.lastrowid})
    _wake.set()
    return {'queued': queued, 'reused': reused, 'rejected': rejected}


def cancel(job_id):
    with _lock:
        row = db.one('SELECT status FROM research WHERE id=?', (job_id,))
        if not row:
            return False
        if row['status'] == 'queued':
            db.run("UPDATE research SET status='cancelled', stage='', finished_at=? WHERE id=?", (now(), job_id))
        elif row['status'] == 'running' and _current['id'] == job_id and _current['proc']:
            db.run("UPDATE research SET status='cancelled', stage='', finished_at=? WHERE id=?", (now(), job_id))
            _current['proc'].kill()
        return True


def delete(job_id):
    db.run("DELETE FROM research WHERE id=? AND status NOT IN ('queued','running')", (job_id,))


def start():
    global _started
    if _started:
        return
    _started = True
    # A previous server process may have died mid-run; those jobs cannot be resumed.
    db.run("UPDATE research SET status='error', stage='', error='서버가 재시작되어 중단되었습니다', finished_at=? "
           "WHERE status='running'", (now(),))
    threading.Thread(target=_loop, name='research', daemon=True).start()


def _loop():
    while True:
        job = db.one("SELECT * FROM research WHERE status='queued' ORDER BY id LIMIT 1")
        if not job:
            _wake.wait(5)
            _wake.clear()
            continue
        try:
            _run(job)
        except Exception as exc:
            db.run("UPDATE research SET status='error', stage='', error=?, finished_at=? WHERE id=? AND status='running'",
                   (f'{type(exc).__name__}: {exc}'[:2000], now(), job['id']))


def _run(job):
    job_id = job['id']
    fd, out_path = tempfile.mkstemp(suffix='.json', prefix='research-')
    os.close(fd)
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}
    with _lock:
        if db.one('SELECT status FROM research WHERE id=?', (job_id,))['status'] != 'queued':
            return
        db.run("UPDATE research SET status='running', stage='시작', started_at=? WHERE id=?", (now(), job_id))
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'hedge.ta_worker', research_symbol(job['ticker']), job['trade_date'], out_path],
            cwd=str(config.ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _current.update(id=job_id, proc=proc)
    stderr_tail = []
    threading.Thread(target=lambda: stderr_tail.extend(proc.stderr.readlines()[-30:]), daemon=True).start()
    killer = threading.Timer(RUN_TIMEOUT_SECONDS, proc.kill)
    killer.start()
    error = None
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue    # libraries occasionally print to stdout
            if event.get('event') == 'stage':
                db.run("UPDATE research SET stage=? WHERE id=? AND status='running'",
                       (f"{event['stage']} ({event['done']}/{event['total']})", job_id))
            elif event.get('event') == 'error':
                error = event.get('message')
        code = proc.wait()
    finally:
        killer.cancel()
        with _lock:
            _current.update(id=None, proc=None)
    try:
        if db.one('SELECT status FROM research WHERE id=?', (job_id,))['status'] != 'running':
            return      # cancelled while running
        result = None
        if code == 0:
            with open(out_path, encoding='utf-8') as f:
                result = json.load(f)
        if result and result.get('rating') in RATINGS:
            db.run("UPDATE research SET status='done', stage='', rating=?, reports=?, finished_at=? WHERE id=?",
                   (result['rating'], json.dumps(result['reports'], ensure_ascii=False), now(), job_id))
        elif result:
            # Upstream returns REVIEW when the final decision carries no parseable rating.
            db.run("UPDATE research SET status='error', stage='', reports=?, error=?, finished_at=? WHERE id=?",
                   (json.dumps(result['reports'], ensure_ascii=False),
                    '최종 판단에서 등급을 읽지 못했습니다 (REVIEW). 보고서를 직접 확인하세요.', now(), job_id))
        else:
            detail = error or ''.join(stderr_tail)[-1500:].strip() or f'종료 코드 {code}'
            db.run("UPDATE research SET status='error', stage='', error=?, finished_at=? WHERE id=?",
                   (detail, now(), job_id))
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
