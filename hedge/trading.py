"""Turn research ratings into an order plan, and execute a plan the user confirmed.

Rules (deliberately simple and visible in the UI):

  Buy          buy up to the per-stock amount
  Overweight   buy up to a fraction of that amount
  Hold         nothing
  Underweight  held -> sell a fraction of the position
  Sell         held -> sell the whole position

Only whole-share LIMIT DAY orders. Nothing is ever sent without an explicit request
from the UI; live orders additionally need TOSS_ENABLE_LIVE=true and a typed phrase.
"""
import math
import threading
import time
import uuid

from . import config, db, research, toss

LIVE_CONFIRM_PHRASE = '실주문 전송'
DEFAULTS = {'buyAmountUsd': 500.0, 'overweightFactor': 0.5, 'underweightSellFraction': 0.5,
            'slippagePct': 0.5, 'paperInitialCash': 10000.0, 'freshDays': research.FRESH_DAYS}
LIMITS = {'buyAmountUsd': (1, 1_000_000), 'overweightFactor': (0, 1), 'underweightSellFraction': (0, 1),
          'slippagePct': (0, 5), 'paperInitialCash': (100, 100_000_000), 'freshDays': (1, 120)}

_exec_lock = threading.Lock()
_toss_retry_at = 0.0


class TradeError(ValueError):
    pass


def settings():
    return {**DEFAULTS, **(db.kv_get('trade_settings') or {})}


def update_settings(patch):
    current = settings()
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        low, high = LIMITS[key]
        value = float(value)
        if not (low <= value <= high) or math.isnan(value):
            raise TradeError(f'{key} 값은 {low}~{high} 사이여야 합니다')
        current[key] = value
    db.kv_set('trade_settings', current)
    return current


# ───────────────────────────── quotes ─────────────────────────────

def quotes(symbols, allow_fallback=True):
    """Last prices in USD. Toss first; public market data as a fallback for paper trading."""
    symbols = sorted(set(symbols))
    if not symbols:
        return {}, None
    global _toss_retry_at
    if config.toss_configured() and (not allow_fallback or time.monotonic() >= _toss_retry_at):
        try:
            return toss.client().prices(symbols), 'toss'
        except toss.BrokerError:
            if not allow_fallback:
                raise
            _toss_retry_at = time.monotonic() + 300      # e.g. IP not allow-listed: don't knock on every page view
    if not allow_fallback:
        raise toss.BrokerError('토스 연결이 설정되지 않았습니다')
    import yfinance as yf
    out = {}
    for symbol in symbols:
        try:
            price = yf.Ticker(research.research_symbol(symbol)).fast_info['last_price']
            if price and price > 0:
                out[symbol] = float(price)
        except Exception:
            continue
    return out, 'yfinance'


# ───────────────────────────── accounts ─────────────────────────────

def paper_cash():
    cash = db.kv_get('paper_cash')
    if cash is None:
        cash = settings()['paperInitialCash']
        db.kv_set('paper_cash', cash)
    return float(cash)


def reset_paper():
    with db.tx() as c:
        c.execute('DELETE FROM paper_positions')
        c.execute("DELETE FROM orders WHERE mode='paper'")
    db.kv_set('paper_cash', settings()['paperInitialCash'])


def account(mode):
    """{'cash', 'positions': [...], 'equity', 'priceSource', 'warning'} in USD."""
    warning = None
    if mode == 'live':
        raw = toss.client().us_account()
        positions, cash, source = raw['positions'], raw['cash'], 'toss'
    elif mode == 'paper':
        rows = db.rows('SELECT * FROM paper_positions WHERE quantity>0 ORDER BY symbol')
        prices, source = quotes([r['symbol'] for r in rows])
        positions = [{'symbol': r['symbol'], 'name': '', 'quantity': float(r['quantity']), 'avgPrice': r['avg_price'],
                      'lastPrice': prices.get(r['symbol'])} for r in rows]
        if any(p['lastPrice'] is None for p in positions):
            warning = '일부 종목의 현재가를 가져오지 못했습니다'
        cash = paper_cash()
    else:
        raise TradeError('mode 는 paper 또는 live 여야 합니다')
    for p in positions:
        p['value'] = p['quantity'] * p['lastPrice'] if p['lastPrice'] is not None else None
        p['pnlPct'] = (p['lastPrice'] / p['avgPrice'] - 1) * 100 if p['lastPrice'] and p['avgPrice'] else None
    priced = all(p['value'] is not None for p in positions)
    equity = cash + sum(p['value'] for p in positions) if priced else None
    for p in positions:
        p['weight'] = p['value'] / equity * 100 if equity and p['value'] is not None else None
    return {'mode': mode, 'cash': cash, 'positions': positions, 'equity': equity, 'priceSource': source, 'warning': warning}


# ───────────────────────────── plan ─────────────────────────────

def build_plan(mode):
    cfg = settings()
    acct = account(mode)
    latest = research.latest_by_ticker(int(cfg['freshDays']))
    held = {p['symbol']: p for p in acct['positions']}
    need = [t for t, r in latest.items() if r['rating'] in ('Buy', 'Overweight') or t in held]
    prices, source = quotes(need, allow_fallback=(mode == 'paper'))
    slip = cfg['slippagePct'] / 100
    sells, buys, skipped = [], [], []

    def skip(ticker, r, reason):
        skipped.append({'symbol': ticker, 'rating': r['rating'], 'researchId': r['id'], 'reason': reason})

    for ticker, r in sorted(latest.items()):
        rating, price, pos = r['rating'], prices.get(ticker), held.get(ticker)
        base = {'symbol': ticker, 'rating': rating, 'researchId': r['id'], 'lastPrice': price}
        if rating == 'Hold':
            skip(ticker, r, '보유 유지 (Hold)' if pos else '관망 (Hold)')
        elif rating in ('Sell', 'Underweight'):
            if not pos:
                skip(ticker, r, '보유하지 않은 종목')
                continue
            if not price:
                skip(ticker, r, '현재가를 가져오지 못함')
                continue
            # A partial sale must not repeat every time the plan is opened: one research, one trim.
            if rating == 'Underweight' and db.one(
                    "SELECT 1 FROM orders WHERE mode=? AND research_id=? AND side='SELL' AND status IN ('filled','submitted')",
                    (mode, r['id'])):
                skip(ticker, r, '이 리서치로 이미 비중을 줄였습니다')
                continue
            whole = int(pos['quantity'])
            quantity = whole if rating == 'Sell' else max(1, int(whole * cfg['underweightSellFraction'])) if whole else 0
            if quantity < 1:
                skip(ticker, r, '1주 미만 보유 (소수점 주식은 지정가 매도 불가)')
                continue
            limit = round(price * (1 - slip), 2)
            sells.append({**base, 'side': 'SELL', 'quantity': quantity, 'maxQuantity': whole, 'price': limit,
                          'amount': quantity * limit,
                          'reason': '전량 매도' if rating == 'Sell' else f"보유분의 {cfg['underweightSellFraction']:.0%} 매도"})
        else:
            if not price:
                skip(ticker, r, '현재가를 가져오지 못함')
                continue
            target = cfg['buyAmountUsd'] * (1 if rating == 'Buy' else cfg['overweightFactor'])
            owned = pos['quantity'] * price if pos else 0
            limit = round(price * (1 + slip), 2)
            quantity = int((target - owned) / limit)
            if quantity < 1:
                skip(ticker, r, '이미 목표 금액만큼 보유' if owned >= target - limit and owned > 0
                     else f'1주 가격(${limit:,.2f})이 남은 목표 금액보다 큼')
                continue
            buys.append({**base, 'side': 'BUY', 'quantity': quantity, 'price': limit, 'amount': quantity * limit,
                         'reason': f'목표 ${target:,.0f}' + (f' (보유 ${owned:,.0f})' if owned else '')})
    # Cash is only what is available now: sale proceeds are not counted until they settle.
    cash = acct['cash']
    buys.sort(key=lambda o: (o['rating'] != 'Buy', -o['researchId']))
    funded = []
    for order in buys:
        affordable = int(cash / order['price'])
        if affordable < 1:
            skipped.append({'symbol': order['symbol'], 'rating': order['rating'], 'researchId': order['researchId'],
                            'reason': '매수 가능 현금 부족'})
            continue
        if affordable < order['quantity']:
            order.update(quantity=affordable, amount=affordable * order['price'], reason=order['reason'] + ' · 현금에 맞춰 축소')
        order['maxQuantity'] = order['quantity']
        cash -= order['amount']
        funded.append(order)
    unresearched = sorted(s for s in held if s not in latest)
    return {'mode': mode, 'account': acct, 'orders': sells + funded, 'skipped': skipped, 'unresearched': unresearched,
            'cashAfterBuys': cash, 'settings': cfg, 'priceSource': source, 'liveEnabled': config.live_enabled(),
            'confirmPhrase': LIVE_CONFIRM_PHRASE}


# ───────────────────────────── execution ─────────────────────────────

def execute(mode, requested, confirm=''):
    """Execute user-approved orders. `requested` = [{'symbol','side','quantity'}].

    Prices, limits and eligibility are recomputed here; the browser only chooses which
    planned orders to send and may lower a quantity.
    """
    if mode == 'live':
        if not config.live_enabled():
            raise TradeError('실주문이 꺼져 있습니다. .env 에서 TOSS_ENABLE_LIVE=true 로 바꾸고 서버를 다시 시작하세요.')
        if confirm != LIVE_CONFIRM_PHRASE:
            raise TradeError(f'확인 문구가 일치하지 않습니다: "{LIVE_CONFIRM_PHRASE}"')
    if not requested:
        raise TradeError('선택한 주문이 없습니다')
    with _exec_lock:
        plan = build_plan(mode)
        planned = {(o['symbol'], o['side']): o for o in plan['orders']}
        approved = []
        for item in requested:
            order = planned.get((item.get('symbol'), item.get('side')))
            if not order:
                raise TradeError(f"{item.get('symbol')} {item.get('side')}: 현재 매매안에 없는 주문입니다. 매매안을 새로고침하세요.")
            quantity = item.get('quantity')
            if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= order['maxQuantity']:
                raise TradeError(f"{order['symbol']}: 수량은 1~{order['maxQuantity']} 사이의 정수여야 합니다")
            approved.append({**order, 'quantity': quantity})
        approved.sort(key=lambda o: o['side'] != 'SELL')     # sells first
        results = []
        stop_reason = None
        for order in approved:
            if stop_reason:
                results.append(_record(mode, order, 'skipped', note=f'앞선 주문 오류로 전송하지 않음: {stop_reason}'))
                continue
            if mode == 'paper':
                results.append(_fill_paper(order))
                continue
            recent = db.one("SELECT id FROM orders WHERE mode='live' AND symbol=? AND side=? AND status='submitted' "
                            "AND created_at >= datetime('now','-10 minutes')", (order['symbol'], order['side']))
            if recent:
                results.append(_record(mode, order, 'skipped', note='10분 이내에 같은 주문을 이미 전송했습니다'))
                continue
            client_id = uuid.uuid4().hex
            try:
                broker = toss.client()
                if order['side'] == 'SELL':
                    order['quantity'] = min(order['quantity'], int(broker.sellable(order['symbol'])))
                    if order['quantity'] < 1:
                        results.append(_record(mode, order, 'skipped', note='매도 가능 수량 없음'))
                        continue
                broker_id = broker.submit_limit_order(order['symbol'], order['side'], order['quantity'], order['price'], client_id)
                results.append(_record(mode, order, 'submitted', client_id, broker_id, '지정가 DAY 주문 접수'))
            except toss.BrokerError as exc:
                stop_reason = str(exc)
                results.append(_record(mode, order, 'failed', client_id, note=stop_reason))
        return results


def _fill_paper(order):
    symbol, quantity, price = order['symbol'], order['quantity'], order['price']
    with db.tx() as c:
        cash = paper_cash()
        row = c.execute('SELECT quantity, avg_price FROM paper_positions WHERE symbol=?', (symbol,)).fetchone()
        owned, avg = (row['quantity'], row['avg_price']) if row else (0, 0.0)
        if order['side'] == 'BUY':
            cost = quantity * price
            if cost > cash + 1e-9:
                return _record('paper', order, 'failed', note='모의 현금 부족', cursor=c)
            total = owned + quantity
            c.execute('INSERT INTO paper_positions(symbol,quantity,avg_price) VALUES(?,?,?) ON CONFLICT(symbol) '
                      'DO UPDATE SET quantity=excluded.quantity, avg_price=excluded.avg_price',
                      (symbol, total, (owned * avg + cost) / total))
            cash -= cost
        else:
            if quantity > owned:
                return _record('paper', order, 'failed', note='모의 보유 수량 부족', cursor=c)
            c.execute('UPDATE paper_positions SET quantity=? WHERE symbol=?', (owned - quantity, symbol))
            c.execute('DELETE FROM paper_positions WHERE quantity<=0')
            cash += quantity * price
        c.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  ('paper_cash', repr(float(cash))))
        return _record('paper', order, 'filled', note='모의 체결 (지정가 기준)', cursor=c)


def _record(mode, order, status, client_id=None, broker_id=None, note='', cursor=None):
    cur = (cursor or db.conn()).execute(
        'INSERT INTO orders(mode,symbol,side,quantity,price,status,client_order_id,broker_order_id,research_id,rating,note) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (mode, order['symbol'], order['side'], order['quantity'], order['price'], status, client_id, broker_id,
         order['researchId'], order['rating'], note))
    return {'id': cur.lastrowid, 'symbol': order['symbol'], 'side': order['side'], 'quantity': order['quantity'],
            'price': order['price'], 'status': status, 'note': note}


def history(limit=200):
    return db.rows('SELECT * FROM orders ORDER BY id DESC LIMIT ?', (limit,))
