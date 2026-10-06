"""Quarter-over-quarter portfolio changes and cross-fund consensus.

13F tables are quarter-end snapshots, so "bought/sold this quarter" is the difference
between two snapshots. Three things keep that difference honest:

* stock splits  - a 2:1 split looks like every holder doubling. Detected across all
                  stored funds and the previous share count is adjusted.
* tiny changes  - index-like rebalancing below `min_change` percent counts as HOLD.
* options       - PUT/CALL rows are shown per fund but never count as buying or selling.
"""
import statistics
import threading
from functools import lru_cache

from . import db
from .sources import period_label, previous_period

# Forward splits (new shares per old) and reverse splits (1-for-N).
SPLIT_FACTORS = [1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50]
SPLIT_FACTORS += [1 / n for n in (2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 100)]

_version = 0
_version_lock = threading.Lock()


def invalidate():
    """Call after any holdings write."""
    global _version
    with _version_lock:
        _version += 1


def periods():
    return [r['period'] for r in db.rows('SELECT DISTINCT period FROM filings ORDER BY period DESC')]


def default_period():
    """Newest period that at least a third of the tracked funds have filed."""
    counts = db.rows('SELECT period, COUNT(*) n FROM filings GROUP BY period ORDER BY period DESC')
    if not counts:
        return None
    most = max(c['n'] for c in counts)
    for c in counts:
        if c['n'] >= max(1, most / 3):
            return c['period']
    return counts[0]['period']


def market(prev, cur):
    return _market(prev, cur, _version)


@lru_cache(maxsize=8)
def _market(prev, cur, _v):
    """Implied quarter-end prices and split factors, pooled over every stored fund."""
    data = {}
    for r in db.rows("SELECT period, key, cik, value, shares FROM holdings WHERE kind='' AND shares>0 AND value>0 "
                     'AND period IN (?,?)', (prev, cur)):
        data.setdefault(r['key'], {}).setdefault(r['period'], {})[r['cik']] = (r['value'], r['shares'])
    prices, splits = {}, {}
    for key, by_period in data.items():
        p = {period: statistics.median(v / s for v, s in holders.values()) for period, holders in by_period.items()}
        prices[key] = (p.get(prev), p.get(cur))
        if prev not in by_period or cur not in by_period:
            continue
        both = [by_period[cur][c][1] / by_period[prev][c][1] for c in by_period[cur] if c in by_period[prev]]
        if len(both) < 2:
            continue
        price_ratio = p[cur] / p[prev]
        for f in SPLIT_FACTORS:
            hits = sum(1 for ratio in both if abs(ratio / f - 1) < 0.01)
            # A split keeps value roughly constant: the split-adjusted price move must be an
            # ordinary quarterly move, and most continuing holders must show exactly the factor.
            if hits >= 2 and hits >= len(both) * 2 / 3 and 0.6 <= price_ratio * f <= 1.6:
                splits[key] = f
                break
    return prices, splits


def fund_diff(cik, period, min_change=0.0):
    """Positions of one fund at `period` compared with the previous quarter."""
    prev = previous_period(period)
    filing = db.one('SELECT * FROM filings WHERE cik=? AND period=?', (cik, period))
    if not filing:
        return None
    prev_filing = db.one('SELECT * FROM filings WHERE cik=? AND period=?', (cik, prev))
    cur_rows = {(r['key'], r['kind']): r for r in db.rows('SELECT * FROM holdings WHERE cik=? AND period=?', (cik, period))}
    prev_rows = {(r['key'], r['kind']): r for r in db.rows('SELECT * FROM holdings WHERE cik=? AND period=?', (cik, prev))}
    prices, splits = market(prev, period)
    total = filing['total_value'] or 1
    prev_total = (prev_filing['total_value'] if prev_filing else 0) or 1
    positions = []
    for ident in cur_rows.keys() | prev_rows.keys():
        now, was = cur_rows.get(ident), prev_rows.get(ident)
        key, kind = ident
        ref = now or was
        split = splits.get(key, 1) if kind == '' else 1
        shares = now['shares'] if now else 0
        value = now['value'] if now else 0
        prev_shares = was['shares'] * split if was else 0
        prev_value = was['value'] if was else 0
        change = None
        if not prev_filing:
            action = 'hold'
        elif not was:
            action = 'new'
        elif not now:
            action, change = 'exit', -100.0
        else:
            change = (shares / prev_shares - 1) * 100 if prev_shares else 0.0
            action = 'add' if change >= max(min_change, 0.01) else 'reduce' if change <= -max(min_change, 0.01) else 'hold'
        if action == 'exit':
            trade_value = -prev_value
        elif action == 'new':
            trade_value = value
        elif shares and action in ('add', 'reduce'):
            trade_value = (shares - prev_shares) * (value / shares)
        else:
            trade_value = 0
        weight = value / total * 100
        prev_weight = prev_value / prev_total * 100 if prev_filing else None
        positions.append({
            'key': key, 'kind': kind, 'ticker': ref['ticker'], 'cusip': ref['cusip'], 'name': ref['name'], 'cls': ref['cls'],
            'value': value, 'shares': shares, 'weight': weight,
            'prevValue': prev_value, 'prevShares': prev_shares, 'prevWeight': prev_weight,
            'weightDelta': weight - prev_weight if prev_weight is not None else None,
            'changePct': change, 'action': action, 'tradeValue': trade_value, 'split': split if split != 1 else None,
        })
    positions.sort(key=lambda p: (-p['value'], -p['prevValue']))
    return {'filing': filing, 'prevFiling': prev_filing, 'period': period, 'prevPeriod': prev, 'positions': positions}


def copy_return(cik, period):
    """Return of holding last quarter's long book unchanged through this quarter.

    Uses implied quarter-end prices (value / shares) pooled across all stored funds, so
    a position counts only when some tracked fund still reports it. Not the fund's
    actual performance: intra-quarter trades, shorts, options and cash are invisible.
    """
    prev = previous_period(period)
    prices, splits = market(prev, period)
    rows = db.rows("SELECT key, value FROM holdings WHERE cik=? AND period=? AND kind=''", (cik, prev))
    covered = gain = 0.0
    for r in rows:
        p0, p1 = prices.get(r['key'], (None, None))
        if not p0 or not p1:
            continue
        ratio = p1 / p0 * splits.get(r['key'], 1)
        if not 0.2 <= ratio <= 5:      # unexplained jump: corporate action we cannot see
            continue
        covered += r['value']
        gain += r['value'] * (ratio - 1)
    total = sum(r['value'] for r in rows)
    if not covered or covered < total * 0.5:
        return None
    return {'returnPct': gain / covered * 100, 'coveragePct': covered / total * 100}


def fund_summary(fund, period):
    """Card data for the fund list."""
    cik = fund['cik']
    filing = db.one('SELECT * FROM filings WHERE cik=? AND period=?', (cik, period)) if period else None
    out = {**fund, 'period': period, 'hasData': bool(filing),
           'periods': [r['period'] for r in db.rows('SELECT period FROM filings WHERE cik=? ORDER BY period DESC', (cik,))]}
    if not filing:
        return out
    diff = fund_diff(cik, period, 1.0)
    longs = [p for p in diff['positions'] if p['kind'] == '' and p['value'] > 0]
    counts = {a: sum(1 for p in diff['positions'] if p['kind'] == '' and p['action'] == a)
              for a in ('new', 'add', 'reduce', 'exit')}
    turnover = None
    if diff['prevFiling']:
        turnover = sum(abs(p['weightDelta'] or 0) for p in diff['positions']) / 2
    out.update({
        'totalValue': filing['total_value'], 'positions': filing['positions'], 'filedAt': filing['filed_at'],
        'source': filing['source'], 'sourceUrl': filing['source_url'],
        'prevTotalValue': diff['prevFiling']['total_value'] if diff['prevFiling'] else None,
        'top': [{'label': p['ticker'] or p['name'], 'weight': p['weight']} for p in longs[:5]],
        'top10Weight': sum(p['weight'] for p in longs[:10]),
        'counts': counts, 'turnover': turnover, 'copyReturn': copy_return(cik, period),
    })
    return out


def consensus(ciks, period, min_change=5.0):
    """Tickers the selected funds bought or sold together during `period`."""
    funds = {f['cik']: f for f in db.rows(f'SELECT cik,name,manager FROM funds WHERE cik IN ({",".join("?" * len(ciks))})', ciks)} if ciks else {}
    table, included, missing = {}, [], []
    for cik in ciks:
        diff = fund_diff(cik, period, min_change) if cik in funds else None
        if not diff or not diff['prevFiling']:
            missing.append({'cik': cik, 'name': funds.get(cik, {}).get('name', cik),
                            'reason': '이 분기 공시 없음' if not diff else '직전 분기 공시 없음'})
            continue
        included.append({'cik': cik, 'name': funds[cik]['name']})
        for p in diff['positions']:
            if p['kind'] != '':
                continue
            row = table.setdefault(p['key'], {'key': p['key'], 'ticker': p['ticker'], 'name': p['name'], 'cusip': p['cusip'],
                                              'funds': []})
            row['ticker'] = row['ticker'] or p['ticker']
            row['funds'].append({'cik': cik, 'fund': funds[cik]['name'], 'action': p['action'], 'changePct': p['changePct'],
                                 'weight': p['weight'], 'prevWeight': p['prevWeight'], 'weightDelta': p['weightDelta'],
                                 'value': p['value'], 'tradeValue': p['tradeValue'], 'shares': p['shares'],
                                 'prevShares': p['prevShares']})
    out = []
    for row in table.values():
        f = row['funds']
        buyers = [x for x in f if x['action'] in ('new', 'add')]
        sellers = [x for x in f if x['action'] in ('exit', 'reduce')]
        if not buyers and not sellers:
            continue
        row.update({
            'buyCount': len(buyers), 'sellCount': len(sellers),
            'newCount': sum(1 for x in f if x['action'] == 'new'), 'exitCount': sum(1 for x in f if x['action'] == 'exit'),
            'holdCount': sum(1 for x in f if x['action'] == 'hold'),
            'ownerCount': sum(1 for x in f if x['value'] > 0),
            'netCount': len(buyers) - len(sellers),
            'buyWeightDelta': sum(x['weightDelta'] or 0 for x in buyers),
            'sellWeightDelta': sum(x['weightDelta'] or 0 for x in sellers),
            'buyValue': sum(x['tradeValue'] for x in buyers), 'sellValue': -sum(x['tradeValue'] for x in sellers),
            'totalValue': sum(x['value'] for x in f),
        })
        row['funds'].sort(key=lambda x: -abs(x['weightDelta'] or 0))
        out.append(row)
    out.sort(key=lambda r: (-r['netCount'], -r['buyCount'], -r['buyWeightDelta']))
    return {'period': period, 'periodLabel': period_label(period), 'prevPeriod': previous_period(period),
            'funds': included, 'missing': missing, 'minChange': min_change, 'rows': out}
