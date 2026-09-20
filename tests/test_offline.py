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
