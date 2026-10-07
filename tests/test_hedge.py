"""Offline tests: parsing, quarter diffs, consensus, order planning. No network, no broker."""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ['HEDGE_DATA_DIR'] = tempfile.mkdtemp(prefix='hedge-test-')
os.environ['TOSS_CLIENT_ID'] = ''
os.environ['TOSS_CLIENT_SECRET'] = ''
os.environ['TOSS_ENABLE_LIVE'] = 'false'

from hedge import analysis, config, db, research, sources, trading  # noqa: E402

config._loaded = True       # never read the real .env in tests
FIX = Path(__file__).parent / 'fixtures'
Q1, Q2 = '2026-03-31', '2026-06-30'


def put(cik, period, rows, name=None):
    """rows: (ticker, shares, value[, kind])"""
    db.run('INSERT OR IGNORE INTO funds(cik,name) VALUES(?,?)', (cik, name or f'Fund {cik}'))
    data = [{'ticker': r[0], 'cusip': 'C' + r[0], 'name': r[0] + ' INC', 'cls': 'COM', 'kind': r[3] if len(r) > 3 else '',
             'value': float(r[2]), 'shares': float(r[1])} for r in rows]
    sources.store_filing(sources.Filing(cik, period, f'{cik}{period}', '13F-HR', period, 'test', ''), sources.aggregate(data))
    analysis.invalidate()


class Base(unittest.TestCase):
    def setUp(self):
        for table in ('funds', 'filings', 'holdings', 'securities', 'research', 'orders', 'paper_positions', 'kv'):
            db.run(f'DELETE FROM {table}')
        analysis.invalidate()


class SourcesTest(Base):
    def test_quarters(self):
        self.assertEqual(sources.quarter_end('2026-05-15'), Q2)
        self.assertEqual(sources.quarter_end('2025-12-31'), '2025-12-31')
        self.assertEqual(sources.previous_period(Q2), Q1)
        self.assertEqual(sources.previous_period(Q1), '2025-12-31')
        self.assertEqual(sources.previous_period('2025-12-31'), '2025-09-30')
        self.assertEqual(sources.previous_period('2026-09-30'), Q2)
        self.assertEqual(sources.period_label(Q2), '2026 Q2')

    def test_edgar_information_table(self):
        rows = sources.parse_information_table((FIX / 'infotable.xml').read_bytes())
        merged = sources.aggregate([dict(r) for r in rows])
        ally = next(r for r in merged if r['cusip'] == '02005N100')
        self.assertEqual(ally['name'], 'ALLY FINL INC')
        self.assertGreater(len(rows), len(merged))            # several managers report the same CUSIP
        self.assertGreaterEqual(ally['shares'], 12561737 + 2803875)
        self.assertEqual(sum(r['value'] for r in rows), sum(r['value'] for r in merged))

    def test_edgar_listing_and_index(self):
        data = json.loads((FIX / 'submissions.json').read_text(encoding='utf-8'))
        filings = sources.Edgar.filings_from_submissions('1067983', data)
        self.assertEqual(filings[0].period, Q2)
        self.assertEqual(filings[0].accession, '000119312526352200')
        index = json.loads((FIX / 'index.json').read_text(encoding='utf-8'))
        self.assertEqual(sources.Edgar.table_name(index), '56757.xml')

    def test_pick_filings_skips_partial_amendment(self):
        def f(period, acc, form, filed, count):
            return sources.Filing('1', period, acc, form, filed, 't', '', count)
        chosen = sources.pick_filings([
            f(Q2, 'a', '13F-HR', '2026-08-14', 100), f(Q2, 'b', '13F-HR/A', '2026-08-20', 3),      # new-holdings add-on
            f(Q1, 'c', '13F-HR', '2026-05-15', 90), f(Q1, 'd', '13F-HR/A', '2026-05-30', 92),      # full restatement
            f('2025-12-31', 'e', '13F-HR', '2026-02-14', 80)], 2)
        self.assertEqual([c.accession for c in chosen], ['a', 'd'])

    def test_13finfo_listing_labels_amendments(self):
        def row(period, acc, form, filed, count):
            return (f'<tr class="x"><td class="px-3 py-2 text-center" data-order="{period}">\n<a href="/13f/{acc}-fund-q">Q</a></td>'
                    f'<td class="px-3 py-2 text-right">{count}</td><td class="px-3 py-2 text-right">1,234</td>'
                    f'<td class="px-3 py-2 truncate group" title="AAPL, KO">AAPL, KO</td>'
                    f'<td class="px-3 py-2 text-center truncate" title="{form}">{form}</td>'
                    f'<td class="px-3 py-2 text-right" data-order="{filed}">x</td><td>{acc}</td></tr>')
        a, b, c, d = ('%018d' % n for n in (1, 2, 3, 4))
        html = ('<h1 class="t">Fund  LLC</h1><table id="managerFilings"><thead><tr><th>Quarter</th></tr></thead><tbody>'
                + row(Q2, a, '13F-HR', '2026-08-14', '1,000') + row(Q1, b, 'RESTATEMENT', '2026-06-01', 52)
                + row(Q1, c, 'NEW HOLDINGS', '2026-06-01', 40) + row('2025-12-31', d, 'NEW HOLDINGS', '2026-03-01', 4)
                + '</tbody></table>')
        name, filings = sources.INFO.parse_manager('1', html)
        self.assertEqual(name, 'Fund LLC')
        self.assertEqual([(f.accession, f.form, f.partial, f.count) for f in filings],
                         [(a, '13F-HR', False, 1000), (b, '13F-HR/A', False, 52), (c, '13F-HR/A', True, 40), (d, '13F-HR/A', True, 4)])
        chosen = sources.pick_filings(filings, 3)             # additions are never a period's table
        self.assertEqual([f.accession for f in chosen], [a, b])
        self.assertEqual([f.accession for f in sources.additions(filings, chosen[1])], [c])

    def test_canonical_ticker_is_stable_across_sources(self):
        row = {'cusip': 'X1', 'name': 'A', 'cls': '', 'kind': '', 'value': 1.0, 'shares': 1.0}
        first = sources.finalize([dict(row, ticker='ABC')], 0)
        later = sources.finalize([dict(row, ticker=None)], 0)
        self.assertEqual(first[0]['key'], later[0]['key'])


class AnalysisTest(Base):
    def test_fund_diff_actions(self):
        put('1', Q1, [('AAA', 100, 1000), ('BBB', 100, 1000), ('CCC', 100, 1000), ('DDD', 100, 1000)])
        put('1', Q2, [('AAA', 150, 1500), ('BBB', 50, 500), ('CCC', 102, 1020), ('EEE', 10, 980)])
        by = {p['key']: p for p in analysis.fund_diff('1', Q2, 5.0)['positions']}
        self.assertEqual({k: v['action'] for k, v in by.items()},
                         {'AAA': 'add', 'BBB': 'reduce', 'CCC': 'hold', 'DDD': 'exit', 'EEE': 'new'})
        self.assertAlmostEqual(by['AAA']['changePct'], 50)
        self.assertAlmostEqual(by['AAA']['tradeValue'], 500)
        self.assertAlmostEqual(by['DDD']['tradeValue'], -1000)
        self.assertAlmostEqual(sum(p['weight'] for p in by.values()), 100)

    def test_no_previous_quarter_means_no_actions(self):
        put('1', Q2, [('AAA', 150, 1500)])
        diff = analysis.fund_diff('1', Q2)
        self.assertIsNone(diff['prevFiling'])
        self.assertEqual(diff['positions'][0]['action'], 'hold')

    def test_split_is_not_a_purchase(self):
        for cik in ('1', '2', '3'):
            put(cik, Q1, [('SPL', 100, 10000), ('AAA', 10, 100)])
            put(cik, Q2, [('SPL', 200, 10400), ('AAA', 10, 100)])       # 2:1 split, price +4%
        self.assertEqual(analysis.market(Q1, Q2)[1], {'SPL': 2})
        row = next(p for p in analysis.fund_diff('1', Q2, 5.0)['positions'] if p['key'] == 'SPL')
        self.assertEqual(row['action'], 'hold')
        self.assertFalse(analysis.consensus(['1', '2', '3'], Q2)['rows'])

    def test_real_doubling_is_a_purchase(self):
        for cik in ('1', '2'):
            put(cik, Q1, [('AAA', 100, 10000)])
            put(cik, Q2, [('AAA', 200, 20000)])
        self.assertEqual(analysis.market(Q1, Q2)[1], {})          # price unchanged, so not a split

    def test_consensus_counts_and_options_excluded(self):
        put('1', Q1, [('AAA', 100, 1000), ('ZZZ', 100, 1000)])
        put('1', Q2, [('AAA', 200, 2000), ('ZZZ', 100, 1000), ('PPP', 5, 50, 'PUT')])
        put('2', Q1, [('ZZZ', 100, 1000)])
        put('2', Q2, [('AAA', 50, 500), ('ZZZ', 40, 400)])
        put('3', Q1, [('AAA', 100, 1000), ('ZZZ', 100, 1000)])
        put('3', Q2, [('ZZZ', 100, 1000)])
        put('4', Q2, [('AAA', 1, 10)])                                   # no previous quarter
        result = analysis.consensus(['1', '2', '3', '4'], Q2, 5.0)
        self.assertEqual([m['cik'] for m in result['missing']], ['4'])
        rows = {r['key']: r for r in result['rows']}
        aaa, zzz = rows['AAA'], rows['ZZZ']
        self.assertEqual((aaa['buyCount'], aaa['newCount'], aaa['sellCount'], aaa['exitCount']), (2, 1, 1, 1))
        self.assertEqual((zzz['buyCount'], zzz['sellCount'], zzz['holdCount']), (0, 1, 2))
        self.assertNotIn('PPP', rows)

    def test_copy_return(self):
        put('1', Q1, [('AAA', 100, 1000), ('BBB', 100, 1000)])
        put('1', Q2, [('AAA', 100, 1200)])                                # BBB sold; another fund still prices it
        put('2', Q1, [('BBB', 10, 100)])
        put('2', Q2, [('BBB', 10, 90)])
        r = analysis.copy_return('1', Q2)
        self.assertAlmostEqual(r['returnPct'], 5.0)                       # (+20% and -10%) / 2
        self.assertAlmostEqual(r['coveragePct'], 100.0)


class TradingTest(Base):
    def setUp(self):
        super().setUp()
        self.prices = {'BUYA': 100.0, 'OVRA': 40.0, 'SELA': 50.0, 'UNDA': 20.0, 'HLDA': 10.0, 'BIGA': 5000.0}
        self._quotes = trading.quotes
        trading.quotes = lambda symbols, allow_fallback=True: ({s: self.prices[s] for s in symbols if s in self.prices}, 'test')

    def tearDown(self):
        trading.quotes = self._quotes

    def rate(self, ticker, rating):
        db.run("INSERT INTO research(ticker,trade_date,status,rating,finished_at) VALUES(?,?,'done',?,?)",
               (ticker, '2026-10-06', rating, research.now()))

    def hold(self, symbol, quantity, avg):
        db.run('INSERT INTO paper_positions(symbol,quantity,avg_price) VALUES(?,?,?)', (symbol, quantity, avg))

    def test_plan_follows_ratings(self):
        for t, r in (('BUYA', 'Buy'), ('OVRA', 'Overweight'), ('SELA', 'Sell'), ('UNDA', 'Underweight'),
                     ('HLDA', 'Hold'), ('BIGA', 'Buy')):
            self.rate(t, r)
        self.hold('SELA', 7, 60)
        self.hold('UNDA', 10, 15)
        self.hold('XXXA', 3, 1)
        plan = trading.build_plan('paper')
        orders = {o['symbol']: o for o in plan['orders']}
        self.assertEqual((orders['SELA']['side'], orders['SELA']['quantity']), ('SELL', 7))
        self.assertEqual((orders['UNDA']['side'], orders['UNDA']['quantity']), ('SELL', 5))
        self.assertEqual((orders['BUYA']['side'], orders['BUYA']['quantity'], orders['BUYA']['price']), ('BUY', 4, 100.5))
        self.assertEqual(orders['OVRA']['quantity'], 6)                   # $250 target / $40.20
        self.assertEqual({s['symbol'] for s in plan['skipped']}, {'HLDA', 'BIGA'})
        self.assertEqual(plan['unresearched'], ['XXXA'])
        self.assertLess(orders['SELA']['price'], 50)                      # sell limit sits below last

    def test_bad_rating_without_position_does_nothing(self):
        self.rate('SELA', 'Sell')
        plan = trading.build_plan('paper')
        self.assertEqual(plan['orders'], [])
        self.assertEqual(plan['skipped'][0]['reason'], '보유하지 않은 종목')

    def test_buys_shrink_to_cash(self):
        db.kv_set('paper_cash', 250.0)
        self.rate('BUYA', 'Buy')
        plan = trading.build_plan('paper')
        self.assertEqual(plan['orders'][0]['quantity'], 2)
        self.assertGreaterEqual(plan['cashAfterBuys'], 0)

    def test_stale_research_is_ignored(self):
        db.run("INSERT INTO research(ticker,trade_date,status,rating,finished_at) "
               "VALUES('BUYA','2026-01-01','done','Buy','2026-01-01T00:00:00+00:00')")
        self.assertEqual(trading.build_plan('paper')['orders'], [])

    def test_paper_execution_round_trip(self):
        self.rate('BUYA', 'Buy')
        done = trading.execute('paper', [{'symbol': 'BUYA', 'side': 'BUY', 'quantity': 3}])
        self.assertEqual(done[0]['status'], 'filled')
        acct = trading.account('paper')
        self.assertAlmostEqual(acct['cash'], 10000 - 3 * 100.5)
        self.assertEqual(acct['positions'][0]['quantity'], 3)
        db.run("UPDATE research SET rating='Sell'")
        done = trading.execute('paper', [{'symbol': 'BUYA', 'side': 'SELL', 'quantity': 3}])
        self.assertEqual(done[0]['status'], 'filled')
        self.assertEqual(trading.account('paper')['positions'], [])
        self.assertAlmostEqual(trading.paper_cash(), 10000 - 3 * 100.5 + 3 * 99.5)

    def test_underweight_trims_only_once_per_research(self):
        self.rate('UNDA', 'Underweight')
        self.hold('UNDA', 10, 15)
        trading.execute('paper', [{'symbol': 'UNDA', 'side': 'SELL', 'quantity': 5}])
        plan = trading.build_plan('paper')
        self.assertEqual(plan['orders'], [])
        self.assertEqual(plan['account']['positions'][0]['quantity'], 5)

    def test_execution_rejects_orders_outside_the_plan(self):
        self.rate('BUYA', 'Buy')
        for bad in ({'symbol': 'BUYA', 'side': 'BUY', 'quantity': 99},      # above planned quantity
                    {'symbol': 'HLDA', 'side': 'BUY', 'quantity': 1},       # never researched
                    {'symbol': 'BUYA', 'side': 'SELL', 'quantity': 1}):     # wrong direction
            with self.assertRaises(trading.TradeError):
                trading.execute('paper', [bad])

    def test_live_is_locked_without_flag_and_phrase(self):
        self.rate('BUYA', 'Buy')
        order = [{'symbol': 'BUYA', 'side': 'BUY', 'quantity': 1}]
        with self.assertRaises(trading.TradeError):
            trading.execute('live', order, trading.LIVE_CONFIRM_PHRASE)     # flag off
        os.environ.update(TOSS_CLIENT_ID='x', TOSS_CLIENT_SECRET='y', TOSS_ENABLE_LIVE='true')
        try:
            with self.assertRaises(trading.TradeError):
                trading.execute('live', order, 'yes')                       # wrong phrase, nothing is sent
        finally:
            os.environ.update(TOSS_CLIENT_ID='', TOSS_CLIENT_SECRET='', TOSS_ENABLE_LIVE='false')


class ResearchTest(Base):
    def test_ticker_rules(self):
        self.assertTrue(research.is_us_ticker('NVDA'))
        self.assertTrue(research.is_us_ticker('BRK.B'))
        for bad in ('BN.TO', '0A2S.IL', None):
            self.assertFalse(research.is_us_ticker(bad))
        self.assertEqual(research.research_symbol('BRK.B'), 'BRK-B')


if __name__ == '__main__':
    unittest.main()
