"""SQLite storage. One connection per thread, WAL so the UI can read during a refresh."""
import json
import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = '''
CREATE TABLE IF NOT EXISTS funds(
  cik TEXT PRIMARY KEY, name TEXT NOT NULL, manager TEXT DEFAULT '', style TEXT DEFAULT '',
  featured INTEGER DEFAULT 0, added_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_refresh TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS filings(
  cik TEXT NOT NULL, period TEXT NOT NULL, accession TEXT, form TEXT, filed_at TEXT,
  total_value REAL, positions INTEGER, source TEXT, source_url TEXT, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(cik, period));
-- key = ticker when known, otherwise CUSIP. kind = '' (shares) | PUT | CALL | PRN (debt principal)
CREATE TABLE IF NOT EXISTS holdings(
  cik TEXT NOT NULL, period TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
  ticker TEXT, cusip TEXT, name TEXT, cls TEXT, value REAL NOT NULL, shares REAL NOT NULL,
  PRIMARY KEY(cik, period, key, kind));
CREATE INDEX IF NOT EXISTS holdings_period_key ON holdings(period, key);
CREATE TABLE IF NOT EXISTS securities(cusip TEXT PRIMARY KEY, ticker TEXT, name TEXT, checked_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS research(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
  status TEXT NOT NULL, stage TEXT DEFAULT '', rating TEXT, reports TEXT, context TEXT, error TEXT,
  provider TEXT, deep_model TEXT, quick_model TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT);
CREATE INDEX IF NOT EXISTS research_ticker ON research(ticker, id);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, mode TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL,
  status TEXT NOT NULL, client_order_id TEXT, broker_order_id TEXT, research_id INTEGER, rating TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS paper_positions(symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, avg_price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Every 13F quarter on record for each fund (back to its first filing on 13f.info), share positions only.
-- Used for cost basis and holding periods, which need more than the recent quarters in `holdings`.
-- Only securities some tracked fund holds in `holdings` are kept, so the table stays small.
CREATE TABLE IF NOT EXISTS history_filings(cik TEXT NOT NULL, period TEXT NOT NULL, accession TEXT, PRIMARY KEY(cik, period));
CREATE TABLE IF NOT EXISTS history(
  key TEXT NOT NULL, period TEXT NOT NULL, cik TEXT NOT NULL, value REAL NOT NULL, shares REAL NOT NULL,
  PRIMARY KEY(key, period, cik)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_cik ON history(cik, period);
-- Stock splits confirmed from market data (Yahoo Finance), by quarter: factor = new shares per old.
-- split_checks says up to which quarter a key was looked up; there, "no row" means "no split".
CREATE TABLE IF NOT EXISTS splits(key TEXT NOT NULL, period TEXT NOT NULL, factor REAL NOT NULL, PRIMARY KEY(key, period));
CREATE TABLE IF NOT EXISTS split_checks(key TEXT PRIMARY KEY, upto TEXT NOT NULL);
-- Tickers Yahoo had no prices for (delisted, renamed): not asked again, left to the 13F split rule.
CREATE TABLE IF NOT EXISTS split_unknown(key TEXT PRIMARY KEY, upto TEXT NOT NULL);
'''

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def conn():
    global _initialized
    c = getattr(_local, 'conn', None)
    if c is None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        with _init_lock:
            if not _initialized:
                c.executescript(SCHEMA)
                _initialized = True
        _local.conn = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute('BEGIN IMMEDIATE')
    try:
        yield c
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise


def rows(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql, args=()):
    return conn().execute(sql, args)


def kv_get(key, default=None):
    r = conn().execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(key, value, cursor=None):
    """Store a JSON value. Pass `cursor` to write inside an open `tx()`."""
    (cursor or conn()).execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(value, ensure_ascii=False)))
