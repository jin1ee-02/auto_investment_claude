"""Quarter-over-quarter portfolio changes and cross-fund consensus.

13F tables are quarter-end snapshots, so "bought/sold this quarter" is the difference
between two snapshots. Three things keep that difference honest:

* stock splits  - a 2:1 split looks like every holder doubling. Detected across all
                  stored funds and the previous share count is adjusted.
* tiny changes  - index-like rebalancing below `min_change` percent counts as HOLD.
* options       - PUT/CALL rows are shown per fund but never count as buying or selling.
"""
import functools
import math
import statistics
import threading

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


def versioned(maxsize):
    """lru_cache that starts over whenever `invalidate()` is called.

    Results are shared between callers, so callers must not mutate what they get back.
    """
    def wrap(fn):
        cache = functools.lru_cache(maxsize)(lambda _v, *args: fn(*args))

        @functools.wraps(fn)
        def cached(*args):
            return cache(_version, *args)
        cached.uncached = fn
        return cached
    return wrap


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


def change_action(prev_shares, shares, min_change):
    """add / reduce / hold for a position held in both quarters, and the share change in percent.

    Changes smaller than `min_change` percent (index-like rebalancing) count as hold.
    """
    change = (shares / prev_shares - 1) * 100 if prev_shares else 0.0
    threshold = max(min_change, 0.01)
    return ('add' if change >= threshold else 'reduce' if change <= -threshold else 'hold'), change


def split_factor(ratios, price_ratio, min_hits=2):
    """The stock split behind a quarter's share changes, or None.

    ratios: each continuing holder's shares now / shares a quarter ago; price_ratio: the implied
    price now / a quarter ago. A split leaves value roughly where it was, so dividing the price
    move by the factor must leave an ordinary quarterly move, and a smaller one than without it.
    """
    if len(ratios) < min_hits:
        return None

    def explains_price(f):
        return 0.6 <= price_ratio * f <= 1.6 and abs(math.log(price_ratio * f)) < abs(math.log(price_ratio))

    # Most continuing holders show exactly the factor. Two thirds of holders on it puts the
    # median on it too, so only factors next to the median need the full count.
    mid = statistics.median(ratios)
    for f in SPLIT_FACTORS:
        if abs(mid / f - 1) >= 0.01:
            continue
        hits = sum(1 for ratio in ratios if abs(ratio / f - 1) < 0.01)
        if hits >= min_hits and hits >= len(ratios) * 2 / 3 and explains_price(f):
            return f
    # Active funds trade around a split, so often only some holders show the factor, and only
    # roughly (AAPL 4:1 in 2020: 3.85 and 4.16 next to 0.79 and 2.22). For a forward split of 3 or
    # more the price must also have fallen by more than half, which trading noise does not do, so a
    # looser match is safe there: a third of holders within 5% of the factor, and more of them
    # than holders who kept their shares. Halving or doubling is left to the exact rule above:
    # among small caps with a few holders, trims of half during a rally look just like a 1:2.
    unchanged = sum(1 for ratio in ratios if abs(ratio - 1) < 0.05)
    best, best_hits = None, 0
    for f in SPLIT_FACTORS:
        if f < 3 or not explains_price(f):
            continue
        hits = sum(1 for ratio in ratios if abs(ratio / f - 1) < 0.05)
        if hits >= min_hits and hits * 3 >= len(ratios) and hits > unchanged and hits > best_hits:
            best, best_hits = f, hits
    return best


@versioned(maxsize=1)
def known_splits():
    """Splits confirmed from market data: {key: (checked up to quarter, {quarter: factor})}."""
    out = {r['key']: (r['upto'], {}) for r in db.rows('SELECT key, upto FROM split_checks')}
    for r in db.rows('SELECT key, period, factor FROM splits'):
        out.setdefault(r['key'], ('', {}))[1][r['period']] = r['factor']
    return out


def quarter_split(key, period, ratios, price_ratio):
    """Split factor of `key` in the quarter ending `period`, or None.

    Market data decides wherever the key was looked up; the 13F rule (`split_factor`) covers
    the rest: CUSIP-only securities, tickers Yahoo does not know, and quarters after the lookup.
    """
    upto, events = known_splits().get(key, ('', {}))
    if period <= upto:
        return events.get(period)
    return split_factor(ratios, price_ratio)


@versioned(maxsize=64)
def market(prev, cur):
    """Implied quarter-end prices and split factors, pooled over every stored fund."""
    data = {}
    # Every holding of two quarters: read as plain tuples, which is several times faster than dict rows.
    for period, key, cik, value, shares in db.run(
            "SELECT period, key, cik, value, shares FROM holdings WHERE kind='' AND shares>0 AND value>0 "
            'AND period IN (?,?)', (prev, cur)):
        data.setdefault(key, {}).setdefault(period, {})[cik] = (value, shares)
    prices, splits = {}, {}
    for key, by_period in data.items():
        p = {period: statistics.median(v / s for v, s in holders.values()) for period, holders in by_period.items()}
        prices[key] = (p.get(prev), p.get(cur))
        if prev not in by_period or cur not in by_period:
            continue
        both = [by_period[cur][c][1] / by_period[prev][c][1] for c in by_period[cur] if c in by_period[prev]]
        f = quarter_split(key, cur, both, p[cur] / p[prev])
        if f:
            splits[key] = f
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
            action, change = change_action(prev_shares, shares, min_change)
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


BENCHMARKS = ('SPY', 'IVV', 'VOO')      # S&P 500 ETFs; the first one any tracked fund reports wins


def price_step(key, period):
    """Split-adjusted price ratio of `key` over the quarter ending `period`, or None."""
    prices, splits = market(previous_period(period), period)
    p0, p1 = prices.get(key, (None, None))
    if not p0 or not p1:
        return None
    ratio = p1 / p0 * splits.get(key, 1)
    return ratio if 0.2 <= ratio <= 5 else None


def benchmark_return(period):
    """S&P 500 price return over the quarter, from implied ETF prices in the stored 13F tables."""
    for key in BENCHMARKS:
        ratio = price_step(key, period)
        if ratio:
            return {'returnPct': (ratio - 1) * 100, 'ticker': key}
    return None


def annualise(cum_pct, quarters):
    """Chained return per year. Fewer than four quarters would turn one lucky quarter into a silly number."""
    return ((1 + cum_pct / 100) ** (4 / quarters) - 1) * 100 if quarters >= 4 else None


@versioned(maxsize=128)
def fund_history(cik):
    """Per-quarter AUM, position count and estimated return of one fund, oldest first.

    Cumulative return chains only quarters whose estimate exists; `chained` says how many,
    overall and up to each quarter, so a view of an earlier quarter can read its own chain.
    The AUM change is split into a price effect (last quarter's book times this quarter's
    estimated return) and the rest, which is net buying/selling plus whatever the estimate misses.
    """
    filings = db.rows('SELECT period, filed_at, total_value, positions FROM filings WHERE cik=? ORDER BY period', (cik,))
    have = {f['period'] for f in filings}
    out, cum, bench_cum, n = [], 1.0, 1.0, 0
    prev_total = first_linked = None
    for f in filings:
        period = f['period']
        prev = previous_period(period)
        ret = copy_return(cik, period) if prev in have else None
        bench = benchmark_return(period) if ret else None
        rows = db.rows("SELECT value FROM holdings WHERE cik=? AND period=? AND kind='' ORDER BY value DESC", (cik, period))
        total = f['total_value'] or 0
        q = {'period': period, 'label': period_label(period), 'filedAt': f['filed_at'], 'totalValue': total,
             'positions': f['positions'], 'returnPct': ret['returnPct'] if ret else None,
             'coveragePct': ret['coveragePct'] if ret else None, 'benchPct': bench['returnPct'] if bench else None,
             'top10Weight': sum(r['value'] for r in rows[:10]) / total * 100 if total else None,
             'top20Weight': sum(r['value'] for r in rows[:20]) / total * 100 if total else None,
             'prevTotalValue': prev_total if prev in have else None, 'priceEffect': None, 'netFlow': None}
        if q['prevTotalValue'] is not None and ret:
            q['priceEffect'] = q['prevTotalValue'] * ret['returnPct'] / 100
            q['netFlow'] = total - q['prevTotalValue'] - q['priceEffect']
        if ret and bench:
            n += 1
            cum *= 1 + ret['returnPct'] / 100
            bench_cum *= 1 + bench['returnPct'] / 100
            first_linked = first_linked or period
        q['chained'] = n
        q['cumPct'] = (cum - 1) * 100 if n else None
        q['benchCumPct'] = (bench_cum - 1) * 100 if n else None
        q['annualPct'] = annualise(q['cumPct'], n) if n else None
        q['benchAnnualPct'] = annualise(q['benchCumPct'], n) if n else None
        out.append(q)
        prev_total = total
    last = out[-1] if out else {}
    return {'quarters': out, 'chained': n, 'firstLinked': first_linked,
            **{k: last.get(k) for k in ('cumPct', 'benchCumPct', 'annualPct', 'benchAnnualPct')}}


def price_path(key, upto, quarters=None):
    """Implied quarter-end price of `key` for each stored period up to `upto`, on `upto`'s share basis.

    Earlier prices are divided by every split detected after them, so a 2:1 split does not
    look like a halving. Returns ({period: price or None}, {period: split factor to upto}).
    """
    quarters = quarters or [q for q in reversed(periods()) if q <= upto]
    raw, split = {}, {}
    for q in quarters:
        prices, splits = market(previous_period(q), q)
        raw[q] = prices.get(key, (None, None))[1]
        split[q] = splits.get(key, 1)
    adj, factor = {}, 1.0
    for q in reversed(quarters):
        adj[q] = factor
        factor *= split[q]
    return {q: raw[q] / adj[q] if raw[q] else None for q in quarters}, adj


def cost_basis(steps):
    """Average cost per share of the open position, buying at each quarter-end price.

    steps: [(shares, price)] oldest first, shares and prices on one split basis. Adds are bought
    at that quarter's price, trims keep the average, and a full exit starts over. None when an
    add has no price.
    """
    cost = held = 0.0
    unknown = False
    for shares, price in steps:
        if shares > held:
            if price is None:
                unknown = True
            else:
                cost += (shares - held) * price
        elif shares < held:
            cost *= shares / held
        if shares <= 0:
            cost, unknown = 0.0, False
        held = shares
    return None if unknown or held <= 0 else cost / held


@versioned(maxsize=64)
def long_positions(cik, period):
    """Holding period and estimated cost of each share position `cik` holds at `period`, over its whole 13F record.

    Reads `history`, which goes back to the fund's first 13F on 13f.info, so a position bought
    years before the recent quarters gets its real entry price rather than the first recent one.
    Prices are quarter-end prices implied by every tracked fund's value / shares, and splits come
    from `quarter_split` as in `market`: market data where it was looked up, else the 13F rule.

    Per key: heldQuarters, firstPeriod, heldSinceStart, sincePct, breakeven, basisFrom.
    basisFrom is set when the position was already open in the fund's first quarter on record:
    the cost then counts everything held at that point as bought at that quarter's price.
    """
    filed = [q for (q,) in db.run('SELECT period FROM history_filings WHERE cik=? AND period<=? ORDER BY period',
                                  (cik, period))]
    if not filed:
        return {}
    data = {}
    for key, q, holder, value, shares in db.run(
            'SELECT h.key, h.period, h.cik, h.value, h.shares FROM holdings c JOIN history h ON h.key = c.key '
            "WHERE c.cik=? AND c.period=? AND c.kind='' AND c.shares>0 AND h.period<=? AND h.value>0 AND h.shares>0",
            (cik, period, period)):
        data.setdefault(key, {}).setdefault(q, {})[holder] = (value, shares)
    return {key: _long_position(key, cik, period, filed, by_period) for key, by_period in data.items()
            if cik in by_period.get(period, {})}


def _long_position(key, cik, period, filed, by_period):
    quarters = sorted(by_period)
    raw = {q: statistics.median(v / s for v, s in holders.values()) for q, holders in by_period.items()}
    # Carry every quarter onto `period`'s share basis: divide by each split that came after it.
    adj, factor = {}, 1.0
    for q in reversed(quarters):
        adj[q] = factor
        prev = previous_period(q)
        if prev in by_period:
            both = [by_period[q][c][1] / by_period[prev][c][1] for c in by_period[q] if c in by_period[prev]]
            factor *= quarter_split(key, q, both, raw[q] / raw[prev]) or 1
    price = {q: raw[q] / adj[q] for q in quarters}
    mine = {q: holders[cik][1] * adj[q] for q, holders in by_period.items() if cik in holders}
    streak, q = [], period
    while mine.get(q):
        streak.append(q)
        q = previous_period(q)
    first = streak[-1]
    since_start = first == filed[0]
    return {'heldQuarters': len(streak), 'firstPeriod': first, 'heldSinceStart': since_start,
            'sincePct': (price[period] / price[first] - 1) * 100 if len(streak) > 1 else None,
            'breakeven': cost_basis([(mine.get(q, 0), price.get(q)) for q in filed]),
            'basisFrom': first if since_start else None}


@versioned(maxsize=128)
def position_history(cik, period):
    """For each share position held at `period`: how long it has been held and how its price moved.

    heldQuarters counts consecutive quarters back from `period`; `heldSinceStart` means the
    streak reaches the oldest filing on record, so the real holding period may be longer.
    sincePct is the split-adjusted price change since the first quarter of that streak.
    breakeven is the estimated average cost (quarter-end prices) and plPct the gain on it at `period`.
    Holding period, sincePct and breakeven come from the full 13F record (`long_positions`);
    the recent quarters in `holdings` are only the fallback.
    """
    fund_periods = [r['period'] for r in db.rows('SELECT period FROM filings WHERE cik=? AND period<=? ORDER BY period',
                                                 (cik, period))]
    quarters = [q for q in reversed(periods()) if q <= period]
    totals = {r['period']: r['total_value'] or 1 for r in db.rows('SELECT period, total_value FROM filings WHERE cik=?', (cik,))}
    long = long_positions(cik, period)
    weights, shares = {}, {}
    for r in db.rows("SELECT period, key, value, shares FROM holdings WHERE cik=? AND kind='' AND period<=?", (cik, period)):
        weights.setdefault(r['key'], {})[r['period']] = r['value'] / totals[r['period']] * 100
        shares.setdefault(r['key'], {})[r['period']] = r['shares']
    out = {}
    for key, w in weights.items():
        if period not in w:
            continue
        streak, p = [], period
        while p in w:
            streak.append(p)
            p = previous_period(p)
        first = streak[-1]
        ratio = 1.0
        for q in streak[:-1]:          # price moves of every quarter after the first one held
            step = price_step(key, q)
            if step is None:
                ratio = None
                break
            ratio *= step
        quarter = price_step(key, period)
        price, adj = price_path(key, period, quarters)
        cost = cost_basis([(shares[key].get(q, 0) * adj.get(q, 1), price.get(q)) for q in fund_periods])
        now = price.get(period)
        out[key] = {'heldQuarters': len(streak), 'firstPeriod': first, 'heldSinceStart': first == fund_periods[0],
                    'sincePct': (ratio - 1) * 100 if ratio is not None and len(streak) > 1 else None,
                    'quarterPricePct': (quarter - 1) * 100 if quarter else None,
                    'price': now, 'breakeven': cost, 'basisFrom': fund_periods[0] if first == fund_periods[0] else None,
                    'weights': [round(w.get(q, 0), 3) for q in fund_periods]}
        # The full 13F record, when there is one, replaces the recent-quarters estimate.
        out[key].update(long.get(key) or {})
        cost = out[key]['breakeven']
        out[key]['plPct'] = (now / cost - 1) * 100 if cost and now else None
        if out[key]['basisFrom']:
            out[key]['basisFromLabel'] = period_label(out[key]['basisFrom'])
    return {'periods': fund_periods, 'positions': out}


def position_track(cik, key, period):
    """One position over every stored quarter: price, the fund's shares, and what it did each quarter."""
    quarters = [q for q in reversed(periods()) if q <= period]
    price, adj = price_path(key, period, quarters)
    filed = {r['period'] for r in db.rows('SELECT period FROM filings WHERE cik=?', (cik,))}
    held = {r['period']: r['shares'] for r in db.rows("SELECT period, shares FROM holdings WHERE cik=? AND key=? AND kind=''",
                                                      (cik, key))}
    rows, steps, prev = [], [], None
    for q in quarters:
        sh = held.get(q, 0) * adj[q] if q in filed else None
        action = None
        if sh is not None and prev is not None:
            if prev == 0 and sh > 0:
                action = 'new'
            elif prev > 0 and sh == 0:
                action = 'exit'
            elif prev > 0:
                action = change_action(prev, sh, 1.0)[0]     # the same 1% the fund detail table uses
        elif sh:
            action = 'hold'
        if sh is not None:
            steps.append((sh, price[q]))
            prev = sh
        rows.append({'period': q, 'label': period_label(q), 'price': price[q], 'shares': sh,
                     'value': sh * price[q] if sh and price[q] else None, 'action': action, 'filed': q in filed})
    long = long_positions(cik, period).get(key)
    if long:
        return {'key': key, 'rows': rows, 'breakeven': long['breakeven'],
                'basisFromLabel': period_label(long['basisFrom']) if long['basisFrom'] else None}
    return {'key': key, 'rows': rows, 'breakeven': cost_basis(steps), 'basisFromLabel': None}


def track_record(cik, period):
    """Chained estimated return of `cik` up to `period`, for the fund list."""
    history = fund_history(cik)
    upto = [q for q in history['quarters'] if q['period'] <= period and q['chained']]
    if not upto:
        return None
    last = upto[-1]
    return {'quarters': last['chained'], 'cumPct': last['cumPct'], 'benchCumPct': last['benchCumPct'],
            'annualPct': last['annualPct'], 'from': period_label(previous_period(history['firstLinked']))}


@versioned(maxsize=512)
def fund_summary(cik, period):
    """Card data for the fund list; None for an unknown fund."""
    fund = db.one('SELECT * FROM funds WHERE cik=?', (cik,))
    if not fund:
        return None
    filing = db.one('SELECT * FROM filings WHERE cik=? AND period=?', (cik, period)) if period else None
    filed = db.rows('SELECT period, filed_at FROM filings WHERE cik=? ORDER BY period DESC', (cik,))
    out = {**fund, 'period': period, 'hasData': bool(filing), 'periods': [r['period'] for r in filed],
           'latestPeriod': filed[0]['period'] if filed else None,
           'latestLabel': period_label(filed[0]['period']) if filed else None,
           'latestFiledAt': filed[0]['filed_at'] if filed else None}
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
        'top20Weight': sum(p['weight'] for p in longs[:20]),
        'track': track_record(cik, period),
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
