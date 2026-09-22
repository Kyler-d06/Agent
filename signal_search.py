"""Bounded stock signal search with trailing-stop exits.

Research and paper testing only.  Everything here is pure and deterministic: no
network, no file writes, no order placement.  The grid is fixed in code, so the
search cannot grow beyond the settings listed below.

Execution assumptions (deliberately conservative, since only OHLC bars exist):
* A signal is evaluated at a bar's close and the trade enters at the NEXT open,
  plus adverse slippage.
* The stop active during bar j is computed only from information through bar
  j-1.  Bar j's own high never raises the stop before bar j's low is tested, so
  a bar that both rallies and touches the old stop is treated as stopped out.
* If a bar opens beyond the stop, the fill is the (worse) open, not the stop.
* Stop exits pay double slippage; every trade pays the round-trip cost.
* Each period is simulated on its own.  A trade still open at the period's last
  bar is closed at that bar's close, so no period ever sees a later period.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right

SIGNALS = ("momentum_20d", "breakout_20d", "trend_pullback", "mean_reversion_uptrend")
CONTROL = "always_long_control"
ATR_MULTIPLES = (1.5, 2.0, 2.5, 3.0)
PCT_STOPS = (3.0, 5.0, 7.0, 10.0)
MAX_HOLDS = (10, 20, None)          # None = no time limit (stop or period end only)
WARMUP = 100                        # bars needed by the 100-day average
ATR_PERIOD = 14
MIN_PERIOD_DATES = 20
PROFIT_FACTOR_CAP = 999.0
PERIODS = ("train", "validation", "holdout")


# ----------------------------------------------------------------- grid helpers
def stop_settings():
    return [("atr", v) for v in ATR_MULTIPLES] + [("pct", v) for v in PCT_STOPS]


def config_key(signal, kind, value, hold):
    return f"{signal}|{kind}{value:g}|hold{'none' if hold is None else hold}"


def make_config(signal, kind, value, hold):
    return {"key": config_key(signal, kind, value, hold), "signal": signal,
            "stop_kind": kind, "stop_value": value, "max_hold_bars": hold}


def build_grid():
    return [make_config(s, k, v, h) for s in SIGNALS for (k, v) in stop_settings() for h in MAX_HOLDS]


def control_config(cfg):
    return make_config(CONTROL, cfg["stop_kind"], cfg["stop_value"], cfg["max_hold_bars"])


def neighbor_configs(cfg):
    """Adjacent settings: one step along the stop axis and one along the hold axis."""
    same_kind = [v for k, v in stop_settings() if k == cfg["stop_kind"]]
    i = same_kind.index(cfg["stop_value"])
    stop = [make_config(cfg["signal"], cfg["stop_kind"], same_kind[j], cfg["max_hold_bars"])
            for j in (i - 1, i + 1) if 0 <= j < len(same_kind)]
    k = MAX_HOLDS.index(cfg["max_hold_bars"])
    hold = [make_config(cfg["signal"], cfg["stop_kind"], cfg["stop_value"], MAX_HOLDS[j])
            for j in (k - 1, k + 1) if 0 <= j < len(MAX_HOLDS)]
    return {"stop": stop, "hold": hold}


def multiple_testing_hurdle(configs):
    """Rough t-statistic the best of N unrelated configurations tends to reach by luck."""
    return math.sqrt(2 * math.log(max(2, configs)))


# ------------------------------------------------------------------- data prep
def clean_symbols(document):
    usable, excluded = [], []
    for item in document.get("symbols") or []:
        symbol = str(item.get("symbol") or "?")
        try:
            rows = item.get("bars") or []
            dates = [str(r["t"])[:10] for r in rows]
            o, h, l, c = ([float(r[k]) for r in rows] for k in ("o", "h", "l", "c"))
        except (KeyError, TypeError, ValueError):
            excluded.append({"symbol": symbol, "reason": "malformed bars"})
            continue
        if len(c) < WARMUP + 40:
            excluded.append({"symbol": symbol, "reason": "too few bars"})
            continue
        if any(b <= a for a, b in zip(dates, dates[1:])):
            excluded.append({"symbol": symbol, "reason": "dates are not strictly increasing"})
            continue
        invalid = any(
            not all(math.isfinite(x) for x in (o[i], h[i], l[i], c[i])) or min(o[i], h[i], l[i], c[i]) <= 0
            or h[i] < l[i] or h[i] < max(o[i], c[i]) or l[i] > min(o[i], c[i])
            for i in range(len(c)))
        if invalid:
            excluded.append({"symbol": symbol, "reason": "invalid OHLC values"})
            continue
        sym = {"symbol": symbol, "dates": dates, "o": o, "h": h, "l": l, "c": c}
        sym["atr"] = atr_series(h, l, c)
        sym["flags"] = signal_flags(h, c)
        usable.append(sym)
    return usable, excluded


def atr_series(h, l, c, period=ATR_PERIOD):
    """Wilder ATR; value at index i uses bars up to and including i."""
    n = len(c)
    out = [None] * n
    if n <= period:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    value = sum(tr[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, n):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out


def signal_flags(h, c):
    """Entry signals evaluated at each bar's close, using data through that bar only."""
    n = len(c)
    flags = {name: [False] * n for name in (*SIGNALS, CONTROL)}
    for i in range(WARMUP, n - 1):
        window = c[i - 19:i + 1]
        sma20 = statistics.fmean(window)
        sma50 = statistics.fmean(c[i - 49:i + 1])
        sma100 = statistics.fmean(c[i - 99:i + 1])
        std20 = statistics.stdev(window) if len(set(window)) > 1 else 0
        z20 = (c[i] - sma20) / std20 if std20 else 0
        flags["momentum_20d"][i] = c[i] > sma20 and c[i] / c[i - 20] - 1 > 0.03
        flags["trend_pullback"][i] = sma20 > sma50 and sma50 > sma100 and sma50 < c[i] < sma20
        flags["mean_reversion_uptrend"][i] = c[i] > sma100 and z20 <= -1.5
        flags["breakout_20d"][i] = c[i] > max(h[i - 20:i])
        flags[CONTROL][i] = True
    return flags


def split_periods(symbols, train_fraction, validation_fraction):
    """Chronological, non-overlapping periods defined by calendar date across all symbols."""
    all_dates = sorted({d for s in symbols for d in s["dates"]})
    usable = all_dates[WARMUP + 1:]
    n = len(usable)
    a, b = int(n * train_fraction), int(n * (train_fraction + validation_fraction))
    parts = {"train": usable[:a], "validation": usable[a:b], "holdout": usable[b:]}
    if any(len(v) < MIN_PERIOD_DATES for v in parts.values()):
        raise ValueError("dataset is too short for separate training, validation and holdout periods")
    return {name: {"start": v[0], "end": v[-1], "days": len(v), "dates": v} for name, v in parts.items()}


# ------------------------------------------------------------------ simulation
def run_trade(sym, e, hi, kind, value, max_hold):
    """Walk bar by bar from entry bar e.  Returns (exit_index, raw_fill, reason)."""
    o, h, l, c, atr = sym["o"], sym["h"], sym["l"], sym["c"], sym["atr"]
    entry_open = o[e]
    stop = entry_open - value * atr[e - 1] if kind == "atr" else entry_open * (1 - value / 100.0)
    highest = 0.0
    for j in range(e, hi + 1):
        if j > e:  # ratchet using information through bar j-1 only; never lower the stop
            candidate = highest - value * atr[j - 1] if kind == "atr" else highest * (1 - value / 100.0)
            if candidate > stop:
                stop = candidate
        if o[j] <= stop:
            return j, o[j], "stop_gap"
        if l[j] <= stop:
            return j, stop, "stop"
        if h[j] > highest:
            highest = h[j]
        if max_hold is not None and j - e >= max_hold:
            return j, c[j], "max_hold"
        if j == hi:
            return j, c[j], "period_end"
    return hi, c[hi], "period_end"


def simulate_trades(sym, cfg, lo, hi, cost_bps, slippage_bps):
    kind, value, max_hold = cfg["stop_kind"], cfg["stop_value"], cfg["max_hold_bars"]
    flags = sym["flags"][cfg["signal"]]
    dates, o, c, atr = sym["dates"], sym["o"], sym["c"], sym["atr"]
    slip, cost = slippage_bps / 1e4, cost_bps / 1e4
    trades, i = [], max(lo - 1, WARMUP)
    while i < hi:
        if not flags[i] or (kind == "atr" and not (atr[i] or 0) > 0):
            i += 1
            continue
        e = i + 1
        x, raw, reason = run_trade(sym, e, hi, kind, value, max_hold)
        stopped = reason in ("stop", "stop_gap")
        entry_fill = o[e] * (1 + slip)
        exit_fill = raw * (1 - slip * (2 if stopped else 1))
        daily = []
        for j in range(e, x + 1):
            base = entry_fill if j == e else c[j - 1]
            daily.append((dates[j], (exit_fill / base - 1 - cost) if j == x else (c[j] / base - 1)))
        trades.append({"symbol": sym["symbol"], "entry_date": dates[e], "exit_date": dates[x],
                       "hold_bars": x - e, "reason": reason, "ret": exit_fill / entry_fill - 1 - cost,
                       "daily": daily})
        i = x + 1
    return trades


def _r(value, digits=6):
    return round(float(value), digits) if math.isfinite(float(value)) else 0.0


def summarize(trades, period_dates, slots):
    """Per-trade statistics plus a time-ordered, equal-weight portfolio curve.

    Each open trade carries weight 1/max(slots, open trades) so capital is never
    over-committed.  Idle capital earns nothing.  Trades are NOT compounded in
    symbol order, which would manufacture huge cumulative returns.
    """
    empty = {"trades": 0, "expectancy_bps": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "t_stat": 0.0,
             "cumulative_return": 0.0, "max_drawdown": 0.0, "avg_hold_bars": 0.0,
             "period_end_share": 0.0, "stop_share": 0.0}
    if not trades:
        return empty
    returns = [t["ret"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    mean = statistics.fmean(returns)
    sd = statistics.stdev(returns) if len(returns) > 1 else 0.0
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (PROFIT_FACTOR_CAP if wins else 0.0)
    agg = {}
    for t in trades:
        for date, dr in t["daily"]:
            total, count = agg.get(date, (0.0, 0))
            agg[date] = (total + dr, count + 1)
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for date in period_dates:
        total, count = agg.get(date, (0.0, 0))
        if count:
            equity *= max(0.0, 1 + total / max(slots, count))
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    n = len(trades)
    return {"trades": n, "expectancy_bps": _r(mean * 1e4, 3), "win_rate": _r(len(wins) / n, 4),
            "profit_factor": _r(min(profit_factor, PROFIT_FACTOR_CAP), 4),
            "t_stat": _r(mean / sd * math.sqrt(n) if sd else 0.0, 4),
            "cumulative_return": _r(equity - 1, 5), "max_drawdown": _r(drawdown, 5),
            "avg_hold_bars": _r(statistics.fmean(t["hold_bars"] for t in trades), 2),
            "period_end_share": _r(sum(t["reason"] == "period_end" for t in trades) / n, 4),
            "stop_share": _r(sum(t["reason"] in ("stop", "stop_gap") for t in trades) / n, 4)}


# ---------------------------------------------------------------------- search
def dataset_flags(document):
    params = document.get("parameters") or {}
    quality = document.get("data_quality") or {}
    flags = ["SURVIVORSHIP BIAS: the universe is today's S&P 100 constituents applied to the past, so companies "
             "that later fell out of the index are missing and results are biased upward."]
    if params.get("min_realized_vol") is not None or params.get("min_dollar_volume") is not None:
        flags.append("LOOK-AHEAD RISK: symbols were screened by realized volatility and dollar volume measured over "
                     "the same window used here, so the universe already reflects how each stock turned out, "
                     "including during the holdout. Recollect with a universe fixed before the test window.")
    flags.append("Prices are adjusted for splits and dividends using data known at collection time (a mild "
                 "look-ahead in price levels). Taxes, borrow limits, market impact and liquidity are not modeled.")
    if quality.get("feed") == "iex":
        flags.append("The bars come from the single-exchange IEX feed, so volume and extremes can differ from the consolidated tape.")
    return flags


class SignalSearch:
    def __init__(self, document, *, cost_bps=10.0, slippage_bps=5.0, train_fraction=0.5,
                 validation_fraction=0.25, minimum_train_trades=60, minimum_validation_trades=30,
                 minimum_holdout_trades=30, portfolio_slots=10, minimum_neighbor_fraction=0.5):
        if not 0 <= cost_bps <= 100 or not 0 <= slippage_bps <= 50:
            raise ValueError("cost_bps must be within 0-100 and slippage_bps within 0-50")
        if not (0.3 <= train_fraction <= 0.7 and 0.1 <= validation_fraction <= 0.3
                and train_fraction + validation_fraction <= 0.9):
            raise ValueError("train_fraction must be 0.3-0.7, validation_fraction 0.1-0.3, leaving at least 10% for the holdout")
        if (document.get("parameters") or {}).get("timeframe", "1Day") != "1Day":
            raise ValueError("signal search supports daily bars only")
        self.cost_bps, self.slippage_bps = float(cost_bps), float(slippage_bps)
        self.slots = max(1, int(portfolio_slots))
        self.minimum = {"train": int(minimum_train_trades), "validation": int(minimum_validation_trades),
                        "holdout": int(minimum_holdout_trades)}
        self.min_neighbor_fraction = float(minimum_neighbor_fraction)
        self.document = document
        self.symbols, self.excluded = clean_symbols(document)
        if len(self.symbols) < 3:
            raise ValueError("at least three usable symbols are required")
        self.periods = split_periods(self.symbols, float(train_fraction), float(validation_fraction))
        self._cache = {}

    def metrics(self, cfg, period):
        key = (cfg["key"], period)
        if key not in self._cache:
            p, trades = self.periods[period], []
            for sym in self.symbols:
                lo, hi = bisect_left(sym["dates"], p["start"]), bisect_right(sym["dates"], p["end"]) - 1
                if hi > lo:
                    trades.extend(simulate_trades(sym, cfg, lo, hi, self.cost_bps, self.slippage_bps))
            self._cache[key] = summarize(trades, p["dates"], self.slots)
        return self._cache[key]

    def _reject_reasons(self, row):
        reasons = []
        for name in ("train", "validation"):
            m = row[name]
            if m["trades"] < self.minimum[name]:
                reasons.append(f"too_few_{name}_trades")
            elif m["expectancy_bps"] <= 0:
                reasons.append(f"non_positive_{name}_expectancy")
        return reasons

    def period_summary(self):
        return {name: {k: v for k, v in p.items() if k != "dates"} for name, p in self.periods.items()}

    def select(self):
        """Choose a configuration from training and validation data only."""
        grid = []
        for cfg in build_grid():
            ctrl = control_config(cfg)
            row = {"key": cfg["key"], "config": cfg,
                   "train": self.metrics(cfg, "train"), "validation": self.metrics(cfg, "validation")}
            row["train_lift_bps"] = _r(row["train"]["expectancy_bps"] - self.metrics(ctrl, "train")["expectancy_bps"], 3)
            row["validation_lift_bps"] = _r(row["validation"]["expectancy_bps"] - self.metrics(ctrl, "validation")["expectancy_bps"], 3)
            row["score"] = _r(min(row["train"]["t_stat"], row["validation"]["t_stat"]), 4)
            grid.append(row)
        by_key = {row["key"]: row for row in grid}
        for row in grid:
            reasons = self._reject_reasons(row)
            near = neighbor_configs(row["config"])
            near_all = near["stop"] + near["hold"]
            passing = [n for n in near_all if not self._reject_reasons(by_key[n["key"]])]
            row["neighbor_fraction"] = _r(len(passing) / len(near_all), 4)
            if not reasons and row["neighbor_fraction"] < self.min_neighbor_fraction:
                reasons.append("unstable_neighbors")
            row["reject_reasons"], row["eligible"] = reasons, not reasons
        candidates = [r for r in grid if r["eligible"]]
        selected = max(candidates, key=lambda r: (r["score"], r["key"])) if candidates else None
        hurdle = multiple_testing_hurdle(len(grid))
        flags = dataset_flags(self.document)
        if selected is None:
            flags.insert(0, "No configuration met the minimum trade counts, positive expectancy in both periods, and neighbor stability.")
        else:
            tr, va = selected["train"], selected["validation"]
            if va["t_stat"] < hurdle:
                flags.insert(0, f"OVERFITTING RISK: the best validation t-statistic ({va['t_stat']:.2f}) is below about {hurdle:.2f}, "
                                f"which the best of {len(grid)} unrelated configurations often reaches by chance (heuristic hurdle).")
            if va["expectancy_bps"] < 0.5 * tr["expectancy_bps"]:
                flags.insert(0, "OVERFITTING RISK: validation expectancy is less than half of training expectancy.")
            if selected["train_lift_bps"] <= 0 or selected["validation_lift_bps"] <= 0:
                flags.insert(0, "The selected signal did not beat the always-long control (same stops, same costs) in training and validation, so market drift may explain the result.")
            if selected["neighbor_fraction"] < 1:
                flags.insert(0, f"PARAMETER INSTABILITY: only {selected['neighbor_fraction']:.0%} of adjacent settings passed selection.")
            if max(tr["period_end_share"], va["period_end_share"]) > 0.2:
                flags.append("More than 20% of selected-config trades were closed at a period boundary; the periods may be too short for these stops.")
        return {"periods": self.period_summary(), "configs_tested": len(grid), "hurdle_t": _r(hurdle, 3),
                "eligible_count": len(candidates), "grid": grid, "selected": selected, "flags": flags,
                "excluded_symbols": self.excluded, "symbols_used": len(self.symbols)}

    def examine_holdout(self, selection):
        """Evaluate the frozen selection and its neighbors on the holdout exactly once."""
        selected = selection["selected"]
        cfg = selected["config"]
        near = neighbor_configs(cfg)

        def row(c):
            m, ctrl = self.metrics(c, "holdout"), self.metrics(control_config(c), "holdout")
            return {"key": c["key"], "config": c, "holdout": m, "control": ctrl,
                    "lift_bps": _r(m["expectancy_bps"] - ctrl["expectancy_bps"], 3)}

        chosen, stop_rows, hold_rows = row(cfg), [row(c) for c in near["stop"]], [row(c) for c in near["hold"]]
        minimum = self.minimum["holdout"]

        def positive(r):
            h = r["holdout"]
            return h["trades"] >= minimum and h["expectancy_bps"] > 0 and h["profit_factor"] > 1

        checks = {"selected_min_trades": chosen["holdout"]["trades"] >= minimum,
                  "selected_positive_after_costs": positive(chosen),
                  "stop_neighbors_positive": bool(stop_rows) and all(positive(r) for r in stop_rows),
                  "beats_control": chosen["lift_bps"] > 0}
        return {"examined": True, "period": self.period_summary()["holdout"], "selected": chosen,
                "stop_neighbors": stop_rows, "hold_neighbors": hold_rows, "checks": checks}


def final_verdict(selection, holdout):
    if selection["selected"] is None:
        return "NO CANDIDATE PASSED TRAINING/VALIDATION SELECTION"
    if not holdout or not holdout.get("examined"):
        return "SELECTION ONLY - untouched holdout not examined"
    c = holdout["checks"]
    if not c["selected_min_trades"]:
        return "REJECTED - too few holdout trades to judge"
    if not c["selected_positive_after_costs"]:
        return "NO VALIDATED EDGE - selected configuration failed the untouched holdout"
    if not c["stop_neighbors_positive"]:
        return "INCONCLUSIVE - holdout positive, but neighboring stop settings did not confirm it"
    if not c["beats_control"]:
        return "INCONCLUSIVE - holdout positive, but not better than the always-long control"
    return "HOLDOUT-POSITIVE AND NEIGHBOR-STABLE - research lead only, not confirmed"


# ---------------------------------------------------------------------- report
def _stop_label(cfg):
    return f"{cfg['stop_value']:g} ATR" if cfg["stop_kind"] == "atr" else f"{cfg['stop_value']:g}%"


def describe(cfg):
    return f"{cfg['signal']} | stop {_stop_label(cfg)} | max hold {_hold_label(cfg)}"


def _hold_label(cfg):
    return "none" if cfg["max_hold_bars"] is None else str(cfg["max_hold_bars"])


def _m(m):
    return (f"{m['trades']} trades, {m['expectancy_bps']:.1f} bps, PF {m['profit_factor']:.2f}, win {m['win_rate']:.0%}, "
            f"cum {m['cumulative_return']:.1%}, DD {m['max_drawdown']:.1%}")


def render_report(title, result):
    p, sel, hold = result["parameters"], result["selection"], result["holdout_examination"]
    lines = ["---", "tags: [trading-research, signal-search, stocks, paper]", f"created: {result['collected_at']}", "---", "",
             f"# {title}", "", f"**Verdict:** {result['verdict']}", "",
             "Research and paper testing only. Nothing here can place or authorize a live order.", "",
             "## Flags", ""]
    lines += [f"- {f}" for f in result["flags"]]
    lines += ["", "## Methodology", "",
              "- **Signals (unchanged from the existing backtester):** momentum_20d, breakout_20d, trend_pullback, mean_reversion_uptrend. A signal is read at a bar's close; entry is the next open.",
              "- **Exits:** initial protective stop at entry, ratcheting trailing stop (ATR(14) multiples 1.5/2.0/2.5/3.0 or 3/5/7/10 percent from the highest high since entry), optional maximum hold of 10 or 20 bars or none. Stops never move down.",
              "- **Conservative fills:** the stop for a bar uses only earlier bars, so a bar that rallies and also touches the old stop counts as stopped out. A gap open beyond the stop fills at the open. Stop exits pay double slippage.",
              f"- **Costs:** round-trip cost {p['cost_bps']} bps plus {p['slippage_bps']} bps slippage per side.",
              "- **Periods:** chronological and split by calendar date across all symbols, so no symbol sees another's future. Each period is simulated on its own; open trades are closed at the period's last bar.",
              f"- **Selection:** training and validation only. A configuration must have at least {p['minimum_train_trades']} training and {p['minimum_validation_trades']} validation trades, positive expectancy in both, and at least {p['minimum_neighbor_fraction']:.0%} of adjacent settings passing the same tests. Score is the lower of the two t-statistics.",
              "- **Control:** an always-long entry with identical stops, holds and costs, to separate signal value from market drift.",
              f"- **Portfolio curve:** time-ordered and equal weight with {p['portfolio_slots']} slots; trades are never compounded in symbol order.",
              f"- **Holdout:** examined {'once, for the frozen selection and its neighbors' if hold and hold.get('examined') else 'not at all in this run'}. Minimum holdout trades: {p['minimum_holdout_trades']}.",
              "- **Multiple testing:** the grid has 96 signal configurations; the t-statistic hurdle is heuristic, not a formal correction.", "",
              "## Periods", "", "| Period | Start | End | Days |", "|---|---|---|---:|"]
    for name in PERIODS:
        q = sel["periods"][name]
        lines.append(f"| {name} | {q['start']} | {q['end']} | {q['days']} |")
    lines += ["", f"Symbols used: {sel['symbols_used']}. Configurations tested: {sel['configs_tested']}. Passing selection: {sel['eligible_count']}.", ""]
    for ex in sel["excluded_symbols"]:
        lines.append(f"- Excluded {ex['symbol']}: {ex['reason']}")
    s = sel["selected"]
    lines += ["", "## Selected configuration", ""]
    if s is None:
        lines.append("None passed selection.")
    else:
        c = s["config"]
        lines += [f"**{c['signal']}**, stop {_stop_label(c)}, max hold {_hold_label(c)}", "", f"- Selection fingerprint: `{result['selection_fingerprint']}`",
                  f"- Train: {_m(s['train'])}", f"- Validation: {_m(s['validation'])}",
                  f"- Lift versus control (bps): train {s['train_lift_bps']:.1f}, validation {s['validation_lift_bps']:.1f}",
                  f"- Neighbor stability: {s['neighbor_fraction']:.0%} of adjacent settings passed"]
    lines += ["", "## Untouched holdout", ""]
    if not hold or not hold.get("examined"):
        lines.append("Not examined in this run. Selection is frozen and identified by the fingerprint above.")
    else:
        q = hold["period"]
        lines += [f"Period {q['start']} to {q['end']} ({q['days']} days).", "", "| Setting | Holdout | Control lift (bps) |", "|---|---|---:|"]
        for label, r in [("selected", hold["selected"])] + [("stop neighbor", x) for x in hold["stop_neighbors"]] + [("hold neighbor", x) for x in hold["hold_neighbors"]]:
            c = r["config"]
            lines.append(f"| {label}: {_stop_label(c)}, hold {_hold_label(c)} | {_m(r['holdout'])} | {r['lift_bps']:.1f} |")
        lines += ["", "Checks: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in hold["checks"].items())]
    lines += ["", "## Full grid (training and validation)", "",
              "| Signal | Stop | Hold | Train n | Train bps | Train PF | Val n | Val bps | Val PF | Lift val | Neighbors | Result |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in sel["grid"]:
        c, tr, va = r["config"], r["train"], r["validation"]
        outcome = "eligible" if r["eligible"] else ", ".join(r["reject_reasons"])
        lines.append(f"| {c['signal']} | {_stop_label(c)} | {_hold_label(c)} | {tr['trades']} | {tr['expectancy_bps']:.1f} | {tr['profit_factor']:.2f} | "
                     f"{va['trades']} | {va['expectancy_bps']:.1f} | {va['profit_factor']:.2f} | {r['validation_lift_bps']:.1f} | {r['neighbor_fraction']:.0%} | {outcome} |")
    lines += ["", "## Reproducibility", "", f"- Dataset: `{result['source_dataset']}`", f"- Dataset SHA-256: `{result['source_sha256']}`",
              f"- Parameters: `{p}`", "- The search is deterministic: the same dataset and parameters reproduce the same selection fingerprint."]
    return "\n".join(lines) + "\n"
