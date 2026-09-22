"""离线单元测试：不联网、不依赖本地行情数据，供 CI 使用。

运行：
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import akshare as ak
import pandas as pd

import re

import skin
import analyzer
import fundamentals
import signal_filter
import storage_fundamental
import storage_kline as sk
import watchlist_store

# 刻意选一个本地不会有的代码，保证用例不受开发机上的缓存数据影响
EMPTY_CODE = "999999"
DAY = "2026-09-19"


def _offline(*a, **k):
    raise ConnectionError("测试环境：网络已断开")


class OfflineBase(unittest.TestCase):
    """把所有出网口封死，保证用例离线、可重复、结果确定。"""

    def setUp(self):
        self._saved = (sk.fetch_raw, sk.fetch_factor,
                       signal_filter.trading_calendar, ak.tool_trade_date_hist_sina)
        sk.fetch_raw = _offline
        sk.fetch_factor = _offline
        signal_filter.trading_calendar = _offline
        ak.tool_trade_date_hist_sina = _offline

    def tearDown(self):
        (sk.fetch_raw, sk.fetch_factor,
         signal_filter.trading_calendar, ak.tool_trade_date_hist_sina) = self._saved


class TestAnalyzeStockContract(OfflineBase):
    """analyze_stock 的对外契约：返回结构化 dict，且永不抛异常。"""

    KEYS = ("code", "trade_date", "source", "ok", "error", "elapsed",
            "data_source", "data_error")

    def test_returns_dict_with_required_keys(self):
        r = analyzer.analyze_stock(EMPTY_CODE, DAY, use_llm=False)
        self.assertIsInstance(r, dict)
        for k in self.KEYS:
            self.assertIn(k, r, "结果缺少字段 " + k)

    def test_missing_data_gives_structured_error(self):
        r = analyzer.analyze_stock(EMPTY_CODE, DAY, use_llm=False)
        self.assertFalse(r["ok"])
        self.assertIn("无法获取 K 线数据", r["error"])
        self.assertIsNone(r["data_source"])
        self.assertTrue(r["data_error"])

    def test_allow_fetch_false_reports_reason(self):
        r = analyzer.analyze_stock(EMPTY_CODE, DAY, use_llm=False, allow_fetch=False)
        self.assertFalse(r["ok"])
        self.assertIn("未开启自动拉取", r["data_error"])

    def test_never_raises_when_calendar_unavailable(self):
        """交易日历都取不到时也必须返回结果，而不是抛异常（ADR-015）。"""
        r = analyzer.analyze_stock(EMPTY_CODE, DAY, use_llm=False)
        self.assertIsInstance(r, dict)

    def test_exception_is_converted_to_error_field(self):
        """内部实现抛异常时，应被包装成 ok=False + error。"""
        saved = analyzer._analyze_impl

        def boom(*a, **k):
            raise ValueError("故意炸一个")

        analyzer._analyze_impl = boom
        try:
            r = analyzer.analyze_stock(EMPTY_CODE, DAY, use_llm=False)
        finally:
            analyzer._analyze_impl = saved
        self.assertFalse(r["ok"])
        self.assertIn("分析过程异常", r["error"])
        self.assertIn("故意炸一个", r["data_error"])


class TestEnsureKline(OfflineBase):
    """数据兜底 ensure_kline 的离线行为。"""

    def test_no_data_no_network_returns_none_and_reason(self):
        src, err = analyzer.ensure_kline(EMPTY_CODE, DAY)
        self.assertIsNone(src)
        self.assertTrue(err)

    def test_calendar_down_with_local_data_is_cache(self):
        """日历不可用 + 本地有数据 -> 直接判 cache，不再尝试联网。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-09-18"]).astype("datetime64[ns]"),
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "volume": [1.0], "amount": [1.0], "qfq_factor": [1.0],
        })
        saved = analyzer.load_kline
        analyzer.load_kline = lambda code: df
        try:
            self.assertEqual(analyzer.ensure_kline(EMPTY_CODE, DAY), ("cache", None))
        finally:
            analyzer.load_kline = saved

    def test_calendar_failure_is_swallowed(self):
        self.assertIsNone(analyzer._expected_last_trading_day(DAY))


class TestStorageKline(unittest.TestCase):
    def test_parquet_path_layout(self):
        self.assertEqual(sk.parquet_path("600519").name, "600519.parquet")

    def test_missing_file_reads_as_empty_frame(self):
        df = sk.load_kline(EMPTY_CODE)
        self.assertTrue(df.empty)

    def test_exchange_prefix(self):
        self.assertEqual(sk.ts_code("600519"), "sh600519")
        self.assertEqual(sk.ts_code("000001"), "sz000001")


class TestModulesImport(unittest.TestCase):
    """所有模块都要能 import —— 挡住语法错、循环依赖、拉不到依赖。"""

    MODULES = ["min_loop", "signal_filter", "backtest", "storage_kline", "storage_signal",
               "storage_report", "confirm_dates", "mark_primary", "report_builder",
               "llm_client", "agent_graph", "analyzer"]

    def test_importable(self):
        for m in self.MODULES:
            with self.subTest(module=m):
                __import__(m)


class TestWatchlistStore(unittest.TestCase):
    """自选股池读写：解析、校验、保存、恢复默认（ADR-016）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = watchlist_store.LOCAL_PATH
        watchlist_store.LOCAL_PATH = Path(self._tmp.name) / "watchlist.local.yaml"

    def tearDown(self):
        watchlist_store.LOCAL_PATH = self._saved
        self._tmp.cleanup()

    def test_max_stocks_is_ten(self):
        self.assertEqual(watchlist_store.MAX_STOCKS, 10)

    def test_parse_mixed_separators_and_dedup(self):
        got = watchlist_store.parse_codes("600519, 601318\n000001、000002; 600519")
        self.assertEqual(got, ["600519", "601318", "000001", "000002"])

    def test_parse_empty(self):
        self.assertEqual(watchlist_store.parse_codes(""), [])
        self.assertEqual(watchlist_store.parse_codes(None), [])

    def test_validate_rejects_non_six_digit(self):
        ok, bad = watchlist_store.validate_codes(["600519", "60051", "abcd", "000001"])
        self.assertEqual(ok, ["600519", "000001"])
        self.assertEqual([c for c, _ in bad], ["60051", "abcd"])

    def test_save_then_load_roundtrip(self):
        watchlist_store.save_watchlist(["600519", "000001"])
        self.assertTrue(watchlist_store.is_custom())
        self.assertEqual(watchlist_store.load_watchlist(), ["600519", "000001"])

    def test_save_strips_blanks(self):
        watchlist_store.save_watchlist(["600519", "", "  ", "000001"])
        self.assertEqual(watchlist_store.load_watchlist(), ["600519", "000001"])

    def test_reset_falls_back_to_repo_default(self):
        watchlist_store.save_watchlist(["600519"])
        self.assertTrue(watchlist_store.reset_watchlist())
        self.assertFalse(watchlist_store.is_custom())
        self.assertEqual(watchlist_store.load_watchlist(), watchlist_store.load_default())

    def test_empty_or_corrupt_local_falls_back(self):
        watchlist_store.LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        watchlist_store.LOCAL_PATH.write_text("watchlist: []\n", encoding="utf-8")
        self.assertFalse(watchlist_store.is_custom())
        self.assertEqual(watchlist_store.load_watchlist(), watchlist_store.load_default())

    def test_default_pool_comes_from_repo_settings(self):
        self.assertIn("600519", watchlist_store.load_default())

    def test_default_pool_within_limit(self):
        """仓库默认池不得超过 UI 上限，否则页面一打开就报错、两个按钮全灰。"""
        self.assertLessEqual(len(watchlist_store.load_default()), watchlist_store.MAX_STOCKS)

    def test_report_builder_delegates_to_store(self):
        import report_builder
        self.assertEqual(report_builder.load_watchlist(), watchlist_store.load_watchlist())


class TestFundamentals(unittest.TestCase):
    """基本面解析与格式化（离线，纯函数）。"""

    def test_parse_num_units(self):
        self.assertEqual(fundamentals.parse_num("272.43亿"), 27243000000.0)
        self.assertEqual(fundamentals.parse_num("5,123.4万"), 51234000.0)
        self.assertEqual(fundamentals.parse_num("21.7600"), 21.76)

    def test_wanyi_matched_before_yi(self):
        # "万亿" 必须排在 "亿" 之前匹配，否则 1.2万亿 会被解析成 1.2亿
        self.assertEqual(fundamentals.parse_num("1.2万亿"), 1.2e12)

    def test_percent_kept_as_percent(self):
        self.assertEqual(fundamentals.parse_num("1.47%"), 1.47)

    def test_parse_num_blank_and_junk(self):
        for v in (None, False, True, "", "  ", "nan", "None", "abc", "--"):
            self.assertIsNone(fundamentals.parse_num(v), repr(v))

    def test_tx_idx_verified_anchors(self):
        """这三个下标当初是用财务数据反算校验过的，改动必须显式意识到。"""
        self.assertEqual(fundamentals.TX_IDX["pe_ttm"], 39)
        self.assertEqual(fundamentals.TX_IDX["pb"], 46)
        self.assertEqual(fundamentals.TX_IDX["total_cap_yi"], 45)
        self.assertTrue(all(isinstance(i, int) and i > 0
                            for i in fundamentals.TX_IDX.values()))

    def test_summarize_empty_is_safe(self):
        s = fundamentals.summarize({})
        self.assertEqual(s["errors"], [])
        self.assertEqual(s["history"], [])
        self.assertIsNone(s["pe_ttm"])
        self.assertEqual(fundamentals.format_lines(s), [])

    def test_summarize_takes_newest_period(self):
        s = fundamentals.summarize({"financials": [
            {"report_period": "2026-06-30", "net_profit": 4.45e10, "roe": 16.75},
            {"report_period": "2026-03-31", "net_profit": 2.72e10, "roe": 10.57}]})
        self.assertEqual(s["report_period"], "2026-06-30")
        self.assertEqual(len(s["history"]), 2)

    def test_format_lines_contains_key_facts(self):
        s = fundamentals.summarize({
            "profile": {"industry": "白酒", "listing_date": "2001-08-27"},
            "valuation": {"pe_ttm": 19.3, "pb": 6.25, "total_cap_yi": 15715.03}})
        txt = "\n".join(fundamentals.format_lines(s))
        self.assertIn("白酒", txt)
        self.assertIn("19.30", txt)
        self.assertIn("2001-08-27", txt)

    def test_formatters_handle_none(self):
        self.assertEqual(fundamentals.fmt_num(None), "—")
        self.assertEqual(fundamentals.fmt_pct(None), "—")
        self.assertEqual(fundamentals.fmt_yi(None), "—")
        self.assertEqual(fundamentals.fmt_yi(2.7243e10), "272.43亿")
        self.assertEqual(fundamentals.fmt_yi_plain(15715.03), "15715.03亿")


class TestStorageFundamental(unittest.TestCase):
    """基本面缓存读写（用临时库，不碰真实 data/chan_agent.db）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = storage_fundamental.DB_PATH
        storage_fundamental.DB_PATH = Path(self._tmp.name) / "t.db"

    def tearDown(self):
        storage_fundamental.DB_PATH = self._saved
        self._tmp.cleanup()

    def test_stale_rules(self):
        self.assertTrue(storage_fundamental.is_stale(None, 7))
        self.assertTrue(storage_fundamental.is_stale("垃圾值", 7))
        self.assertFalse(storage_fundamental.is_stale(storage_fundamental.now_str(), 7))
        self.assertTrue(storage_fundamental.is_stale("2020-01-01 00:00:00", 7))

    def test_financials_roundtrip_newest_first(self):
        storage_fundamental.save_financials("600519", [
            {"report_period": "2026-03-31", "net_profit": 1.0, "roe": 10.0},
            {"report_period": "2026-06-30", "net_profit": 2.0, "roe": 16.0}])
        rows = storage_fundamental.load_financials("600519")
        self.assertEqual([r["report_period"] for r in rows], ["2026-06-30", "2026-03-31"])
        self.assertEqual(rows[0]["net_profit"], 2.0)
        self.assertTrue(rows[0]["updated_at"])

    def test_save_financials_is_upsert(self):
        """同一报告期重复写不能变成两行（靠 (code, report_period) 主键 + upsert）。"""
        storage_fundamental.save_financials("600519", [{"report_period": "2026-06-30", "roe": 1.0}])
        storage_fundamental.save_financials("600519", [{"report_period": "2026-06-30", "roe": 2.0}])
        rows = storage_fundamental.load_financials("600519")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["roe"], 2.0)

    def test_profile_roundtrip_and_upsert(self):
        self.assertIsNone(storage_fundamental.load_profile("600519"))
        storage_fundamental.save_profile({"code": "600519", "name": "贵州茅台", "industry": "白酒"})
        self.assertEqual(storage_fundamental.load_profile("600519")["industry"], "白酒")
        storage_fundamental.save_profile({"code": "600519", "name": "贵州茅台", "industry": "酿酒"})
        self.assertEqual(storage_fundamental.load_profile("600519")["industry"], "酿酒")

    def test_missing_code_returns_empty(self):
        self.assertEqual(storage_fundamental.load_financials("000000"), [])
        self.assertIsNone(storage_fundamental.load_profile("000000"))

    def test_get_fundamentals_never_raises_when_all_sources_down(self):
        saved = (fundamentals.fetch_profile, fundamentals.fetch_financials,
                 fundamentals.fetch_valuation)

        def boom(*a, **k):
            raise ConnectionError("断网")

        fundamentals.fetch_profile = boom
        fundamentals.fetch_financials = boom
        fundamentals.fetch_valuation = boom
        try:
            r = fundamentals.get_fundamentals("600519", refresh=True)
        finally:
            (fundamentals.fetch_profile, fundamentals.fetch_financials,
             fundamentals.fetch_valuation) = saved
        self.assertEqual(r["financials"], [])
        self.assertIsNone(r["valuation"])
        self.assertEqual(len(r["errors"]), 3, "三个数据源失败都应记进 errors 而不是抛出")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestVerifyEdge(unittest.TestCase):
    """有效性对照里的统计计算（离线，纯函数）。

    这是「测量工具」，算错了比没有更糟 —— 所以每个公式都用已知值钉死。
    """

    def setUp(self):
        import verify_edge
        self.ve = verify_edge

    def test_t_crit_table(self):
        self.assertAlmostEqual(self.ve.t_crit(1), 12.706, places=3)
        self.assertAlmostEqual(self.ve.t_crit(30), 2.042, places=3)
        self.assertEqual(self.ve.t_crit(200), 1.96)

    def test_mean_ci_known_value(self):
        m, lo, hi = self.ve.mean_ci([1, 2, 3, 4, 5])
        self.assertAlmostEqual(m, 3.0)
        self.assertAlmostEqual(lo, 1.0371, places=3)
        self.assertAlmostEqual(hi, 4.9629, places=3)

    def test_mean_ci_tiny_samples(self):
        self.assertEqual(self.ve.mean_ci([]), (None, None, None))
        self.assertEqual(self.ve.mean_ci([0.5]), (0.5, None, None))

    def test_wilson_known_values(self):
        lo, hi = self.ve.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        lo, hi = self.ve.wilson(10, 10)
        self.assertAlmostEqual(hi, 1.0, places=6)
        self.assertLess(lo, 1.0)
        self.assertAlmostEqual(self.ve.wilson(0, 10)[0], 0.0, places=6)

    def test_profit_factor(self):
        self.assertAlmostEqual(self.ve.profit_factor([0.1, -0.05, 0.2, -0.15]), 1.5, places=6)
        self.assertEqual(self.ve.profit_factor([0.1, 0.2]), float("inf"))
        self.assertEqual(self.ve.profit_factor([-0.1]), 0.0)

    def test_max_losing_streak_sorts_by_date(self):
        rows = [("2026-01-01", 1), ("2026-01-02", 0), ("2026-01-03", 0),
                ("2026-01-04", 0), ("2026-01-05", 1)]
        self.assertEqual(self.ve.max_losing_streak(rows), 3)
        self.assertEqual(self.ve.max_losing_streak(list(reversed(rows))), 3)

    def test_quantile_endpoints(self):
        self.assertEqual(self.ve.quantile([1, 2, 3, 4, 5], 0.0), 1.0)
        self.assertEqual(self.ve.quantile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertEqual(self.ve.quantile([1, 2, 3, 4, 5], 1.0), 5.0)

    def test_cost_per_round_trip(self):
        class A:
            commission, stamp, transfer, slippage = 0.00025, 0.0005, 0.00001, 0.0005
        self.assertAlmostEqual(self.ve.cost_per_round_trip(A), 0.00202, places=8)

    def test_zero_cost_gives_zero(self):
        class A:
            commission = stamp = transfer = slippage = 0.0
        self.assertEqual(self.ve.cost_per_round_trip(A), 0.0)

    def test_type_order_pairs_buy_sell(self):
        got = self.ve.all_types([{"signal_type": t} for t in
                                 ["第三类卖点", "第一类买点", "第二类买点", "第一类卖点"]])
        self.assertEqual(got, ["第一类买点", "第一类卖点", "第二类买点", "第三类卖点"])

    def test_direction_follows_type(self):
        import backtest as bt
        self.assertEqual(bt.direction_of("第一类买点"), 1)
        self.assertEqual(bt.direction_of("第三类卖点"), -1)

    def test_split_point_counts_distinct_dates(self):
        rows = [{"entry_date": d} for d in
                ["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]]
        # 去重后 4 个日期，下标 int(4*0.6)=2 -> 切在第三个日期
        self.assertEqual(self.ve.split_point(rows, 0.6), "2026-01-03")

    def test_split_point_never_returns_last_date(self):
        """切分点必须留得下验证期，否则后 40% 是空的。"""
        rows = [{"entry_date": d} for d in ["2026-01-01", "2026-01-02", "2026-01-03"]]
        self.assertNotEqual(self.ve.split_point(rows, 0.99), "2026-01-03")
        self.assertIsNone(self.ve.split_point([{"entry_date": "2026-01-01"}], 0.6))

    def test_partition_keeps_cut_in_observation(self):
        rows = [{"entry_date": d} for d in ["2026-01-01", "2026-01-05", "2026-01-09"]]
        a, b = self.ve.partition(rows, "2026-01-05")
        self.assertEqual([r["entry_date"] for r in a], ["2026-01-01", "2026-01-05"])
        self.assertEqual([r["entry_date"] for r in b], ["2026-01-09"])

    def test_assign_blocks_monotonic_and_clamped(self):
        rows = [{"entry_date": d} for d in
                ["2026-01-01", "2026-01-11", "2026-01-21", "2026-01-31"]]
        idx = self.ve.assign_blocks(rows, 4)
        got = [idx[i] for i in range(4)]
        self.assertEqual(got, sorted(got))
        self.assertEqual(got[0], 0)
        self.assertEqual(got[-1], 3, "最后一天必须落进最后一段，不能越界")

    def test_assign_blocks_degenerate_inputs(self):
        rows = [{"entry_date": "2026-01-01"}] * 3
        self.assertEqual(set(self.ve.assign_blocks(rows, 1).values()), {0})
        self.assertEqual(set(self.ve.assign_blocks(rows, 5).values()), {0},
                         "可选日期比段数还少时全部落第 1 段，不能崩")

    def test_judge_rules(self):
        s = {"n": 100, "exc": 0.01, "exc_lo": 0.001, "exc_hi": 0.02, "net": 0.008}
        self.assertEqual(self.ve.judge(s, 0.002, 30), "✓ 正超额")
        self.assertEqual(self.ve.judge(dict(s, n=10), 0.002, 30), "⚠ 样本不足")
        self.assertEqual(self.ve.judge(dict(s, exc_lo=-0.001), 0.002, 30), "超额不显著")
        self.assertEqual(self.ve.judge(dict(s, net=-0.001), 0.002, 30), "扣费后为负")
        self.assertEqual(self.ve.judge(None, 0.002, 30), "—")

    def test_stats_for_empty_is_none(self):
        self.assertIsNone(self.ve.stats_for([], 5, 0.002))

    def test_welch_diff_ci_known_value(self):
        d, lo, hi = self.ve.welch_diff_ci([1, 2, 3, 4, 5], [2, 3, 4, 5, 6])
        self.assertAlmostEqual(d, -1.0, places=6)
        self.assertAlmostEqual(lo, -3.3064, places=3)
        self.assertAlmostEqual(hi, 1.3064, places=3)

    def test_welch_diff_ci_needs_two_samples_each(self):
        self.assertEqual(self.ve.welch_diff_ci([1], [2, 3]), (None, None, None))
        self.assertEqual(self.ve.welch_diff_ci([1, 2], [3]), (None, None, None))

    def test_index_return_uses_open_to_close(self):
        d = pd.DataFrame({"date": pd.to_datetime(["2026-01-05", "2026-01-09"]),
                          "open": [100.0, 200.0], "close": [110.0, 220.0],
                          "high": [0.0, 0.0], "low": [0.0, 0.0], "volume": [0, 0]})
        im = self.ve.make_idx_map(d)
        self.assertAlmostEqual(self.ve.index_return(im, "2026-01-05", "2026-01-09"), 1.20)
        self.assertIsNone(self.ve.index_return(im, "2026-01-05", "2099-01-01"))
        self.assertIsNone(self.ve.index_return(im, "1999-01-01", "2026-01-09"))
        self.assertEqual(self.ve.make_idx_map(None), {})


class TestStorageIndex(unittest.TestCase):
    """指数日线存取（离线：只测符号、过期、落盘往返）。"""

    def setUp(self):
        import storage_index
        self.si = storage_index
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = storage_index.INDEX_DIR
        storage_index.INDEX_DIR = Path(self._tmp.name)

    def tearDown(self):
        self.si.INDEX_DIR = self._saved
        self._tmp.cleanup()

    def test_symbol_prefix(self):
        """000xxx 在上交所、399xxx 在深交所 —— 搞反了会拉到错误的指数。"""
        self.assertEqual(self.si.symbol_of("000300"), "sh000300")
        self.assertEqual(self.si.symbol_of("000001"), "sh000001")
        self.assertEqual(self.si.symbol_of("399001"), "sz399001")

    def test_index_name_known_and_fallback(self):
        self.assertEqual(self.si.index_name("000300"), "沪深300")
        self.assertEqual(self.si.index_name("999999"), "999999")

    def test_missing_file_reads_as_empty(self):
        df = self.si.load_index("000300")
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), self.si.COLUMNS)
        self.assertTrue(self.si.is_stale(df))

    def test_save_load_roundtrip_sorted_dedup(self):
        raw = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-09", "2026-01-05", "2026-01-05"]),
            "open": [3.0, 1.0, 9.0], "high": [3.0, 1.0, 9.0],
            "low": [3.0, 1.0, 9.0], "close": [3.0, 1.0, 9.0], "volume": [1, 1, 1]})
        self.assertEqual(self.si.save_index("000300", raw), 2)
        got = self.si.load_index("000300")
        self.assertEqual([str(d.date()) for d in got["date"]], ["2026-01-05", "2026-01-09"])
        self.assertEqual(got["close"].iloc[0], 9.0, "重复日期应保留最后一条")

    def test_is_stale_boundary(self):
        import datetime as _dt
        today = _dt.date(2026, 1, 10)
        fresh = pd.DataFrame({"date": pd.to_datetime(["2026-01-08"])})
        old = pd.DataFrame({"date": pd.to_datetime(["2025-12-01"])})
        self.assertFalse(self.si.is_stale(fresh, today=today))
        self.assertTrue(self.si.is_stale(old, today=today))
        self.assertTrue(self.si.is_stale(None, today=today))


class TestMarketRegime(unittest.TestCase):
    """市场状态判断（离线，纯函数）。"""

    def setUp(self):
        import market_regime
        self.mr = market_regime

    @staticmethod
    def _idx(closes, start="2026-01-05"):
        n = len(closes)
        return pd.DataFrame({
            "date": pd.bdate_range(start, periods=n),
            "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": [1] * n})

    def test_uptrend_labels(self):
        d = self.mr.with_indicators(self._idx([100, 101, 102, 103, 104, 105]),
                                    ma_win=3, mom_win=2)
        self.assertEqual(list(d["regime"][:2]), ["未知", "未知"], "样本不足应是未知")
        self.assertEqual(list(d["regime"][2:]), ["上行"] * 4)
        self.assertTrue(bool(d["above_ma"].iloc[2]))

    def test_downtrend_labels(self):
        d = self.mr.with_indicators(self._idx([105, 104, 103, 102, 101, 100]),
                                    ma_win=3, mom_win=2)
        self.assertEqual(list(d["regime"][2:]), ["下行"] * 4)

    def test_sideways_is_the_else_branch(self):
        # 第 4 根：收盘跌破均线但 2 日动量刚好为 0 -> 既不上行也不下行
        d = self.mr.with_indicators(self._idx([100, 101, 102, 101, 100, 99]),
                                    ma_win=3, mom_win=2)
        self.assertEqual(d["regime"].iloc[2], "上行")
        self.assertEqual(d["regime"].iloc[3], "震荡")
        self.assertEqual(d["regime"].iloc[4], "下行")

    def test_label_edge_cases(self):
        self.assertEqual(self.mr._label(True, float("nan"), 1.0, 1.0), self.mr.UNKNOWN)
        self.assertEqual(self.mr._label(True, float("nan"), 1.0, float("nan")), self.mr.UNKNOWN)
        self.assertEqual(self.mr._label(True, 0.01, 1.0, 0.5), "上行")
        self.assertEqual(self.mr._label(False, -0.01, 1.0, 0.5), "下行")
        self.assertEqual(self.mr._label(True, -0.01, 1.0, 0.5), "震荡")
        self.assertEqual(self.mr._label(False, 0.01, 1.0, 0.5), "震荡")

    def test_row_on_never_returns_future(self):
        """用「<= date」而不是「== date」：非交易日也要能取到状态，但绝不能取到未来。"""
        d = self.mr.with_indicators(self._idx([100, 101, 102, 103, 104, 105]),
                                    ma_win=3, mom_win=2)
        r = self.mr.row_on(d, "2026-01-07")
        self.assertEqual(str(r["date"].date()), "2026-01-07")
        r = self.mr.row_on(d, "2026-01-10")          # 周六，无行情
        self.assertEqual(str(r["date"].date()), "2026-01-09", "应回退到最近一个已有交易日")
        self.assertIsNone(self.mr.row_on(d, "2026-01-01"), "早于全部数据应返回 None")

    def test_regime_on_empty_and_early(self):
        d = self.mr.with_indicators(self._idx([100, 101, 102]), ma_win=3, mom_win=2)
        self.assertEqual(self.mr.regime_on(d, "2026-01-01"), self.mr.UNKNOWN)
        self.assertEqual(self.mr.regime_on(pd.DataFrame(), "2026-01-07"), self.mr.UNKNOWN)

    def test_above_ma_on(self):
        d = self.mr.with_indicators(self._idx([100, 101, 102, 103, 104, 105]),
                                    ma_win=3, mom_win=2)
        self.assertTrue(self.mr.above_ma_on(d, "2026-01-09"))
        self.assertIsNone(self.mr.above_ma_on(d, "2026-01-01"))


class TestVerifyRobustness(unittest.TestCase):
    """参数敏感性 / 信号稳定性里的纯函数（离线）。

    重点守两件事：
      1. 扫描范围必须包含「当前值」，否则输出里没法做对照
      2. signals_on 的默认参数必须跟着 min_loop 的常量走，不能各写一份
    """

    def setUp(self):
        import verify_robustness
        self.vr = verify_robustness

    def test_jaccard(self):
        self.assertEqual(self.vr.jaccard(set(), set()), 1.0)
        self.assertEqual(self.vr.jaccard({1, 2}, {1, 2}), 1.0)
        self.assertAlmostEqual(self.vr.jaccard({1, 2}, {2, 3}), 1 / 3)
        self.assertEqual(self.vr.jaccard({1}, set()), 0.0)
        self.assertEqual(self.vr.jaccard(set(), {1}), 0.0)

    def test_forward_returns_uses_next_open(self):
        """入场必须是**信号日次一交易日**开盘 —— 用信号日开盘就是未来函数。"""
        bars = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03",
                                    "2026-01-04", "2026-01-05", "2026-01-06"]),
            "open": [100.0] * 6,
            "close": [100.0, 100.0, 110.0, 120.0, 130.0, 140.0],
        })
        got = self.vr.forward_returns({("2026-01-01", "第一类买点")}, bars, 3)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0], 0.20, places=6)

    def test_forward_returns_sell_is_direction_adjusted(self):
        bars = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03",
                                    "2026-01-04", "2026-01-05", "2026-01-06"]),
            "open": [100.0] * 6,
            "close": [100.0, 100.0, 110.0, 120.0, 130.0, 140.0],
        })
        got = self.vr.forward_returns({("2026-01-01", "第一类卖点")}, bars, 3)
        self.assertAlmostEqual(got[0], -0.20, places=6)

    def test_forward_returns_skips_signals_at_the_edge(self):
        bars = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
            "open": [100.0] * 3,
            "close": [100.0] * 3,
        })
        self.assertEqual(self.vr.forward_returns({("2026-01-03", "第一类买点")}, bars, 3), [])
        self.assertEqual(self.vr.forward_returns({("2099-01-01", "第一类买点")}, bars, 3), [])

    def test_baseline_matches_min_loop_constants(self):
        import min_loop
        self.assertEqual(self.vr.BASELINE["min_bi_len"], min_loop.MIN_BI_LEN)
        self.assertEqual(self.vr.BASELINE["max_zs_bis"], min_loop.MAX_ZS_BIS)
        self.assertEqual(self.vr.BASELINE["power_tol"], min_loop.POWER_TOL)

    def test_every_sweep_contains_current_value(self):
        for key, values, _ in self.vr.SWEEPS:
            self.assertIn(self.vr.BASELINE[key], values,
                          "%s 的扫描范围必须包含当前值，否则没法对照" % key)


class TestVerifyLevel(unittest.TestCase):
    """级别共振里的纯函数（离线）。重点是**防未来函数**那条边界。"""

    def setUp(self):
        import verify_level
        self.vl = verify_level

    @staticmethod
    def _daily(n=10):
        # 2026-01-05 是周一，10 个工作日正好两周
        return pd.DataFrame({
            "date": pd.bdate_range("2026-01-05", periods=n),
            "open": list(range(1, n + 1)), "high": list(range(11, n + 11)),
            "low": list(range(101, n + 111))[:n], "close": list(range(201, n + 201)),
            "volume": [1] * n, "amount": [2] * n,
        })

    def test_week_start_is_monday(self):
        self.assertEqual(str(self.vl.week_start("2026-01-05").date()), "2026-01-05")
        self.assertEqual(str(self.vl.week_start("2026-01-07").date()), "2026-01-05")
        self.assertEqual(str(self.vl.week_start("2026-01-11").date()), "2026-01-05")

    def test_weekly_bars_uses_last_trading_day(self):
        """周线的 date 必须是该周**最后一个真实交易日**，不是日历周五。"""
        w = self.vl.weekly_bars(self._daily(10))
        self.assertEqual(len(w), 2)
        self.assertEqual(str(w["date"].iloc[0].date()), "2026-01-09")
        self.assertEqual(str(w["date"].iloc[1].date()), "2026-01-16")
        self.assertEqual(w["open"].iloc[0], 1)        # first
        self.assertEqual(w["high"].iloc[0], 15)       # max of 11..15
        self.assertEqual(w["low"].iloc[0], 101)       # min of 101..105
        self.assertEqual(w["close"].iloc[0], 205)     # last of 201..205
        self.assertEqual(w["volume"].iloc[0], 5)      # sum

    def test_weekly_bars_handles_empty(self):
        self.assertTrue(self.vl.weekly_bars(pd.DataFrame()).empty)

    def test_weekly_index_never_sees_current_week(self):
        """最关键的一条：不管信号日落在周几，取到的周线必须**严格早于本周一**。"""
        w = self.vl.weekly_bars(self._daily(10))
        checked = 0
        for d in ["2026-01-05", "2026-01-07", "2026-01-09",
                  "2026-01-12", "2026-01-14", "2026-01-16"]:
            j = self.vl.weekly_index_for(w, d)
            if j is None:
                continue
            checked += 1
            self.assertLess(w["date"].iloc[j], self.vl.week_start(d),
                            "%s 取到了当周或更晚的周线（未来函数）" % d)
        self.assertGreater(checked, 0, "至少要有一个信号日能取到历史周线，否则这条没测到")

    def test_weekly_index_none_when_no_history(self):
        w = self.vl.weekly_bars(self._daily(5))          # 只有第 1 周
        self.assertIsNone(self.vl.weekly_index_for(w, "2026-01-07"),
                          "第 1 周内没有任何「已走完」的周线，必须返回 None")

    def test_group_of_w1(self):
        self.assertTrue(self.vl.group_of("W1 周线笔同向", {"bi_dir": "向上"}, 1))
        self.assertFalse(self.vl.group_of("W1 周线笔同向", {"bi_dir": "向上"}, -1))
        self.assertTrue(self.vl.group_of("W1 周线笔同向", {"bi_dir": "向下"}, -1))
        self.assertIsNone(self.vl.group_of("W1 周线笔同向", {"bi_dir": None}, 1))

    def test_group_of_w2_is_direction_symmetric(self):
        above = {"close": 100.0, "zg": 90.0, "zd": 80.0}
        self.assertTrue(self.vl.group_of("W2 周线中枢", above, 1))
        self.assertFalse(self.vl.group_of("W2 周线中枢", above, -1))
        below = {"close": 70.0, "zg": 90.0, "zd": 80.0}
        self.assertFalse(self.vl.group_of("W2 周线中枢", below, 1))
        self.assertTrue(self.vl.group_of("W2 周线中枢", below, -1))
        self.assertIsNone(self.vl.group_of("W2 周线中枢", {"close": 100.0}, 1))

    def test_group_of_w3(self):
        st = {"close": 100.0, "ma20": 95.0}
        self.assertTrue(self.vl.group_of("W3 周线MA20", st, 1))
        self.assertFalse(self.vl.group_of("W3 周线MA20", st, -1))
        self.assertIsNone(self.vl.group_of("W3 周线MA20", {"close": 100.0}, 1))

    def test_group_of_unknown_condition_and_no_state(self):
        self.assertIsNone(self.vl.group_of("W9 不存在", {"close": 1}, 1))
        self.assertIsNone(self.vl.group_of("W1 周线笔同向", None, 1))


class TestVerifyHypotheses(unittest.TestCase):
    """假设工具里的纯函数（离线）。

    重点是 match 的边界与 validate 的拦截能力 —— 后者挡住 LLM 编造特征名。
    """

    def setUp(self):
        import verify_hypotheses
        self.vh = verify_hypotheses

    def test_match_numeric_ops(self):
        row = {"v": 1.5}
        for op, t, want in ((">=", 1.5, True), (">", 1.5, False), (">", 1.4, True),
                            ("<=", 1.5, True), ("<", 1.5, False), ("<", 1.6, True)):
            self.assertEqual(self.vh.match(row, [{"feature": "v", "op": op, "threshold": t}]),
                             want, "%s %s" % (op, t))

    def test_match_categorical_in(self):
        row = {"position": "中枢上方"}
        self.assertTrue(self.vh.match(row, [{"feature": "position", "in": ["中枢上方"]}]))
        self.assertFalse(self.vh.match(row, [{"feature": "position", "in": ["中枢下方"]}]))

    def test_match_is_and_across_conditions(self):
        row = {"position": "中枢上方", "bi_pct": 0.2}
        cond = [{"feature": "position", "in": ["中枢上方"]}, {"feature": "bi_pct", "op": ">=", "threshold": 0.13}]
        self.assertTrue(self.vh.match(row, cond))
        self.assertFalse(self.vh.match(dict(row, bi_pct=0.05), cond))

    def test_match_treats_nan_as_no_match(self):
        """缺失值不能当成满足条件 —— 否则会把「没数据」算进实验组。"""
        for bad in (None, float("nan")):
            self.assertFalse(self.vh.match({"v": bad}, [{"feature": "v", "op": ">=", "threshold": 0}]))

    def test_extract_json_handles_fences(self):
        self.assertEqual(self.vh.extract_json('["a"]'), ["a"])
        self.assertEqual(self.vh.extract_json('\n```json\n["a"]\n```\n'), ["a"])
        self.assertEqual(self.vh.extract_json('说明文字 [{"id":"H1"}] 结尾'), [{"id": "H1"}])

    def test_extract_json_raises_without_array(self):
        with self.assertRaises(ValueError):
            self.vh.extract_json("没有数组")

    def test_validate_accepts_well_formed(self):
        good = [{"id": "H1", "hypothesis": "x", "theory": "y", "predict": "high",
                 "when": [{"feature": "vol_ratio_20", "op": ">=", "threshold": 1.5}]},
                {"id": "H2", "hypothesis": "x", "theory": "y", "predict": "low",
                 "when": [{"feature": "position", "in": ["中枢上方"]}]}]
        self.assertEqual(self.vh.validate(good), [])

    def test_validate_rejects_unknown_feature(self):
        bad = [{"id": "H1", "hypothesis": "x", "theory": "y", "predict": "high",
                "when": [{"feature": "macd", "op": ">=", "threshold": 1}]}]
        self.assertTrue(any("未知特征" in e for e in self.vh.validate(bad)))

    def test_validate_rejects_bad_op_and_missing_threshold(self):
        bad = [{"id": "H1", "hypothesis": "x", "theory": "y", "predict": "high",
                "when": [{"feature": "vol_ratio_20", "op": "≈", "threshold": 1}]},
               {"id": "H2", "hypothesis": "x", "theory": "y", "predict": "high",
                "when": [{"feature": "vol_ratio_20", "op": ">="}]}]
        errs = self.vh.validate(bad)
        self.assertTrue(any("op 非法" in e for e in errs))
        self.assertTrue(any("缺 threshold" in e for e in errs))

    def test_validate_rejects_bad_predict_and_empty_when(self):
        bad = [{"id": "H1", "hypothesis": "x", "theory": "y", "predict": "up", "when": []}]
        errs = self.vh.validate(bad)
        self.assertTrue(any("predict" in e for e in errs))
        self.assertTrue(any("when" in e for e in errs))

    def test_validate_rejects_missing_field(self):
        errs = self.vh.validate([{"id": "H1", "when": [{"feature": "regime", "in": ["上行"]}]}])
        self.assertTrue(any("缺字段" in e for e in errs))


class TestStructureGap(unittest.TestCase):
    """H3 的距中枢结束天数（离线，纯函数）。

    这个字段进了报告和 UI，标签写错会直接误导用户，所以单独钉住。
    """

    def setUp(self):
        import structure_gap
        self.sg = structure_gap

    def test_note_of(self):
        f = self.sg.note_of
        self.assertEqual(f(None), "")
        self.assertEqual(f(-1), "无中枢可参照")
        self.assertIn("较近", f(0))
        self.assertIn("较近", f(self.sg.GAP_THRESHOLD - 1))
        self.assertIn("较远", f(self.sg.GAP_THRESHOLD))

    def test_label_of(self):
        f = self.sg.label_of
        self.assertEqual(f(None), "—")
        self.assertEqual(f(-1), "无中枢")
        self.assertEqual(f(3), "3 日")
        self.assertIn("⚠️", f(self.sg.GAP_THRESHOLD))

    def test_threshold_is_ten(self):
        """阈值来自 H3（ADR-021）。改它必须显式意识到会改动报告/UI 的提示口径。"""
        self.assertEqual(self.sg.GAP_THRESHOLD, 10)

    def test_annotate_writes_all_three_fields(self):
        sigs = [{"date": "2026-01-05", "type": "第一类买点"},
                {"date": "2026-01-06", "type": "第二类买点"},
                {"date": "2026-01-07", "type": "第三类买点"}]
        self.sg.annotate(sigs, {("2026-01-05", "第一类买点"): 3,
                                ("2026-01-06", "第二类买点"): 15})
        self.assertEqual(sigs[0]["zs_gap_days"], 3)
        self.assertFalse(sigs[0]["zs_far"])
        self.assertTrue(sigs[1]["zs_far"])
        self.assertIn("较远", sigs[1]["zs_note"])
        self.assertIsNone(sigs[2]["zs_gap_days"])
        self.assertEqual(sigs[2]["zs_note"], "")

    def test_fmt_gap_matches_threshold(self):
        import llm_client
        self.assertEqual(llm_client._fmt_gap({"zs_gap_days": None}), "")
        self.assertIn("无中枢", llm_client._fmt_gap({"zs_gap_days": -1}))
        self.assertNotIn("偏低", llm_client._fmt_gap({"zs_gap_days": 3}))
        self.assertIn("偏低", llm_client._fmt_gap({"zs_gap_days": 10}))


class TestMarketScan(unittest.TestCase):
    """全市场扫描器的纯函数（离线）。

    ok_code 是「能扫谁」的唯一判定 —— 它错了会漏掉整个板块（北交所 92 开头踩过），
    或者把 ST 放进结果里。
    """

    def setUp(self):
        import market_scan
        self.ms = market_scan

    def test_ok_code_accepts_four_boards(self):
        for c in ("600519", "688981", "000001", "300750"):
            self.assertTrue(self.ms.ok_code(c, "测试"), c)

    def test_ok_code_rejects_beijing_exchange(self):
        for c in ("920100", "830799", "870436", "430047"):
            self.assertFalse(self.ms.ok_code(c, "某北交所"), c)

    def test_ok_code_rejects_st_and_delisting(self):
        self.assertFalse(self.ms.ok_code("600519", "ST某某"))
        self.assertFalse(self.ms.ok_code("600519", "*ST某某"))
        self.assertFalse(self.ms.ok_code("600519", "某某退"))
        self.assertFalse(self.ms.ok_code("600519", "某某退市"))

    def test_ok_code_handles_whitespace_and_case(self):
        self.assertFalse(self.ms.ok_code("600519", " * st 某某 "))
        self.assertFalse(self.ms.ok_code("600519", " * sT 某某 "))

    def test_ok_code_rejects_malformed(self):
        for bad in ("", None, "60051", "abcdef", "6005199"):
            self.assertFalse(self.ms.ok_code(bad, "测试"), repr(bad))

    def _scan_db(self, td):
        db = str(Path(td) / "scan.db")
        self.ms.init_db(db)
        return db

    def _put(self, db, day, code, status):
        from contextlib import closing
        with closing(self.ms.connect(db)) as conn, conn:
            conn.execute(
                "INSERT INTO scan_state (scan_date, stock_code, status, n_hits,"
                " note, scanned_at) VALUES (?,?,?,?,?,?)",
                (day, code, status, 0, "", ""))

    def test_failed_code_is_not_done(self):
        """fail 不能算「已完成」。

        回归：全市场那次 243 只 DNS 解析失败被当成已完成，重跑直接跳过 ——
        4.9% 的股票会**永久**缺席，而且没有任何提示。
        """
        with tempfile.TemporaryDirectory() as td:
            db = self._scan_db(td)
            for code, st in (("600000", "ok"), ("600001", "skip"),
                             ("600002", "fail"), ("600003", "ok")):
                self._put(db, "2026-09-21", code, st)
            self.assertEqual(self.ms.done_codes("2026-09-21", db),
                             {"600000", "600001", "600003"})

    def test_done_codes_scoped_to_date(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._scan_db(td)
            self._put(db, "2026-09-21", "600000", "ok")
            self._put(db, "2026-09-18", "600009", "ok")
            self.assertEqual(self.ms.done_codes("2026-09-21", db), {"600000"})


SETTINGS_WITH_NAME = ('pool_name: "仓库样本池"' + chr(10)
                      + 'watchlist:' + chr(10) + '  - "600519"' + chr(10))


class TestRunQueue(unittest.TestCase):
    """长任务队列（B9 / ADR-036）。只测注册表与查找，不真的跑任务。"""

    def setUp(self):
        import run_queue
        self.q = run_queue

    def test_task_ids_unique(self):
        ids = [t['id'] for t in self.q.TASKS]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_every_task_is_runnable(self):
        for t in self.q.TASKS:
            with self.subTest(tid=t['id']):
                self.assertTrue(t['name'])
                self.assertTrue(callable(t['progress']))
                self.assertTrue(callable(t['run']))

    def test_find(self):
        self.assertIsNotNone(self.q.find('scan'))
        self.assertIsNone(self.q.find('no-such-task'))

    def test_survivorship_requires_upstream_done(self):
        """上游没跑满时对比任务不能算「已完成」—— 否则队列会跳过它、留下部分样本的结果。"""
        d, n = self.q.delist_compute_progress()
        sd, _sn = self.q.surv_progress()
        if n > 0 and d < n:
            self.assertEqual(sd, 0)

class TestMultipleComparison(unittest.TestCase):
    """多重比较台账（B5 / ADR-033）的 p 值反推与 BH-FDR。"""

    def setUp(self):
        import verify_multiple
        self.vm = verify_multiple

    def test_p_from_ci_matches_normal(self):
        f = self.vm.p_from_ci
        self.assertAlmostEqual(f(0.0, -0.0196, 0.0196), 1.0, places=6)   # 估计为 0 -> p=1
        self.assertAlmostEqual(f(0.0196, 0.0, 0.0392), 0.05, places=2)   # |z|=1.96 -> p≈.05
        self.assertIsNone(f(None, 0, 1))
        self.assertIsNone(f(1, 1, 1))                                     # 零宽区间

    def test_bh_finds_clear_signal(self):
        items = [{"p": 0.0001}, {"p": 0.001}, {"p": 0.9}, {"p": 0.8}]
        ok = self.vm.bh(items, q=0.05)
        self.assertEqual(sum(1 for x in ok if x["discovery"]), 2)

    def test_bh_rejects_all_when_nothing_significant(self):
        items = [{"p": 0.4}, {"p": 0.6}, {"p": 0.9}]
        ok = self.vm.bh(items, q=0.05)
        self.assertEqual(sum(1 for x in ok if x["discovery"]), 0)

    def test_bh_skips_none_p(self):
        items = [{"p": None}, {"p": 0.0001}]
        ok = self.vm.bh(items, q=0.05)
        self.assertEqual(len(ok), 1)


class TestLimitRoll(unittest.TestCase):
    """涨跌停顺延（B2 / ADR-030）。

    买点遇涨停开盘买不进、卖点遇跌停开盘卖不掉，都要顺延到第一个能成交的交易日。
    价格口径不变 —— 仍是那一天的**开盘价**。
    """

    @staticmethod
    def _bars(opens, closes, highs=None, lows=None):
        """逐日构造 K 线。**下标 0 是"前一日"**，它的收盘价就是下标 1 的昨收。"""
        import pandas as pd
        n = len(opens)
        return pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": opens, "close": closes,
            "high": highs if highs is not None else opens,
            "low": lows if lows is not None else opens,
            "volume": [1.0] * n, "amount": [1.0] * n, "qfq_factor": [1.0] * n,
        })

    def _bt(self):
        import backtest
        return backtest

    def test_buy_blocked_by_limit_up_open(self):
        bt = self._bt()
        b = self._bars([10.0, 11.0], [10.0, 11.0])      # 昨收10 -> 开11 = 涨停
        self.assertTrue(bt.blocked_open(b, 1, 1, 0.10))
        self.assertFalse(bt.blocked_open(b, 1, -1, 0.10))   # 卖点不怕涨停

    def test_sell_blocked_by_limit_down_open(self):
        bt = self._bt()
        b = self._bars([10.0, 9.0], [10.0, 9.0])        # 昨收10 -> 开9 = 跌停
        self.assertTrue(bt.blocked_open(b, 1, -1, 0.10))
        self.assertFalse(bt.blocked_open(b, 1, 1, 0.10))

    def test_normal_open_not_blocked(self):
        bt = self._bt()
        b = self._bars([10.0, 10.5], [10.0, 10.5])
        self.assertFalse(bt.blocked_open(b, 1, 1, 0.10))

    def test_star_board_20pct_not_blocked_at_10pct(self):
        """创业板 ±20% —— 10% 开盘在主板算涨停，在这里不算。"""
        bt = self._bt()
        b = self._bars([10.0, 11.0], [10.0, 11.0])
        self.assertTrue(bt.blocked_open(b, 1, 1, 0.10))
        self.assertFalse(bt.blocked_open(b, 1, 1, 0.20))

    def test_resolve_entry_rolls_forward(self):
        bt = self._bt()
        # 连续两天涨停：10 -> 11 -> 12.1（各自以昨日收盘为基数 +10%），第 4 天才可成交
        b = self._bars([10.0, 11.0, 12.1, 12.0], [10.0, 11.0, 12.1, 12.0])
        j, rolled = bt.resolve_entry(b, 1, 1, 0.10)
        self.assertEqual(rolled, 2)
        self.assertEqual(j, 3)

    def test_resolve_entry_stops_at_prev_close_zero(self):
        """昨收为 0（脏数据）不能死循环。"""
        bt = self._bt()
        b = self._bars([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
        j, rolled = bt.resolve_entry(b, 1, 1, 0.10)
        self.assertEqual(rolled, 0)
        self.assertEqual(j, 1)


class TestBatchQuote(unittest.TestCase):
    """批量行情的解析与「收盘守卫」（ADR-028）。

    收盘守卫是生死线：盘中拿到的 [3]/[33]/[34] 是未完成的当日 bar，
    一旦写进本地就污染 K 线 -> 污染信号与回测。
    """

    @staticmethod
    def _line(dt="20260921120537"):
        """按**字段下标**显式构造一行，避免手写时数错位置。

        实测布局：[1]名称 [2]代码 [3]当前价 [4]昨收 [5]今开
                  [30]时间戳 [33]最高 [34]最低 [36]成交量(手) [37]成交额(万元)
        """
        f = ["1", "贵州茅台", "600519", "1251.57", "1257.12", "1259.00", "15049", "6652"]
        f += ["0"] * (30 - len(f))                      # [8..29] 占位
        f.append(dt)                                    # [30] 时间戳
        f += ["-5.55", "-0.44"]                         # [31][32] 涨跌/涨跌%
        f += ["1259.95", "1250.80"]                     # [33][34] 最高/最低
        f += ["1251.57/15049/188652", "15049", "188652"]  # [35][36][37]
        return 'v_sh600519="' + "~".join(f) + '"'

    @property
    def LINE_OPEN(self):
        return self._line()

    @property
    def LINE_CLOSE(self):
        return self._line("20260921150000")

    def setUp(self):
        import batch_quote
        self.bq = batch_quote

    def test_parse_line_fields(self):
        b = self.bq.parse_line(self.LINE_OPEN)
        self.assertEqual(b["code"], "600519")
        self.assertEqual(b["name"], "贵州茅台")
        self.assertEqual(b["open"], 1259.00)
        self.assertEqual(b["prev_close"], 1257.12)
        self.assertEqual(b["close"], 1251.57)
        self.assertEqual(b["high"], 1259.95)
        self.assertEqual(b["low"], 1250.80)
        self.assertEqual(b["volume"], 15049 * 100)
        self.assertEqual(b["amount"], 188652 * 10000)
        self.assertEqual(b["dt"].strftime("%Y-%m-%d %H:%M:%S"), "2026-09-21 12:05:37")

    def test_parse_line_garbage(self):
        for bad in ("", "no quotes here", 'v_sh600519="1~2~3"', 'v_sh600519="1~贵州茅台~600519"'):
            self.assertIsNone(self.bq.parse_line(bad), bad[:30])

    def test_close_guard(self):
        """盘中一律不算完成 —— 这条错了就会把半根 K 线写进库里。"""
        self.assertFalse(self.bq.is_final(self.bq.parse_line(self.LINE_OPEN)))
        self.assertTrue(self.bq.is_final(self.bq.parse_line(self.LINE_CLOSE)))
        self.assertFalse(self.bq.is_final({"dt": None}))

    def test_close_guard_boundary(self):
        for hhmm, want in (("1459", False), ("1500", True), ("1501", True)):
            b = self.bq.parse_line(self.LINE_OPEN.replace("20260921120537",
                                                          "20260921" + hhmm + "00"))
            self.assertEqual(self.bq.is_final(b), want, hhmm)


class TestPoolName(unittest.TestCase):
    """股票池名称（UI 显示 + ADR 之外的补充功能）。

    重点是那个必须钉住的回归点：**只改代码时不能把用户设的名称冲掉** ——
    watchlist.local.yaml 是整份重写的，早期版本会静默丢名。
    """

    def setUp(self):
        import watchlist_store as ws
        self.ws = ws
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (ws.LOCAL_PATH, ws.SETTINGS_PATH)
        ws.LOCAL_PATH = Path(self._tmp.name) / "watchlist.local.yaml"
        ws.SETTINGS_PATH = Path(self._tmp.name) / "settings.yaml"

    def tearDown(self):
        self.ws.LOCAL_PATH, self.ws.SETTINGS_PATH = self._saved
        self._tmp.cleanup()

    def _settings(self, text):
        self.ws.SETTINGS_PATH.write_text(text, encoding="utf-8")

    def test_normalize_name(self):
        f = self.ws.normalize_name
        self.assertEqual(f("  我的  池 "), "我的 池")
        self.assertEqual(f(""), self.ws.DEFAULT_POOL_NAME)
        self.assertEqual(f(None), self.ws.DEFAULT_POOL_NAME)
        self.assertEqual(f("   "), self.ws.DEFAULT_POOL_NAME)
        self.assertEqual(len(f("x" * 100)), self.ws.MAX_NAME_LEN)

    def test_name_falls_back_to_default(self):
        self.assertEqual(self.ws.load_pool_name(), self.ws.DEFAULT_POOL_NAME)

    def test_name_from_settings(self):
        self._settings(SETTINGS_WITH_NAME)
        self.assertEqual(self.ws.load_pool_name(), "仓库样本池")

    def test_local_name_overrides_settings(self):
        self._settings(SETTINGS_WITH_NAME)
        self.ws.save_watchlist(["600519", "601318"], name="我的池")
        self.assertEqual(self.ws.load_pool_name(), "我的池")

    def test_saving_codes_keeps_existing_name(self):
        """回归点：只改代码时名称必须保留。"""
        self.ws.save_pool_name("我的池")
        self.ws.save_watchlist(["600519", "601318"])
        self.assertEqual(self.ws.load_pool_name(), "我的池")
        self.assertEqual(self.ws.load_watchlist(), ["600519", "601318"])

    def test_save_pool_name_only_changes_name(self):
        self.ws.save_watchlist(["600519"])
        self.ws.save_pool_name("新名字")
        self.assertEqual(self.ws.load_pool_name(), "新名字")
        self.assertEqual(self.ws.load_watchlist(), ["600519"])

    def test_reset_restores_settings_name(self):
        self._settings(SETTINGS_WITH_NAME)
        self.ws.save_pool_name("临时名字")
        self.assertEqual(self.ws.load_pool_name(), "临时名字")
        self.ws.reset_watchlist()
        self.assertEqual(self.ws.load_pool_name(), "仓库样本池")


class TestSignalScore(unittest.TestCase):
    """排序打分（ADR-023/024）。它进了报告、UI 和 LLM 上下文，门槛写错会误导用户。"""

    def setUp(self):
        import signal_score
        self.ss = signal_score

    def test_two_flags(self):
        f = self.ss.score_of
        self.assertEqual(f(0.20, 2), 2)
        self.assertEqual(f(0.20, 12), 1)
        self.assertEqual(f(0.05, 2), 1)
        self.assertEqual(f(0.05, 12), 0)

    def test_thresholds_are_inclusive(self):
        f = self.ss.score_of
        self.assertEqual(f(self.ss.W_THR, 0), 2)          # 宽度恰好等于阈值 -> 得分
        self.assertEqual(f(0.20, self.ss.G_THR), 1)       # 距中枢恰好等于阈值 -> 不得分

    def test_missing_factor_contributes_zero(self):
        """**缺数据的那个因子**记 0 分 —— 不能因为「不知道」就给正面评价。

        注意是「每个因子」不是「总分」：宽度未知、但距中枢确实很近时，
        距中枢这一分照样要拿（(None, 3) -> 1）。
        """
        f = self.ss.score_of
        self.assertEqual(f(None, 3), 1, "宽度未知只丢宽度那一分")
        self.assertEqual(f(0.2, None), 1, "距中枢未知只丢距中枢那一分")
        self.assertEqual(f(None, None), 0, "两个都未知 -> 0")
        self.assertEqual(f("x", 3), 1, "脏数据同未知")
        self.assertEqual(f(0.2, "x"), 1, "脏数据同未知")

    def test_negative_gap_does_not_score(self):
        """-1 是「无中枢可参照」的哨兵值，不能当成「离得很近」。"""
        self.assertEqual(self.ss.score_of(0.2, -1), 1)

    def test_labels(self):
        self.assertEqual(self.ss.label_of(2), "★★")
        self.assertEqual(self.ss.label_of(1), "★")
        self.assertEqual(self.ss.label_of(0), "—")

    def test_annotate_sets_score(self):
        import structure_gap
        sigs = [{"date": "2026-01-05", "type": "第一类买点"},
                {"date": "2026-01-06", "type": "第一类买点"}]
        structure_gap.annotate(sigs, {("2026-01-05", "第一类买点"): {"gap": 2, "width": 0.2},
                                      ("2026-01-06", "第一类买点"): {"gap": 20, "width": 0.01}})
        self.assertEqual(sigs[0]["zs_score"], 2)
        self.assertEqual(sigs[1]["zs_score"], 0)


class TestAppBoots(unittest.TestCase):
    """app.py 能渲染出首屏（不联网、不点分析）。

    这层是 CI 之前完全没有覆盖的：compileall 只查语法，查不出运行期的渲染错误
    （比如 metric_cards 传错元组长度、f-string 里引用了不存在的变量）。
    """

    def test_first_screen_renders_without_exception(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(Path(ROOT) / "app.py"))
        at.run(timeout=120)
        self.assertEqual([str(e.value) for e in at.exception], [])
        self.assertEqual([r.label for r in at.sidebar.radio], ["模式"])
        self.assertIn("单股分析", at.sidebar.radio[0].options)

    def test_rerun_with_cached_result(self):
        """已经有结果时页面重跑，不能依赖 run_btn 分支里的局部变量。

        回归：stock_score_history(code) 里的 code 原先只在「开始分析」分支里赋值，
        于是任何别的重跑都会 NameError（用户是在点「生成 AI 总结」时撞上的）。
        """
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(Path(ROOT) / "app.py"))
        at.session_state["res"] = {
            "ok": True, "code": EMPTY_CODE, "name": "测试股",
            "kline": {}, "structure": {}, "signals": [], "backtest": [],
            "fundamentals": {}, "llm": {},
        }
        at.run(timeout=120)
        self.assertEqual([str(e.value) for e in at.exception], [])
        # 能拿到这个按钮，说明渲染确实走到了出问题的那一行之后
        self.assertIn("生成 AI 总结", [b.label for b in at.button])


class TestSkin(unittest.TestCase):
    """双皮肤：令牌完整性 + 图表配色（离线）。"""

    def test_css_has_no_undefined_vars(self):
        css = skin.build_css()
        used = set(re.findall(r"var\\((--[a-z0-9-]+)\\)", css))
        declared = set(re.findall(r"(--[a-z0-9-]+):", css))
        self.assertEqual(sorted(used - declared), [], "CSS 引用了未声明的变量")

    def test_both_palettes_emitted(self):
        css = skin.build_css()
        for key in ("bg", "card", "txt", "accent"):
            self.assertIn(f"--{key}:{skin.DARK[key]}", css)
            self.assertIn(f"--{key}:{skin.LIGHT[key]}", css)

    def test_night_toggle_wired(self):
        css = skin.build_css()
        self.assertIn("input:checked", css)
        self.assertIn(".st-key-chx_skin", css)
        self.assertIn("prefers-color-scheme: light", css)

    def test_chart_colors_differ(self):
        night, day = skin.chart_colors(True), skin.chart_colors(False)
        self.assertNotEqual(night["up"], day["up"])
        self.assertNotEqual(night["bi"], day["bi"])
        keys = {"up", "down", "bi", "zs", "buy", "sell", "grid", "axis", "line", "ring"}
        for c in (night, day):
            self.assertEqual(set(c), keys)

class TestSignalFeatures(unittest.TestCase):
    """背驰强度特征（ADR-037）：MACD 柱与「笔内面积」。"""

    def setUp(self):
        import signal_features
        self.sf = signal_features

    def _series(self, n=400, seed=7):
        import numpy as np
        rs = np.random.default_rng(seed)
        px = 100 * np.cumprod(1 + rs.normal(0, 0.02, n))
        return pd.DataFrame({
            "date": pd.bdate_range("2020-01-01", periods=n),
            "close": px,
        })

    def test_macd_hist_matches_manual(self):
        q = self._series()
        c = q["close"]
        dif = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        dea = dif.ewm(span=9, adjust=False).mean()
        got = self.sf.macd_hist(q)
        self.assertEqual(len(got), len(q))
        self.assertAlmostEqual(float((got - (dif - dea)).abs().max()), 0.0, places=9)

    def test_prefix_equals_full_no_warmup(self):
        """adjust=False 的递归从首值出发 -> 前缀重算与全序列逐点相同。

        这是「特征只用确认日前缀」却不引入预热误差的依据；若这条挂了，
        面积比就含有起始点依赖，得改成固定热身期。
        """
        q = self._series(seed=11)
        full = self.sf.macd_hist(q)
        for k in (30, 120, 399):
            pre = self.sf.macd_hist(q.iloc[:k])
            self.assertAlmostEqual(
                float((pre - full.iloc[:k]).abs().max()), 0.0, places=9, msg="k=%d" % k)

    def test_bi_area_sums_abs_hist_inclusive(self):
        q = self._series(seed=3)
        hist = self.sf.macd_hist(q)
        b = type("B", (), {})()
        b.sdt = q["date"].iloc[10]
        b.edt = q["date"].iloc[20]
        want = float(hist.iloc[10:21].abs().sum())
        self.assertAlmostEqual(self.sf.bi_area(q, hist, b), want, places=9)

    def test_bi_area_ignores_bars_outside(self):
        """换掉区间外的柱值，面积不应变化（防止把整段前缀都算进去）。"""
        q = self._series(seed=5)
        hist = self.sf.macd_hist(q)
        b = type("B", (), {})()
        b.sdt = q["date"].iloc[200]
        b.edt = q["date"].iloc[210]
        base = self.sf.bi_area(q, hist, b)
        dirty = hist.copy()
        dirty.iloc[:150] = 999.0
        self.assertAlmostEqual(self.sf.bi_area(q, dirty, b), base, places=9)

    def test_bi_area_none_on_unknown_dates(self):
        q = self._series(seed=2)
        hist = self.sf.macd_hist(q)
        b = type("B", (), {})()
        b.sdt = pd.Timestamp("1999-01-01")
        b.edt = pd.Timestamp("1999-02-01")
        self.assertIsNone(self.sf.bi_area(q, hist, b))


class TestIndustry(unittest.TestCase):
    """行业映射与集中度（离线，不联网）。"""

    def setUp(self):
        import industry
        self.im = industry

    def test_split_pads_and_strips(self):
        self.assertEqual(self.im.split("信息技术 / 半导体 / 集成电路 / 集成电路制造"),
                         ("信息技术", "半导体", "集成电路", "集成电路制造"))
        self.assertEqual(self.im.split("金融/银行"), ("金融", "银行", "", ""))
        self.assertEqual(self.im.split(""), ("", "", "", ""))
        self.assertEqual(self.im.split(None), ("", "", "", ""))

    def test_level_of_uses_given_mapping(self):
        m = {"600519": "主要消费 / 食品、饮料与烟草 / 酒 / 白酒"}
        self.assertEqual(self.im.level_of("600519", "门类", m), "主要消费")
        self.assertEqual(self.im.level_of("600519", "次类", m), "食品、饮料与烟草")
        self.assertEqual(self.im.level_of("600519", "中类", m), "白酒")
        self.assertEqual(self.im.level_of("999999", "门类", m), "")

    def test_code_normalization(self):
        m = {"000001": "金融 / 银行 / 商业银行 / 综合性银行"}
        self.assertEqual(self.im.level_of("000001.SZ", "门类", m), "金融")
        self.assertEqual(self.im.level_of("1", "门类", {"000001": "金融"}), "金融")

    def test_concentration_lift(self):
        # 甲 6 只 / 乙 2 只；命中各 1 只 -> 甲 lift 0.67x，乙 lift 2.00x
        m = {c: "甲 / 一 / x / y" for c in
             ("111111", "111112", "111113", "111114", "111115", "111116")}
        m.update({c: "乙 / 三 / x / y" for c in ("222221", "222222")})
        res = self.im.concentration(["111111", "222221"], list(m), level="门类",
                                    mapping=m, min_hits=1)
        by = {x["industry"]: x for x in res}
        self.assertAlmostEqual(by["甲"]["hits_share"], 0.5)
        self.assertAlmostEqual(by["甲"]["base_share"], 6 / 8)
        self.assertAlmostEqual(by["甲"]["lift"], 0.5 / (6 / 8))
        self.assertAlmostEqual(by["乙"]["lift"], 0.5 / (2 / 8))
        self.assertEqual(res[0]["industry"], "乙")          # 按 lift 降序

    def test_concentration_ignores_unknown_and_respects_min_hits(self):
        m = {"111111": "甲 / 一 / x / y"}
        self.assertEqual(self.im.concentration(["999999"], ["111111"],
                                              mapping=m, min_hits=1), [])
        m2 = {"111111": "甲 / 一 / x / y", "222221": "乙 / 三 / x / y"}
        self.assertEqual(self.im.concentration(["111111"], ["111111", "222221"],
                                              mapping=m2, min_hits=2), [])

    def test_build_failure_semantics(self):
        """抛异常（网络）-> 不记录，下次重试；返回空串（源里没有）-> 记录为终局。

        这条是 ADR-022 那次的教训：**把网络失败当成「查过了」，一次抖动就永久漏数据**。
        """
        with tempfile.TemporaryDirectory() as td:
            saved = (self.im.CACHE, self.im.fetch_one)
            self.im.CACHE = Path(td) / "ind.json"
            try:
                calls = []

                def fake(code):
                    calls.append(code)
                    if code == "999002":
                        raise ConnectionError("测试：网络断了")
                    return "甲 / 一 / x / y" if code == "999001" else ""

                self.im.fetch_one = fake
                codes = ["999001", "999002", "999003"]   # 用不存在的代码，避开真实 profiles 种子
                m = self.im.build(codes, verbose=False, progress=99)
                self.assertEqual(m.get("999001"), "甲 / 一 / x / y")
                self.assertEqual(m.get("999003"), "")        # 源里没有 -> 终局
                self.assertNotIn("999002", m)                # 网络失败 -> 不记录
                # 再跑一次：已记录的 1/3 不再请求，只有失败的 2 重试
                calls.clear()
                self.im.build(codes, verbose=False, progress=99)
                self.assertEqual(calls, ["999002"])
            finally:
                self.im.CACHE, self.im.fetch_one = saved

    def test_concentration_rows_degrades_without_map(self):
        """没有行业数据时要静默返回空，不能把扫描页搞崩。"""
        import market_scan as ms
        saved = self.im.load_map
        self.im.load_map = lambda: {}
        try:
            self.assertEqual(ms.concentration_rows("2026-09-21"), [])
        finally:
            self.im.load_map = saved

class TestDataSource(unittest.TestCase):
    """数据源抽象（ADR-039）。"""

    def setUp(self):
        import datasource
        self.ds = datasource
        self.ds.reset()

    def tearDown(self):
        # reset 清空了注册表，必须 force 才能把 providers/ 装回来
        self.ds.discover(force=True)
        self.ds._ACTIVE = None

    def _frame(self, n=3):
        import pandas as pd
        return pd.DataFrame({
            "date": pd.bdate_range("2024-01-02", periods=n),   # n 大了要真日期序列
            "open": [1.0 + i for i in range(n)], "high": [2.0 + i for i in range(n)],
            "low": [0.5 + i for i in range(n)], "close": [1.5 + i for i in range(n)],
            "volume": [100.0 + i for i in range(n)],
            "amount": [1e6 + i for i in range(n)]})

    def test_bare_source_raises_not_supported(self):
        class Bare(self.ds.DataSource):
            name = "bare"
        b = Bare()
        self.assertFalse(b.supports("kline"))
        with self.assertRaises(self.ds.NotSupported):
            b.kline("600519", "2024-01-01", "2024-12-31")
        # 没因子不是错误，是「这个源没有」
        self.assertIsNone(b.factor("600519", None, None))

    def test_register_get_and_available(self):
        class One(self.ds.DataSource):
            name = "one"
            provides = ("kline",)
        self.ds.register(One())
        self.assertEqual(self.ds.get("one").name, "one")
        self.assertIn("one", self.ds.available())
        self.assertTrue(self.ds.get("one").supports("kline"))

    def test_get_unknown_raises_with_list(self):
        self.ds.register(type("X", (self.ds.DataSource,), {"name": "x"})())
        with self.assertRaises(KeyError) as cm:
            self.ds.get("不存在")
        self.assertIn("x", str(cm.exception))

    def test_providers_discovered(self):
        self.assertIn("builtin", self.ds.available())
        self.assertEqual(self.ds.load_errors(), [])

    def test_normalize_kline_sorts_dedups_and_casts(self):
        import pandas as pd
        df = self._frame(3)
        df = pd.concat([df, df.iloc[[0]]], ignore_index=True)     # 重复
        df = df.iloc[::-1]                                        # 倒序
        out = self.ds.normalize_kline(df)
        self.assertEqual(len(out), 3)
        self.assertTrue(out["date"].is_monotonic_increasing)
        self.assertEqual(out["close"].dtype, float)
        self.assertEqual(list(out.columns), self.ds.RAW_COLUMNS)

    def test_normalize_kline_empty_keeps_columns(self):
        out = self.ds.normalize_kline(None)
        self.assertEqual(len(out), 0)
        self.assertEqual(list(out.columns), self.ds.RAW_COLUMNS)

    def test_normalize_factor_drops_bad_rows(self):
        import pandas as pd
        df = pd.DataFrame({"date": ["2024-01-02", "2024-01-03"],
                           "qfq_factor": ["1.0", "abc"]})     # akshare 的因子是字符串
        out = self.ds.normalize_factor(df)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out["qfq_factor"].iloc[0]), 1.0)

    def test_fetch_raw_goes_through_active_source(self):
        """接线点：storage_kline.fetch_raw 必须走活动数据源，不能还硬编码腾讯。"""
        import pandas as pd

        import storage_kline as sk

        frame = self._frame(2)

        class Fake(self.ds.DataSource):
            name = "fake"
            provides = ("kline",)

            def kline(self, code, start, end):
                return frame

        self.ds.register(Fake())
        self.ds.use("fake")
        out = sk.fetch_raw("600519", pd.Timestamp("2024-01-01"),
                           pd.Timestamp("2024-01-05"))
        self.assertEqual(len(out), 2)
        self.assertEqual(list(out.columns), self.ds.RAW_COLUMNS)

    def test_noisy_source_is_deterministic_and_keeps_ohlc_sane(self):
        import verify_datasource as vd

        class Flat(self.ds.DataSource):
            name = "flat"
            provides = ("kline",)

            def kline(self, code, start, end):
                return self._f

        s = Flat()
        s._f = self._frame(50)
        n = vd.NoisySource(s, sigma=0.001, seed=7)
        a = n.kline("600519", "2024-01-01", "2024-12-31")
        b = n.kline("600519", "2024-01-01", "2024-12-31")
        self.assertTrue(a.equals(b), "同一只股票两次结果必须一样")
        self.assertFalse(a["close"].equals(s._f["close"]), "噪声没生效")
        self.assertTrue((a["high"] >= a[["open", "close", "low"]].max(axis=1) - 1e-9).all())
        self.assertTrue((a["low"] <= a[["open", "close", "high"]].min(axis=1) + 1e-9).all())

    def test_contract_check_catches_violations(self):
        import verify_datasource as vd
        good = self._frame(3)
        self.assertEqual(vd.contract_check(good), [])
        self.assertIn("None", vd.contract_check(None)[0])
        bad = good.drop(columns=["amount"])
        self.assertIn("缺列", vd.contract_check(bad)[0])
        rev = good.iloc[::-1].reset_index(drop=True)
        self.assertTrue(any("升序" in x for x in vd.contract_check(rev)))
        dup = pd.concat([good, good.iloc[[0]]], ignore_index=True)
        self.assertTrue(any("重复" in x for x in vd.contract_check(dup)))
        broken = good.copy()
        broken.loc[0, "high"] = broken.loc[0, "low"] - 1
        self.assertTrue(any("high" in x for x in vd.contract_check(broken)))
