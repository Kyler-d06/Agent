import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from trading_tools import ISHARES_OEF_HOLDINGS, TRADING_TOOLS, TradingTools


class TradingToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tools = TradingTools(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_has_no_order_placement_tool(self):
        names = {entry["name"] for entry in TRADING_TOOLS}
        self.assertEqual(names, {"trading_data_status", "collect_sp100_stock_bars", "backtest_stock_edges",
                                 "collect_sp100_options", "collect_kalshi_markets", "kalshi_paper_status",
                                 "start_kalshi_paper", "record_kalshi_paper_fill", "reconcile_kalshi_paper",
                                 "search_stock_signals"})
        self.assertEqual(next(x for x in TRADING_TOOLS if x["name"] == "trading_data_status")["effect"], "read")
        self.assertTrue(all("order" not in name for name in names))

    def test_stock_backtest_writes_reproducible_obsidian_report(self):
        vault = self.root / "vault"
        vault.mkdir()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = []
        price = 100.0
        for i in range(320):
            price *= 1.002 if i % 25 else 0.97
            bars.append({"t": (start + timedelta(days=i)).isoformat().replace("+00:00", "Z"),
                         "o": price * 0.999, "h": price * 1.01, "l": price * 0.99,
                         "c": price, "v": 1_000_000})
        source = self.root / "trading_data/stock.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"dataset": "sp100_stocks", "symbols": [
            {"symbol": "AAA", "bars": bars}, {"symbol": "BBB", "bars": bars}
        ]}), encoding="utf-8")
        with patch.dict(os.environ, {"OBSIDIAN_VAULT": str(vault)}, clear=False):
            value = self.tools.backtest_stock_edges("trading_data/stock.json", minimum_test_trades=5)
        report = Path(value["obsidian_report"]["path"])
        self.assertTrue(report.is_file())
        self.assertIn("Untouched holdout", report.read_text(encoding="utf-8"))
        result = json.loads((self.root / value["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["source_sha256"], __import__("hashlib").sha256(source.read_bytes()).hexdigest())
        self.assertIn(result["verdict"], {"NO VALIDATED EDGE", "PROMISING — requires independent confirmation"})

    def test_universe_parser_rejects_truncated_holdings(self):
        self.tools._request = lambda url: b'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency\n"AAPL","Apple","IT","Equity",1,1,1,1,1,"United States","NASDAQ","USD"\n'
        with self.assertRaisesRegex(RuntimeError, "expected equity universe"):
            self.tools._sp100_universe()

    def test_stock_collection_filters_and_labels_delayed_sip(self):
        universe = [{"symbol": f"T{i}", "name": f"Test {i}", "sector": "IT", "weight_pct": "1"}
                    for i in range(100)]
        bars = {}
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for symbol in ("T0", "T1", "T2", "T3", "T4", "T5"):
            rows = []
            for i in range(150):
                close = 100 + i + (8 if i % 2 else -8)
                rows.append({"t": (start + timedelta(days=i)).isoformat().replace("+00:00", "Z"),
                             "o": close - 1, "h": close + 2, "l": close - 2, "c": close, "v": 1_000_000})
            bars[symbol] = rows
        self.tools._sp100_universe = lambda: universe
        self.tools._paged_stock_bars = lambda symbols, timeframe, start, end, feed: (bars, 1, False)
        value = self.tools.collect_sp100_stock_bars(lookback_days=180, max_symbols=5,
            min_realized_vol=0.05, min_dollar_volume=1, output="trading_data/test/stocks.json")
        doc = json.loads((self.root / "trading_data/test/stocks.json").read_text())
        self.assertEqual(len(doc["symbols"]), 5)
        self.assertEqual(doc["data_quality"]["delayed_minutes"], 20)
        self.assertEqual(doc["parameters"]["feed"], "sip")
        self.assertEqual(value["summary"]["dataset"], "sp100_stocks")

    def test_hourly_collection_batches_symbols_to_prevent_pagination_starvation(self):
        universe = [{"symbol": f"T{i}", "name": f"Test {i}", "sector": "IT", "weight_pct": "1"}
                    for i in range(100)]
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        daily = {f"T{i}": [{"t": (start + timedelta(days=j)).isoformat(), "c": 100 + j + (10 if j % 2 else 0), "v": 1_000_000}
                             for j in range(100)] for i in range(6)}
        calls = []

        def paged(symbols, timeframe, start, end, feed, batch_size=None):
            calls.append((list(symbols), timeframe, batch_size))
            if timeframe == "1Day":
                return daily, 1, False
            return ({symbol: [{"t": "2026-01-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
                    for symbol in symbols}, len(symbols), False)

        self.tools._sp100_universe = lambda: universe
        self.tools._paged_stock_bars = paged
        self.tools.collect_sp100_stock_bars(timeframe="1Hour", lookback_days=180, max_symbols=5,
            min_realized_vol=0.05, min_dollar_volume=1, output="trading_data/test/hourly.json")
        assert calls[-1][2] == 1

    def test_backtest_status_exposes_display_metrics(self):
        path = self.root / "trading_data/stock_backtests/result.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        path.with_suffix(".json.meta").write_text(json.dumps({
            "dataset": "stock_edge_backtest", "record_count": 4,
            "backtest": {"verdict": "NO VALIDATED EDGE", "selected_strategy": "momentum_20d",
                         "holdout": {"trades": 12, "expectancy_bps": -2.5}},
        }), encoding="utf-8")
        row = self.tools.trading_data_status()["datasets"][0]
        self.assertEqual(row["backtest"]["holdout"]["trades"], 12)

    def test_stock_backtest_refuses_truncated_collection(self):
        source = self.root / "trading_data/truncated.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"dataset": "sp100_stocks",
                                      "data_quality": {"pagination_truncated": True}, "symbols": []}))
        with self.assertRaisesRegex(ValueError, "pagination was truncated"):
            self.tools.backtest_stock_edges("trading_data/truncated.json")

    def test_kalshi_collection_filters_kind_and_records_provenance(self):
        def fake(base, path, query=None, headers=None):
            if path == "/markets":
                return {"markets": [{"ticker": "KXBTC15M-TEST", "title": "BTC price up in next 15 mins?"}]}
            if path == "/events":
                return {"events": [
                    {"event_ticker": "KXBTC15M-EVENT", "series_ticker": "KXBTC15M", "title": "Bitcoin price in 15 minutes", "markets": [{"ticker": "KXBTC15M-TEST"}]},
                    {"event_ticker": "OTHER-EVENT", "series_ticker": "OTHER", "title": "Unrelated election", "markets": [{"ticker": "OTHER"}]},
                ]}
            if path.endswith("/orderbook"):
                return {"orderbook_fp": {"yes_dollars": [["0.5000", "3.00"]], "no_dollars": []}}
            if path.endswith("/candlesticks"):
                return {"candlesticks": [{"end_period_ts": 1, "price": {"close": 50}}]}
            raise AssertionError(path)

        self.tools._request_json = fake
        value = self.tools.collect_kalshi_markets("bitcoin_15m", output="trading_data/test/kalshi.json")
        doc = json.loads((self.root / "trading_data/test/kalshi.json").read_text())
        self.assertEqual(doc["record_count"], 1)
        self.assertEqual(doc["markets"][0]["market"]["ticker"], "KXBTC15M-TEST")
        self.assertEqual(doc["markets"][0]["orderbook_format"], "fixed_point_dollars")
        self.assertIn("external-api.kalshi.com", doc["provenance"]["api_base"])
        status = self.tools.trading_data_status()
        self.assertFalse(status["live_trading_enabled"])
        self.assertEqual(status["datasets"][0]["markets"][0]["ticker"], "KXBTC15M-TEST")
        self.assertEqual(value["summary"]["record_count"], 1)

    def test_weather_filter_does_not_confuse_ukraine_hurricanes_or_lowes(self):
        self.assertFalse(self.tools._kalshi_match({"title": "Will Ukraine qualify?", "category": "Sports"}, "weather"))
        self.assertFalse(self.tools._kalshi_match({"title": "Carolina Hurricanes win?", "category": "Sports"}, "weather"))
        self.assertFalse(self.tools._kalshi_match({"ticker": "KXLOW-TEST", "title": "Lowe's comparable sales", "category": "Financials"}, "weather"))
        self.assertTrue(self.tools._kalshi_match({"ticker": "KXHIGHNY-TEST", "title": "High in NYC", "category": "Climate and Weather"}, "weather"))

    def test_kalshi_paper_uses_virtual_budget_and_official_settlement(self):
        vault = self.root / "vault"
        vault.mkdir()
        with patch.dict(os.environ, {"OBSIDIAN_VAULT": str(vault)}, clear=False):
            self.tools.start_kalshi_paper(10)
            market = {"ticker": "KXBTC15M-TEST", "status": "open", "yes_ask_dollars": "0.25"}
            self.tools._request_json = lambda *_args, **_kwargs: {"market": market}
            opened = self.tools.record_kalshi_paper_fill(
                "KXBTC15M-TEST", "yes", 2.0, "outside model requested a bounded test", "claude-mcp"
            )
            self.assertFalse(opened["live_order_placed"])
            self.assertEqual(opened["position"]["contracts"], 7)
            self.assertAlmostEqual(opened["cash_usd"], 8.11)
            market.update({"status": "settled", "result": "yes"})
            settled = self.tools.reconcile_kalshi_paper()
        self.assertEqual(settled["settled"][0]["official_result"], "yes")
        self.assertAlmostEqual(settled["cash_usd"], 15.11)
        self.assertAlmostEqual(settled["realized_pnl_usd"], 5.11)
        self.assertFalse(settled["live_order_placed"])
        self.assertEqual(self.tools.kalshi_paper_status()["cash_usd"], 15.11)
        self.assertIn("KXBTC15M-TEST", (vault / "Trading Research/Kalshi Paper Experiment.md").read_text())

    def test_options_collection_screens_volatility_liquidity_and_marks_indicative(self):
        universe = [{"symbol": "TEST", "name": "Test", "sector": "IT", "weight_pct": "1"}] * 100
        # Deduplicate is not part of collection; provide a realistic unique first symbol
        universe = [{**row, "symbol": f"T{i}"} for i, row in enumerate(universe)]
        bars = [{"c": 100 + (i % 2) * 8 + i, "t": f"2026-01-{i + 1:02d}T00:00:00Z"} for i in range(20)]

        def fake(base, path, query=None, headers=None):
            if path == "/v2/stocks/bars":
                return {"bars": {"T0": bars}}
            if path == "/v1beta1/options/snapshots/T0":
                return {"snapshots": {
                    "T0OPTGOOD": {"latestQuote": {"bp": 1.0, "ap": 1.1}, "dailyBar": {"v": 10}, "impliedVolatility": 0.4},
                    "T0OPTWIDE": {"latestQuote": {"bp": 1.0, "ap": 3.0}, "dailyBar": {"v": 10}},
                    "T0OPTEMPTY": {"latestQuote": {"bp": 1.0, "ap": 1.1}, "dailyBar": {"v": 0}},
                }}
            raise AssertionError(path)

        self.tools._sp100_universe = lambda: universe
        self.tools._request_json = fake
        with patch.dict(os.environ, {"APCA_API_KEY_ID": "test-key", "APCA_API_SECRET_KEY": "test-secret"}, clear=False):
            value = self.tools.collect_sp100_options(max_underlyings=1, min_realized_vol=0.05,
                output="trading_data/test/options.json")
        doc = json.loads((self.root / "trading_data/test/options.json").read_text())
        self.assertEqual(doc["record_count"], 1)
        self.assertEqual(doc["underlyings"][0]["options"][0]["symbol"], "T0OPTGOOD")
        self.assertTrue(any("delayed/modified" in item for item in doc["warnings"]))
        self.assertNotIn("test-secret", json.dumps(doc))
        self.assertEqual(value["summary"]["record_count"], 1)

    def test_output_must_be_confined_json_and_no_clobber(self):
        with self.assertRaises(ValueError):
            self.tools._output_path("../escape.json", "x")
        with self.assertRaises(ValueError):
            self.tools._output_path("trading_data/data.csv", "x")
        path = self.root / "trading_data/existing.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        with self.assertRaises(FileExistsError):
            self.tools._output_path("trading_data/existing.json", "x")


if __name__ == "__main__":
    unittest.main()
