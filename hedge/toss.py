"""Toss Securities Open API client (https://openapi.tossinvest.com).

No automatic retries and no redirects: an order request is sent at most once, and a
missing acknowledgement is reported instead of being resent. Credentials and raw
broker responses are never logged or returned to the browser.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

BASE = 'https://openapi.tossinvest.com'
KNOWN_ERRORS = ('access_denied', 'invalid_client', 'invalid_request', 'ip-not-allowed', 'account-not-found',
                'account-header-required', 'market-closed', 'insufficient-buying-power', 'insufficient-quantity',
                'invalid-symbol', 'invalid-price', 'confirm-high-value-required', 'rate-limit-exceeded')


class BrokerError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BrokerError('토스 API 가 예상치 못한 리다이렉트를 반환했습니다')


class Toss:
    def __init__(self):
        self.client_id = config.env('TOSS_CLIENT_ID')
        self.secret = config.env('TOSS_CLIENT_SECRET')
        self.account = config.env('TOSS_ACCOUNT_SEQ')
        if not self.client_id or not self.secret:
            raise BrokerError('TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 설정되지 않았습니다')
        if self.account and not self.account.isdigit():
            raise BrokerError('TOSS_ACCOUNT_SEQ 는 숫자여야 합니다')
        self.token = None
        self.expires = 0.0
        self.last = 0.0
        self.lock = threading.RLock()
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _http(self, method, path, payload=None, auth=True):
        with self.lock:
            if auth and time.monotonic() >= self.expires:
                token = self._http('POST', '/oauth2/token', {'grant_type': 'client_credentials',
                                                             'client_id': self.client_id,
                                                             'client_secret': self.secret}, auth=False)
                self.token = token['access_token']
                self.expires = time.monotonic() + max(0, int(token.get('expires_in', 0)) - 60)
            headers = {'Accept': 'application/json'}
            data = None
            if auth:
                headers['Authorization'] = 'Bearer ' + self.token
                if self.account:
                    headers['X-Tossinvest-Account'] = self.account
                if payload is not None:
                    headers['Content-Type'] = 'application/json'
                    data = json.dumps(payload).encode()
            elif payload is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
                data = urllib.parse.urlencode(payload).encode()
            time.sleep(max(0.0, 0.35 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=30) as response:
                    body = json.load(response)
            except urllib.error.HTTPError as exc:
                code = None
                try:
                    error = json.loads(exc.read(16384)).get('error')
                    code = error.get('code') if isinstance(error, dict) else error
                except Exception:
                    pass
                if exc.code == 403 and (not auth or code == 'ip-not-allowed'):
                    raise BrokerError('토스가 현재 접속 IP 를 허용하지 않았습니다. 토스증권 WTS > 설정 > Open API > '
                                      '허용 IP 에 이 컴퓨터의 공인 IP 를 등록하세요.') from None
                detail = f' ({code})' if code in KNOWN_ERRORS else ''
                raise BrokerError(f'토스 {"API" if auth else "인증"} 오류 HTTP {exc.code}{detail}') from None
            except (OSError, ValueError):
                raise BrokerError('토스 응답을 받지 못했습니다') from None
            if not auth:
                return body
            if not isinstance(body, dict) or 'result' not in body or body.get('error'):
                raise BrokerError('토스 응답 형식이 올바르지 않습니다')
            return body['result']

    def get(self, path, **query):
        suffix = '?' + urllib.parse.urlencode(query) if query else ''
        return self._http('GET', '/api/v1/' + path + suffix)

    def ensure_account(self):
        """Pick the single brokerage account when none is configured."""
        with self.lock:
            if self.account:
                return self.account
            accounts = self.get('accounts')
            eligible = [a for a in accounts if isinstance(a, dict) and a.get('accountType') == 'BROKERAGE']
            if len(eligible) != 1:
                raise BrokerError('종합매매 계좌가 여러 개입니다. .env 의 TOSS_ACCOUNT_SEQ 를 지정하세요.')
            self.account = str(int(eligible[0]['accountSeq']))
            return self.account

    def prices(self, symbols):
        out = {}
        symbols = sorted(set(symbols))
        for i in range(0, len(symbols), 200):
            for item in self.get('prices', symbols=','.join(symbols[i:i + 200])):
                if item.get('currency') == 'USD':
                    out[item['symbol']] = float(item['lastPrice'])
        return out

    def us_account(self):
        """USD cash and US positions. Korean holdings are ignored."""
        self.ensure_account()
        holdings = self.get('holdings')
        power = self.get('buying-power', currency='USD')
        positions = []
        for item in holdings.get('items', []):
            if item.get('marketCountry') != 'US' or item.get('currency') != 'USD':
                continue
            quantity = float(item['quantity'])
            if quantity <= 0:
                continue
            positions.append({'symbol': item['symbol'], 'name': item.get('name', ''), 'quantity': quantity,
                              'avgPrice': float(item['averagePurchasePrice']), 'lastPrice': float(item['lastPrice'])})
        return {'cash': float(power['cashBuyingPower']), 'positions': positions}

    def sellable(self, symbol):
        return float(self.get('sellable-quantity', symbol=symbol)['sellableQuantity'])

    def submit_limit_order(self, symbol, side, quantity, price, client_order_id):
        """Send one whole-share LIMIT DAY order. Requires TOSS_ENABLE_LIVE=true."""
        if not config.live_enabled():
            raise BrokerError('실주문이 꺼져 있습니다 (.env 의 TOSS_ENABLE_LIVE=true 필요)')
        if side not in ('BUY', 'SELL') or int(quantity) != quantity or quantity <= 0 or price <= 0:
            raise BrokerError('주문 값이 올바르지 않습니다')
        self.ensure_account()
        result = self._http('POST', '/api/v1/orders', {
            'clientOrderId': client_order_id, 'symbol': symbol, 'side': side, 'orderType': 'LIMIT',
            'timeInForce': 'DAY', 'quantity': str(int(quantity)), 'price': f'{price:.2f}'})
        if not isinstance(result, dict) or not result.get('orderId'):
            raise BrokerError('주문 접수 확인을 받지 못했습니다. 토스 앱에서 주문 내역을 확인하세요 (자동 재전송 안 함).')
        return result['orderId']


_instance = None
_instance_lock = threading.Lock()


def client():
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Toss()
        return _instance
