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

import analyzer
import signal_filter
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
