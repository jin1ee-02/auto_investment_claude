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
        for table in ('funds', 'filings', 'holdings', 'securities', 'research', 'orders', 'paper_positions', 'kv',
                      'history', 'history_filings', 'splits', 'split_checks', 'split_unknown'):
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

    def test_13finfo_search_lists_managers_only(self):
        html = '''<div class="font-medium text-xl pt-2 px-3">Managers</div>
            <a href="/manager/0000921669-icahn-carl-c"><div class="px-3 py-1">
              <div class="text-xl leading-6">
                ICAHN CARL C (Carl Icahn)
              </div>
              <div class="text-base leading-4">
                Sunny Isles Beach, FL
              </div></div></a>
          <div class="font-medium text-xl pt-2 px-3">Investments</div>
            <a href="/cusip/451100101"><div><div>IEP - Icahn Enterprises</div></div></a>'''
        self.assertEqual(sources.ThirteenFInfo.parse_search(html),
                         [{'cik': '921669', 'name': 'ICAHN CARL C (Carl Icahn)', 'location': 'Sunny Isles Beach, FL'}])

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

    def test_fund_history_against_benchmark(self):
        q4 = '2025-12-31'
        put('1', q4, [('AAA', 100, 1000), ('SPY', 10, 1000)])
        put('1', Q1, [('AAA', 100, 1100), ('SPY', 10, 1050)])
        put('1', Q2, [('AAA', 200, 2420)])                                # bought more AAA, sold SPY
        put('2', Q2, [('SPY', 10, 1155)])                                 # SPY still priced by another fund
        h = analysis.fund_history('1')
        q = {x['period']: x for x in h['quarters']}
        self.assertIsNone(q[q4]['returnPct'])                             # nothing to compare with
        self.assertAlmostEqual(q[Q1]['returnPct'], 7.5)                   # (+10% and +5%) / 2
        self.assertAlmostEqual(q[Q1]['benchPct'], 5.0)
        self.assertAlmostEqual(q[Q2]['returnPct'], 10.0)                  # (+10% and +10%) / 2
        self.assertAlmostEqual(q[Q2]['cumPct'], (1.075 * 1.10 - 1) * 100)
        self.assertAlmostEqual(q[Q2]['benchCumPct'], (1.05 * 1.10 - 1) * 100)
        self.assertEqual(h['chained'], 2)
        self.assertIsNone(h['annualPct'])                                 # under four quarters: not annualised
        self.assertAlmostEqual(q[Q1]['priceEffect'], 2000 * 0.075)
        self.assertAlmostEqual(q[Q1]['priceEffect'] + q[Q1]['netFlow'], 2150 - 2000)

    def test_fund_list_track_matches_history_chain(self):
        q4 = '2025-12-31'
        put('1', q4, [('AAA', 100, 1000), ('SPY', 10, 1000)])
        put('1', Q1, [('AAA', 100, 1100), ('SPY', 10, 1050)])
        put('1', Q2, [('AAA', 200, 2420), ('SPY', 10, 1155)])
        h = analysis.fund_history('1')
        q = {x['period']: x for x in h['quarters']}
        self.assertEqual((q[q4]['chained'], q[Q1]['chained'], q[Q2]['chained']), (0, 1, 2))
        self.assertEqual(h['firstLinked'], Q1)
        early, late = analysis.track_record('1', Q1), analysis.track_record('1', Q2)
        self.assertEqual((early['quarters'], late['quarters']), (1, 2))   # each view reads its own chain
        self.assertAlmostEqual(early['cumPct'], q[Q1]['cumPct'])
        self.assertAlmostEqual(late['cumPct'], h['cumPct'])
        self.assertEqual(late['from'], '2025 Q4')

    def test_cached_analysis_resets_on_invalidate(self):
        put('1', Q1, [('AAA', 100, 1000)])
        put('1', Q2, [('AAA', 100, 1000)])
        self.assertEqual(analysis.fund_summary('1', Q2)['positions'], 1)
        db.run("INSERT INTO holdings(cik,period,key,kind,ticker,cusip,name,cls,value,shares) "
               "VALUES('1',?,'BBB','','BBB','','','',10,1)", (Q2,))
        db.run("UPDATE filings SET positions=2 WHERE cik='1' AND period=?", (Q2,))
        self.assertEqual(analysis.fund_summary('1', Q2)['positions'], 1)  # cached until told otherwise
        analysis.invalidate()
        self.assertEqual(analysis.fund_summary('1', Q2)['positions'], 2)
        self.assertIsNone(analysis.fund_summary('nope', Q2))

    def test_change_action_threshold(self):
        self.assertEqual(analysis.change_action(100, 100.5, 1.0)[0], 'hold')    # under 1%: rebalancing
        action, change = analysis.change_action(100, 102, 1.0)
        self.assertEqual(action, 'add')
        self.assertAlmostEqual(change, 2.0)
        self.assertEqual(analysis.change_action(100, 90, 5.0)[0], 'reduce')
        self.assertEqual(analysis.change_action(100, 97, 5.0)[0], 'hold')

    def test_position_history_streak_and_price(self):
        q3, q4 = '2025-09-30', '2025-12-31'
        put('1', q3, [('AAA', 100, 1000)])
        put('1', q4, [('BBB', 10, 100)])                                  # AAA dropped for a quarter
        put('1', Q1, [('AAA', 100, 1500), ('BBB', 10, 120)])
        put('1', Q2, [('AAA', 100, 2000), ('BBB', 10, 150)])
        h = analysis.position_history('1', Q2)['positions']
        self.assertEqual(h['AAA']['heldQuarters'], 2)                     # streak restarts after the gap
        self.assertFalse(h['AAA']['heldSinceStart'])
        self.assertAlmostEqual(h['AAA']['sincePct'], 2000 / 1500 * 100 - 100)
        self.assertEqual(h['BBB']['heldQuarters'], 3)
        self.assertAlmostEqual(h['BBB']['sincePct'], 50.0)
        self.assertAlmostEqual(h['BBB']['quarterPricePct'], 25.0)
        self.assertEqual(len(h['BBB']['weights']), 4)

    def test_cost_basis_uses_full_history_with_splits(self):
        def older(cik, period, rows):           # what backfill_history stores for quarters before `holdings`
            with db.tx() as c:
                sources._write_history(c, cik, period, 'old', [(k, float(v), float(s)) for k, s, v in rows])
        q0, q3, q4 = '2025-06-30', '2025-09-30', '2025-12-31'
        older('1', q0, [('AAA', 100, 1000)])                              # bought at 10, before the recent window
        older('1', q3, [('AAA', 100, 1200)])
        older('2', q3, [('AAA', 50, 600)])
        older('1', q4, [('AAA', 200, 1400)])                              # 2:1 split, seen by both holders
        older('2', q4, [('AAA', 100, 700)])
        put('1', Q1, [('AAA', 300, 2400)])                                # +100 post-split shares at 8
        put('1', Q2, [('AAA', 300, 2700)])
        h = analysis.position_history('1', Q2)['positions']['AAA']
        self.assertAlmostEqual(h['breakeven'], (200 * 5 + 100 * 8) / 300)  # 100 @10 is 200 @5 after the split
        self.assertAlmostEqual(h['plPct'], (9 / 6 - 1) * 100)
        self.assertEqual(h['heldQuarters'], 5)
        self.assertAlmostEqual(h['sincePct'], (9 / 5 - 1) * 100)
        self.assertEqual(h['basisFrom'], q0)                              # open since the first quarter on record
        self.assertEqual(h['basisFromLabel'], '2025 Q2')
        t = analysis.position_track('1', 'AAA', Q2)
        self.assertAlmostEqual(t['breakeven'], h['breakeven'])
        self.assertEqual(t['basisFromLabel'], '2025 Q2')
        older('1', '2025-03-31', [])                                      # an earlier quarter without AAA
        analysis.invalidate()
        h = analysis.position_history('1', Q2)['positions']['AAA']
        self.assertIsNone(h['basisFrom'])                                 # the purchase itself is on record
        self.assertFalse(h['heldSinceStart'])
        self.assertAlmostEqual(h['breakeven'], 6.0)

    def test_market_data_splits_override_the_13f_rule(self):
        put('1', Q1, [('AAA', 100, 10000), ('BBB', 100, 1000)])
        put('2', Q1, [('AAA', 50, 5000)])
        put('1', Q2, [('AAA', 1000, 9000), ('BBB', 200, 2400)])        # AAA 10:1, but holders traded around it
        put('2', Q2, [('AAA', 700, 6300)])
        self.assertEqual(analysis.market(Q1, Q2)[1], {})                # ratios 10 and 14: the 13F rule cannot tell
        db.run("INSERT INTO split_checks(key, upto) VALUES('AAA', ?), ('BBB', ?)", (Q2, Q2))
        db.run("INSERT INTO splits(key, period, factor) VALUES('AAA', ?, 10)", (Q2,))
        analysis.invalidate()
        self.assertEqual(analysis.market(Q1, Q2)[1], {'AAA': 10})       # BBB checked too: doubled, no split
        diff = {p['key']: p for p in analysis.fund_diff('1', Q2)['positions']}
        self.assertEqual(diff['AAA']['action'], 'hold')                 # 100 -> 1000 is the split, not a purchase
        self.assertEqual(diff['BBB']['action'], 'add')
        self.assertAlmostEqual(analysis.position_history('1', Q2)['positions']['AAA']['breakeven'], 10.0)

    def test_split_candidates_only_flag_price_jumps(self):
        put('1', Q1, [('AAA', 100, 10000), ('BBB', 100, 1000), ('C1234567', 10, 100)])
        put('1', Q2, [('AAA', 400, 11000), ('BBB', 100, 1100), ('C1234567', 40, 110)])
        self.assertEqual(sources.split_candidates(), {'AAA': Q2})      # CUSIP keys are never looked up

    def test_history_rows_keep_canonical_keys(self):
        rows = [{'ticker': 'FB', 'cusip': '30303M102', 'kind': '', 'value': 100.0, 'shares': 2.0},
                {'ticker': 'GOOGL', 'cusip': 'OLDCUSIP1', 'kind': '', 'value': 50.0, 'shares': 1.0},
                {'ticker': 'GOOGL', 'cusip': '02079K305', 'kind': '', 'value': 50.0, 'shares': 1.0},
                {'ticker': 'META', 'cusip': '30303M102', 'kind': 'CALL', 'value': 9.0, 'shares': 9.0},
                {'ticker': 'ZZZ', 'cusip': 'ZZZCUSIP1', 'kind': '', 'value': 5.0, 'shares': 5.0}]
        known = {'30303M102': 'META', '02079K305': 'GOOGL'}
        got = sorted(sources.history_rows(rows, known, {'META', 'GOOGL'}))
        self.assertEqual(got, [('GOOGL', 100.0, 2.0), ('META', 100.0, 2.0)])   # old ticker FB lands on META

    def test_split_factor(self):
        self.assertEqual(analysis.split_factor([2.0, 2.0, 2.01], 0.52), 2)
        self.assertIsNone(analysis.split_factor([2.0], 0.5))                    # one holder is not enough
        self.assertIsNone(analysis.split_factor([2.0, 2.0], 1.0))               # value doubled: a real purchase
        self.assertIsNone(analysis.split_factor([1.3, 1.5], 0.9))
        # Real splits where holders also traded (13F share ratios across tracked funds):
        self.assertEqual(analysis.split_factor([0.794, 2.219, 3.852, 4.163], 115.81 / 364.8), 4)       # AAPL 2020
        self.assertEqual(analysis.split_factor([19.06, 20.0, 20.02, 20.131, 20.783, 29.792, 41.251, 49.548, 89.443],
                                               95.65 / 2179.26), 20)                                   # GOOGL 2022
        self.assertEqual(analysis.split_factor([1.217, 1.561, 2.069, 5.93, 6.249, 8.379, 9.305, 9.525, 9.76, 9.93,
                                                10.0, 12.752], 123.54 / 903.56), 10)                   # NVDA 2024
        self.assertIsNone(analysis.split_factor([1.5, 1.52, 1.0], 0.95))       # two funds adding half: not a split
        self.assertIsNone(analysis.split_factor([2.0, 2.05, 1.0, 1.0, 1.0], 0.7))   # a crash, most holders unchanged
        # Trading that only looks like a split (seen in the tracked funds' recent 13Fs):
        self.assertIsNone(analysis.split_factor([0.714, 1.503, 1.51], 0.971))       # KMI: price flat, so no 3:2
        self.assertIsNone(analysis.split_factor([0.516, 0.517, 1.616, 1.693, 2.851], 1.235))   # DFS: halving in a rally
        self.assertIsNone(analysis.split_factor([0.328, 0.331, 1.172], 2.114))      # RUN: two funds cut by two thirds
        self.assertEqual(analysis.split_factor([2.0, 2.0, 2.0], 0.52), 2)          # an exact 2:1 still counts

    def test_cost_basis(self):
        self.assertAlmostEqual(analysis.cost_basis([(100, 10), (200, 20)]), 15.0)     # bought 100 @10, 100 @20
        self.assertAlmostEqual(analysis.cost_basis([(100, 10), (50, 30)]), 10.0)      # a trim keeps the average
        self.assertAlmostEqual(analysis.cost_basis([(100, 10), (0, 30), (10, 40)]), 40.0)   # exit starts over
        self.assertIsNone(analysis.cost_basis([(100, None)]))
        self.assertIsNone(analysis.cost_basis([(100, 10), (0, 12)]))

    def test_position_track_marks_trades_and_breakeven(self):
        q4 = '2025-12-31'
        put('1', q4, [('AAA', 100, 1000)])
        put('1', Q1, [('AAA', 300, 6000)])                                # price 10 -> 20, bought 200
        put('1', Q2, [('AAA', 150, 3750)])                                # price 25, trimmed half
        t = analysis.position_track('1', 'AAA', Q2)
        self.assertEqual([r['action'] for r in t['rows']], ['hold', 'add', 'reduce'])
        self.assertEqual([r['price'] for r in t['rows']], [10, 20, 25])
        self.assertAlmostEqual(t['breakeven'], (100 * 10 + 200 * 20) / 300)
        h = analysis.position_history('1', Q2)['positions']['AAA']
        self.assertAlmostEqual(h['breakeven'], t['breakeven'])
        self.assertAlmostEqual(h['plPct'], (25 / t['breakeven'] - 1) * 100)


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
