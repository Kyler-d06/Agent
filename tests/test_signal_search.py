import hashlib
import json
import math
import os
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signal_search as ss  # noqa: E402
import trading_tools  # noqa: E402
from trading_tools import TRADING_TOOLS, TradingTools  # noqa: E402


def make_document(n_symbols=6, n_bars=520, seed=7, drift=0.0006, screened=True):
    rng = random.Random(seed)
    start = date(2023, 1, 2)
    symbols = []
    for k in range(n_symbols):
        price, bars = 100.0 + 10 * k, []
        for i in range(n_bars):
            o = price * (1 + rng.gauss(0, 0.004))
            c = o * (1 + rng.gauss(drift, 0.017))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
            bars.append({"t": (start + timedelta(days=i)).isoformat() + "T05:00:00Z",
                         "o": o, "h": h, "l": l, "c": c, "v": 1_000_000})
            price = c
        symbols.append({"symbol": f"S{k}", "bars": bars})
    params = {"timeframe": "1Day"}
    if screened:
        params.update({"min_realized_vol": 0.2, "min_dollar_volume": 5e7})
    return {"dataset": "sp100_stocks", "parameters": params,
            "data_quality": {"feed": "sip", "pagination_truncated": False}, "symbols": symbols}


def flat_symbol(n=140, atr=1.0):
    return {"symbol": "F", "dates": [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)],
            "o": [100.0] * n, "h": [100.5] * n, "l": [99.5] * n, "c": [100.0] * n, "atr": [atr] * n}


def bars_sym(rows, atr):
    """rows: list of (o, h, l, c)."""
    return {"o": [r[0] for r in rows], "h": [r[1] for r in rows], "l": [r[2] for r in rows],
            "c": [r[3] for r in rows], "atr": list(atr)}


class GridTests(unittest.TestCase):
    def test_grid_is_bounded_and_matches_the_requested_settings(self):
        self.assertEqual(ss.ATR_MULTIPLES, (1.5, 2.0, 2.5, 3.0))
        self.assertEqual(ss.PCT_STOPS, (3.0, 5.0, 7.0, 10.0))
        self.assertEqual(ss.SIGNALS, ("momentum_20d", "breakout_20d", "trend_pullback", "mean_reversion_uptrend"))
        grid = ss.build_grid()
        self.assertEqual(len(grid), 4 * 8 * 3)
        self.assertEqual(len({c["key"] for c in grid}), len(grid))
        self.assertTrue(all(c["signal"] in ss.SIGNALS for c in grid))
        self.assertAlmostEqual(ss.multiple_testing_hurdle(96), math.sqrt(2 * math.log(96)))

    def test_neighbors_step_one_setting_along_each_axis(self):
        cfg = ss.make_config("momentum_20d", "atr", 2.0, 20)
        near = ss.neighbor_configs(cfg)
        self.assertEqual({c["stop_value"] for c in near["stop"]}, {1.5, 2.5})
        self.assertEqual({c["max_hold_bars"] for c in near["hold"]}, {10, None})
        edge = ss.neighbor_configs(ss.make_config("breakout_20d", "pct", 3.0, 10))
        self.assertEqual([c["stop_value"] for c in edge["stop"]], [5.0])
        self.assertEqual([c["max_hold_bars"] for c in edge["hold"]], [20])
        self.assertTrue(all(c["stop_kind"] == "pct" for c in edge["stop"]))


class SignalTests(unittest.TestCase):
    def rows(self, seed=3, n=600):
        doc = make_document(n_symbols=1, n_bars=n, seed=seed, drift=0.001)
        return doc["symbols"][0]["bars"]

    def test_signals_match_existing_backtester_entries(self):
        for seed in (3, 5, 9):
            bars = self.rows(seed)
            h, c = [b["h"] for b in bars], [b["c"] for b in bars]
            flags = ss.signal_flags(h, c)
            index = {b["t"]: i for i, b in enumerate(bars)}
            for name in ss.SIGNALS:
                existing = TradingTools._strategy_trades(bars, name, 0)
                for trade in existing:
                    i = index[trade["entry_time"]] - 1
                    self.assertTrue(flags[name][i], f"{name} flag missing at {i} (seed {seed})")
                if existing:
                    first = next(i for i in range(ss.WARMUP, len(c)) if flags[name][i])
                    self.assertEqual(index[existing[0]["entry_time"]], first + 1)
        seen = {name: 0 for name in ss.SIGNALS}
        for seed in (3, 5, 9):
            bars = self.rows(seed)
            for name in ss.SIGNALS:
                seen[name] += len(TradingTools._strategy_trades(bars, name, 0))
        self.assertTrue(all(count > 0 for count in seen.values()), seen)

    def test_signals_and_atr_do_not_look_ahead(self):
        bars = self.rows(4)
        h, l, c = [b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars]
        full, m = ss.signal_flags(h, c), 350
        part = ss.signal_flags(h[:m], c[:m])
        for name in full:
            self.assertEqual(full[name][:m - 1], part[name][:m - 1], name)
        self.assertEqual(ss.atr_series(h, l, c)[:m], ss.atr_series(h[:m], l[:m], c[:m]))

    def test_atr_wilder_known_values(self):
        n = 40
        c = [100.0] * n
        atr = ss.atr_series([101.0] * n, [99.0] * n, c)
        self.assertTrue(all(v is None for v in atr[:14]))
        self.assertTrue(all(abs(v - 2.0) < 1e-12 for v in atr[14:]))
        gap = ss.atr_series([101.0] * 20 + [111.0] * 20, [99.0] * 20 + [109.0] * 20, [100.0] * 20 + [110.0] * 20)
        self.assertGreater(gap[20], gap[19])  # the gap enters true range


class StopMechanicsTests(unittest.TestCase):
    def test_initial_protective_stop_fills_at_the_stop(self):
        s = bars_sym([(100, 100, 100, 100), (100, 101, 95, 97), (97, 98, 96, 97)], [2, 2, 2])
        self.assertEqual(ss.run_trade(s, 1, 2, "atr", 2.0, None), (1, 96.0, "stop"))

    def test_gap_through_stop_fills_at_the_worse_open(self):
        s = bars_sym([(100, 100, 100, 100), (100, 102, 99, 101), (90, 92, 88, 91)], [2, 2, 2])
        self.assertEqual(ss.run_trade(s, 1, 2, "atr", 2.0, None), (2, 90.0, "stop_gap"))

    def test_percentage_trailing_stop_ratchets_up_from_the_highest_high(self):
        s = bars_sym([(100, 100, 100, 100), (100, 110, 99, 108), (108, 109, 104, 105)], [2, 2, 2])
        x, fill, reason = ss.run_trade(s, 1, 2, "pct", 5.0, None)
        self.assertEqual((x, reason), (2, "stop"))
        self.assertAlmostEqual(fill, 104.5)  # 110 * 0.95, not the initial 95

    def test_same_bar_high_cannot_raise_the_stop_before_the_low_is_tested(self):
        s = bars_sym([(100, 100, 100, 100), (100, 130, 94, 120), (120, 121, 119, 120)], [2, 2, 2])
        self.assertEqual(ss.run_trade(s, 1, 2, "pct", 5.0, None), (1, 95.0, "stop"))

    def test_stop_never_moves_down_when_atr_expands(self):
        rows = [(100, 100, 100, 100), (100, 104, 99, 103), (103, 104, 101, 102), (103, 104, 99.5, 100), (100, 100, 99, 99)]
        s = bars_sym(rows, [2, 2, 10, 10, 10])
        x, fill, reason = ss.run_trade(s, 1, 4, "atr", 2.0, None)
        self.assertEqual((x, reason), (3, "stop"))
        self.assertAlmostEqual(fill, 100.0)  # ratcheted to 104 - 2*2; a widened 104 - 2*10 would not have stopped

    def test_max_hold_exits_at_close_and_stop_takes_precedence(self):
        flat = [(100, 100.5, 99.5, 100.0)] * 8
        s = bars_sym(flat, [1] * 8)
        self.assertEqual(ss.run_trade(s, 1, 7, "pct", 50.0, 3), (4, 100.0, "max_hold"))
        rows = list(flat)
        rows[4] = (100, 100.5, 40, 100.0)
        self.assertEqual(ss.run_trade(bars_sym(rows, [1] * 8), 1, 7, "pct", 50.0, 3)[2], "stop")

    def test_period_end_closes_the_trade_and_never_reads_later_bars(self):
        flat = [(100, 100.5, 99.5, 100.0)] * 10
        base = ss.run_trade(bars_sym(flat, [1] * 10), 1, 5, "pct", 50.0, None)
        self.assertEqual(base, (5, 100.0, "period_end"))
        later = list(flat[:6]) + [(1, 1, 1, 1)] * 4  # crash after the period ends
        self.assertEqual(ss.run_trade(bars_sym(later, [1] * 10), 1, 5, "pct", 50.0, None), base)


class CostTests(unittest.TestCase):
    def sym_with_signal(self, low_at=None):
        s = flat_symbol()
        flags = [False] * len(s["c"])
        flags[100] = True
        s["flags"] = {ss.CONTROL: flags}
        if low_at:
            s["l"][low_at] = 90.0
        return s

    def test_round_trip_cost_and_slippage_are_applied(self):
        cfg = ss.make_config(ss.CONTROL, "pct", 50.0, 5)
        trades = ss.simulate_trades(self.sym_with_signal(), cfg, 0, 139, 10, 5)
        self.assertEqual(len(trades), 1)
        expected = 100.0 * 0.9995 / (100.0 * 1.0005) - 1 - 0.001
        self.assertAlmostEqual(trades[0]["ret"], expected, places=9)
        self.assertEqual(trades[0]["reason"], "max_hold")
        self.assertEqual(trades[0]["hold_bars"], 5)
        compounded = math.prod(1 + dr for _, dr in trades[0]["daily"]) - 1
        self.assertAlmostEqual(compounded, trades[0]["ret"], places=5)

    def test_stop_exits_pay_double_slippage(self):
        cfg = ss.make_config(ss.CONTROL, "pct", 5.0, None)
        trades = ss.simulate_trades(self.sym_with_signal(low_at=103), cfg, 0, 139, 10, 5)
        raw = 100.5 * 0.95  # highest high since entry, less 5 percent
        expected = raw * (1 - 0.001) / (100.0 * 1.0005) - 1 - 0.001
        self.assertEqual(trades[0]["reason"], "stop")
        self.assertAlmostEqual(trades[0]["ret"], expected, places=9)

    def test_zero_costs_flat_market_returns_exactly_zero(self):
        cfg = ss.make_config(ss.CONTROL, "pct", 50.0, 5)
        trades = ss.simulate_trades(self.sym_with_signal(), cfg, 0, 139, 0, 0)
        self.assertAlmostEqual(trades[0]["ret"], 0.0, places=12)


class SplitAndPortfolioTests(unittest.TestCase):
    def test_periods_are_chronological_disjoint_and_cover_the_usable_dates(self):
        symbols, _ = ss.clean_symbols(make_document())
        periods = ss.split_periods(symbols, 0.5, 0.25)
        t, v, h = periods["train"], periods["validation"], periods["holdout"]
        self.assertLess(t["end"], v["start"])
        self.assertLess(v["end"], h["start"])
        joined = t["dates"] + v["dates"] + h["dates"]
        self.assertEqual(joined, sorted(set(joined)))
        self.assertEqual(len(joined), len(sorted({d for s in symbols for d in s["dates"]})) - ss.WARMUP - 1)
        self.assertAlmostEqual(t["days"] / len(joined), 0.5, delta=0.01)

    def test_short_datasets_are_rejected(self):
        symbols, _ = ss.clean_symbols(make_document(n_bars=150))
        with self.assertRaisesRegex(ValueError, "too short"):
            ss.split_periods(symbols, 0.5, 0.25)

    def test_trades_never_cross_the_period_boundary(self):
        symbols, _ = ss.clean_symbols(make_document())
        periods = ss.split_periods(symbols, 0.5, 0.25)
        cfg = ss.make_config("momentum_20d", "atr", 3.0, None)  # long holds stress the boundary
        for name, p in periods.items():
            for sym in symbols:
                from bisect import bisect_left, bisect_right
                lo, hi = bisect_left(sym["dates"], p["start"]), bisect_right(sym["dates"], p["end"]) - 1
                for trade in ss.simulate_trades(sym, cfg, lo, hi, 10, 5):
                    self.assertGreaterEqual(trade["entry_date"], p["start"])
                    self.assertLessEqual(trade["exit_date"], p["end"])

    def test_portfolio_curve_is_time_ordered_not_pooled_compounding(self):
        trade = {"ret": 0.10, "daily": [("d1", 0.10)], "hold_bars": 0, "reason": "stop"}
        m = ss.summarize([dict(trade), dict(trade)], ["d1", "d2"], 10)
        self.assertAlmostEqual(m["cumulative_return"], 0.02, places=6)  # not 1.1 * 1.1 - 1 = 0.21
        self.assertEqual(m["max_drawdown"], 0.0)

    def test_drawdown_and_order_invariance(self):
        a = {"ret": -0.12, "daily": [("d1", 0.10), ("d2", -0.20)], "hold_bars": 1, "reason": "stop"}
        m = ss.summarize([a], ["d1", "d2"], 1)
        self.assertAlmostEqual(m["cumulative_return"], -0.12, places=6)
        self.assertAlmostEqual(m["max_drawdown"], 0.88 / 1.1 - 1, places=5)
        b = {"ret": 0.03, "daily": [("d1", 0.03)], "hold_bars": 0, "reason": "max_hold"}
        self.assertEqual(ss.summarize([a, b], ["d1", "d2"], 5), ss.summarize([b, a], ["d1", "d2"], 5))

    def test_summary_statistics(self):
        trades = [{"ret": r, "daily": [("d1", r)], "hold_bars": 2, "reason": "stop"} for r in (0.02, -0.01, 0.03, -0.01)]
        m = ss.summarize(trades, ["d1"], 10)
        self.assertEqual(m["trades"], 4)
        self.assertAlmostEqual(m["expectancy_bps"], 75.0)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertAlmostEqual(m["profit_factor"], 0.05 / 0.02)
        self.assertEqual(ss.summarize([], ["d1"], 10)["trades"], 0)


class SelectionTests(unittest.TestCase):
    def search(self, doc=None, **kw):
        defaults = dict(minimum_train_trades=5, minimum_validation_trades=5, minimum_holdout_trades=5)
        defaults.update(kw)
        return ss.SignalSearch(doc or make_document(), **defaults)

    def test_selection_is_deterministic(self):
        self.assertEqual(json.dumps(self.search().select(), sort_keys=True),
                         json.dumps(self.search().select(), sort_keys=True))

    def test_holdout_data_cannot_influence_selection(self):
        doc = make_document()
        base = self.search(doc)
        before = base.select()
        start = base.periods["holdout"]["start"]
        for item in doc["symbols"]:
            for bar in item["bars"]:
                if bar["t"][:10] >= start:
                    for key in ("o", "h", "l", "c"):
                        bar[key] *= 3.0
        after = self.search(doc).select()
        self.assertEqual(json.dumps(before["grid"], sort_keys=True), json.dumps(after["grid"], sort_keys=True))
        self.assertEqual(before["selected"] and before["selected"]["key"], after["selected"] and after["selected"]["key"])
        self.assertTrue(any(r["train"]["trades"] > 0 and r["validation"]["trades"] > 0 for r in before["grid"]))

    def test_configs_with_too_few_trades_are_rejected(self):
        sel = self.search(minimum_train_trades=10 ** 6).select()
        self.assertIsNone(sel["selected"])
        self.assertEqual(sel["eligible_count"], 0)
        self.assertTrue(all("too_few_train_trades" in r["reject_reasons"] for r in sel["grid"]))
        self.assertEqual(ss.final_verdict(sel, None), "NO CANDIDATE PASSED TRAINING/VALIDATION SELECTION")

    def test_selection_requires_positive_expectancy_in_both_periods_and_stable_neighbors(self):
        sel = self.search().select()
        for row in sel["grid"]:
            if row["eligible"]:
                self.assertGreater(row["train"]["expectancy_bps"], 0)
                self.assertGreater(row["validation"]["expectancy_bps"], 0)
                self.assertGreaterEqual(row["neighbor_fraction"], 0.5)
                self.assertEqual(row["reject_reasons"], [])
            else:
                self.assertTrue(row["reject_reasons"])
        self.assertEqual(sel["configs_tested"], 96)
        self.assertEqual(sel["eligible_count"], sum(r["eligible"] for r in sel["grid"]))

    def test_dataset_flags_cover_survivorship_lookahead_and_feed(self):
        flags = " ".join(ss.dataset_flags(make_document(screened=True)))
        self.assertIn("SURVIVORSHIP BIAS", flags)
        self.assertIn("LOOK-AHEAD RISK", flags)
        clean = " ".join(ss.dataset_flags(make_document(screened=False)))
        self.assertIn("SURVIVORSHIP BIAS", clean)
        self.assertNotIn("LOOK-AHEAD RISK", clean)
        self.assertIn("IEX", " ".join(ss.dataset_flags({"data_quality": {"feed": "iex"}})))

    def test_invalid_inputs_are_rejected(self):
        bad = make_document()
        bad["parameters"]["timeframe"] = "1Hour"
        with self.assertRaisesRegex(ValueError, "daily bars only"):
            ss.SignalSearch(bad)
        doc = make_document()
        doc["symbols"][0]["bars"][10]["h"] = doc["symbols"][0]["bars"][10]["l"] - 1  # high below low
        doc["symbols"][1]["bars"][50]["t"] = doc["symbols"][1]["bars"][49]["t"]        # duplicate date
        symbols, excluded = ss.clean_symbols(doc)
        self.assertEqual(len(symbols), 4)
        self.assertEqual({e["symbol"] for e in excluded}, {"S0", "S1"})
        with self.assertRaises(ValueError):
            ss.SignalSearch(make_document(), train_fraction=0.9)

    def test_verdict_never_claims_a_working_edge(self):
        chosen = {"config": ss.make_config("momentum_20d", "atr", 2.0, 20)}
        sel = {"selected": chosen}
        ok = {"selected_min_trades": True, "selected_positive_after_costs": True,
              "stop_neighbors_positive": True, "beats_control": True}

        def verdict(**over):
            return ss.final_verdict(sel, {"examined": True, "checks": {**ok, **over}})

        texts = [ss.final_verdict(sel, None), verdict(), verdict(selected_min_trades=False),
                 verdict(selected_positive_after_costs=False), verdict(stop_neighbors_positive=False),
                 verdict(beats_control=False)]
        self.assertTrue(all("working edge" not in t.lower() for t in texts))
        self.assertIn("research lead only", texts[1])
        self.assertTrue(texts[3].startswith("NO VALIDATED EDGE"))
        self.assertTrue(texts[4].startswith("INCONCLUSIVE") and texts[5].startswith("INCONCLUSIVE"))
        self.assertTrue(texts[2].startswith("REJECTED"))
        self.assertTrue(texts[0].startswith("SELECTION ONLY"))


class ToolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.tools = TradingTools(self.root)
        self.doc = make_document()
        self.source = self.root / "trading_data/stock.json"
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps(self.doc), encoding="utf-8")
        self.env = patch.dict(os.environ, {"OBSIDIAN_VAULT": str(self.vault)}, clear=False)
        self.env.start()
        ticks = iter(range(10 ** 6))  # deterministic clock: every timestamped filename is unique
        self.clock = patch.object(trading_tools, "_utc_now", lambda: datetime(2026, 9, 21, tzinfo=timezone.utc) + timedelta(seconds=next(ticks)))
        self.clock.start()
        # Force a selection so the holdout path is exercised regardless of the synthetic data.
        original = ss.SignalSearch.select

        def forced(search):
            sel = original(search)
            if sel["selected"] is None:
                sel["selected"] = sel["grid"][0]
            return sel

        self.forced = patch.object(ss.SignalSearch, "select", forced)
        self.forced.start()

    def tearDown(self):
        self.forced.stop()
        self.clock.stop()
        self.env.stop()
        self.temp.cleanup()

    def run_search(self, **kw):
        base = dict(minimum_train_trades=5, minimum_validation_trades=5, minimum_holdout_trades=5)
        base.update(kw)
        return self.tools.search_stock_signals("trading_data/stock.json", **base)

    def test_tool_is_registered_as_a_write_tool_with_no_order_capability(self):
        spec = next(t for t in TRADING_TOOLS if t["name"] == "search_stock_signals")
        self.assertEqual(spec["effect"], "write")
        self.assertIn("dataset", spec["input_schema"]["required"])
        self.assertTrue(all("order" not in t["name"] for t in TRADING_TOOLS))

    def test_selection_only_run_never_touches_the_holdout_or_the_ledger(self):
        value = self.run_search()
        self.assertFalse(value["holdout_examined"])
        self.assertTrue(value["verdict"].startswith("SELECTION ONLY"))
        self.assertFalse((self.root / "trading_data/holdout_ledger.jsonl").exists())
        result = json.loads((self.root / value["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertIsNone(result["holdout_examination"])
        self.assertEqual(result["holdout"], {})

    def test_holdout_is_examined_once_per_dataset_and_period(self):
        first = self.run_search(reveal_holdout=True)
        self.assertTrue(first["holdout_examined"])
        ledger = (self.root / "trading_data/holdout_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(json.loads(ledger[0])["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        with self.assertRaisesRegex(RuntimeError, "already examined"):
            self.run_search(reveal_holdout=True)
        self.assertEqual(len((self.root / "trading_data/holdout_ledger.jsonl").read_text().splitlines()), 1)
        self.assertFalse(self.run_search()["holdout_examined"])  # selection-only reruns stay allowed

    def test_reveal_refuses_without_a_vault_and_burns_nothing(self):
        with patch.dict(os.environ, {"OBSIDIAN_VAULT": str(self.root / "missing")}):
            with self.assertRaisesRegex(RuntimeError, "OBSIDIAN_VAULT"):
                self.run_search(reveal_holdout=True)
        self.assertFalse((self.root / "trading_data/holdout_ledger.jsonl").exists())

    def test_artifact_and_report_contain_complete_results_and_methodology(self):
        value = self.run_search(reveal_holdout=True, report_title="Signal search test")
        result = json.loads((self.root / value["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["dataset"], "stock_signal_search")
        self.assertEqual(len(result["selection"]["grid"]), 96)
        self.assertEqual(result["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        exam = result["holdout_examination"]
        self.assertTrue(exam["examined"])
        self.assertTrue(exam["stop_neighbors"])
        self.assertEqual(set(exam["checks"]), {"selected_min_trades", "selected_positive_after_costs",
                                               "stop_neighbors_positive", "beats_control"})
        text = Path(value["obsidian_report"]["path"]).read_text(encoding="utf-8")
        self.assertEqual(Path(value["obsidian_report"]["path"]).parent.name, "Trading Research")
        for needle in ("## Methodology", "## Untouched holdout", "## Full grid", "SURVIVORSHIP BIAS",
                       "LOOK-AHEAD RISK", "Selection fingerprint", result["source_sha256"], "always-long control"):
            self.assertIn(needle, text)
        self.assertEqual(text.count("| momentum_20d |") + text.count("| breakout_20d |")
                         + text.count("| trend_pullback |") + text.count("| mean_reversion_uptrend |"), 96)
        row = self.tools.trading_data_status()["datasets"][0]
        self.assertEqual(row["dataset"], "stock_signal_search")
        self.assertEqual(row["backtest"]["verdict"], value["verdict"])

    def test_refuses_other_datasets_and_truncated_data(self):
        other = self.root / "trading_data/other.json"
        other.write_text(json.dumps({"dataset": "kalshi_bitcoin_15m"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sp100_stocks"):
            self.tools.search_stock_signals("trading_data/other.json")
        cut = self.root / "trading_data/cut.json"
        cut.write_text(json.dumps({"dataset": "sp100_stocks", "data_quality": {"pagination_truncated": True}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.tools.search_stock_signals("trading_data/cut.json")
        with self.assertRaises(ValueError):
            self.tools.search_stock_signals("../escape.json")

    def test_same_inputs_give_the_same_selection_fingerprint(self):
        a = self.run_search()["selection_fingerprint"]
        b = self.run_search()["selection_fingerprint"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
