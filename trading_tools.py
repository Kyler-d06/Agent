"""Bounded, research-only market data collection for the Universal Harness.

This module deliberately has no order-placement method.  Network collection is
grant-gated by UniversalPlatform and every artifact records its provenance and
data-quality limitations.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from platform_contracts import confined, tool
import signal_search


TEXT = {"type": "string"}
TRADING_TOOLS = [
    tool(
        "trading_data_status",
        "Inspect locally collected trading-research datasets, manifests, provenance and warnings. Never places orders.",
    ),
    tool(
        "collect_sp100_stock_bars",
        "Collect delayed consolidated S&P 100 stock bars for reproducible swing research. Screens by realized volatility and dollar volume; never places orders.",
        {
            "timeframe": {"type": "string", "enum": ["1Day", "1Hour", "15Min"]},
            "lookback_days": {"type": "integer", "minimum": 30, "maximum": 1000},
            "max_symbols": {"type": "integer", "minimum": 5, "maximum": 100},
            "min_realized_vol": {"type": "number", "minimum": 0.05, "maximum": 3.0},
            "min_dollar_volume": {"type": "number", "minimum": 0, "maximum": 1000000000000},
            "feed": {"type": "string", "enum": ["sip", "iex"]},
            "output": TEXT,
        },
        effect="external",
    ),
    tool(
        "backtest_stock_edges",
        "Run deterministic train/test evaluation of fixed stock rules with costs, write a JSON result, and save a Markdown report in the configured Obsidian vault. No model interpretation is performed.",
        {
            "dataset": TEXT,
            "cost_bps": {"type": "number", "minimum": 0, "maximum": 100},
            "train_fraction": {"type": "number", "minimum": 0.5, "maximum": 0.85},
            "minimum_test_trades": {"type": "integer", "minimum": 5, "maximum": 500},
            "report_title": TEXT,
        },
        ["dataset"],
        effect="write",
    ),
    tool(
        "search_stock_signals",
        "Research-only bounded search over the four existing entry signals with ATR (1.5/2.0/2.5/3.0) and percentage (3/5/7/10) trailing stops, an initial protective stop, optional maximum hold, costs and slippage. Selects on chronological training and validation periods only; the untouched holdout is examined once and only when reveal_holdout is true. Writes JSON and an Obsidian report. Never places orders.",
        {
            "dataset": TEXT,
            "cost_bps": {"type": "number", "minimum": 0, "maximum": 100},
            "slippage_bps": {"type": "number", "minimum": 0, "maximum": 50},
            "train_fraction": {"type": "number", "minimum": 0.3, "maximum": 0.7},
            "validation_fraction": {"type": "number", "minimum": 0.1, "maximum": 0.3},
            "minimum_train_trades": {"type": "integer", "minimum": 5, "maximum": 2000},
            "minimum_validation_trades": {"type": "integer", "minimum": 5, "maximum": 1000},
            "minimum_holdout_trades": {"type": "integer", "minimum": 5, "maximum": 1000},
            "portfolio_slots": {"type": "integer", "minimum": 1, "maximum": 50},
            "reveal_holdout": {"type": "boolean"},
            "report_title": TEXT,
        },
        ["dataset"],
        effect="write",
    ),
    tool(
        "collect_sp100_options",
        "Collect a volatility-filtered S&P 100 option-chain research snapshot from Alpaca. This never places orders; indicative data is delayed/modified and unsuitable for live scalping.",
        {
            "style": {"type": "string", "enum": ["scalp", "swing"]},
            "max_underlyings": {"type": "integer", "minimum": 1, "maximum": 25},
            "min_realized_vol": {"type": "number", "minimum": 0.05, "maximum": 3.0},
            "feed": {"type": "string", "enum": ["indicative", "opra"]},
            "stock_feed": {"type": "string", "enum": ["iex", "sip"]},
            "min_option_volume": {"type": "integer", "minimum": 0, "maximum": 10000000},
            "max_spread_pct": {"type": "number", "minimum": 0.001, "maximum": 2.0},
            "output": TEXT,
        },
        effect="external",
    ),
    tool(
        "collect_kalshi_markets",
        "Collect public Kalshi Bitcoin 15-minute or weather market research data, including rules and optional order books. Never places orders.",
        {
            "kind": {"type": "string", "enum": ["bitcoin_15m", "weather"]},
            "max_markets": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_orderbooks": {"type": "boolean"},
            "lookback_hours": {"type": "integer", "minimum": 1, "maximum": 720},
            "output": TEXT,
        },
        ["kind"],
        effect="external",
    ),
    tool("kalshi_paper_status", "Inspect the local Kalshi paper account. Uses no funds and places no orders."),
    tool(
        "start_kalshi_paper",
        "Create a new virtual Kalshi bankroll. Refuses to overwrite an existing paper account.",
        {"bankroll_usd": {"type": "number", "minimum": 1, "maximum": 100}},
        effect="write",
    ),
    tool(
        "record_kalshi_paper_fill",
        "Record a simulated fill using a current public Kalshi ask. Requires an operator or outside model to supply the ticker, side, and rationale. Never places a live order.",
        {"ticker": TEXT, "side": {"type": "string", "enum": ["yes", "no"]},
         "max_spend_usd": {"type": "number", "minimum": 0.01, "maximum": 100},
         "rationale": TEXT, "decision_source": TEXT,
         "fee_per_contract_usd": {"type": "number", "minimum": 0, "maximum": 1}},
        ["ticker", "side", "max_spend_usd", "rationale", "decision_source"],
        effect="external",
    ),
    tool(
        "reconcile_kalshi_paper",
        "Check open virtual positions against Kalshi's official market result and update paper P&L. Never places a live order.",
        effect="external",
    ),
]


ISHARES_OEF_HOLDINGS = "https://www.ishares.com/us/products/239723/ishares-s-p-100-etf/latest-holdings.csv"
ALPACA_DATA = "https://data.alpaca.markets"
KALSHI_DATA = "https://external-api.kalshi.com/trade-api/v2"


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class TradingTools:
    def __init__(self, root, store=None, opener=urlopen):
        self.root = Path(root).resolve()
        self.store = store
        self.opener = opener
        self.data_root = confined(self.root, "trading_data")

    @property
    def kalshi_paper_path(self):
        return confined(self.root, "trading_data/kalshi_paper/account.json")

    def invoke(self, name, args):
        return getattr(self, name)(**args)

    @staticmethod
    def _alpaca_credentials():
        key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Alpaca credentials are not configured in APCA_API_KEY_ID/APCA_API_SECRET_KEY")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _request(self, url, *, headers=None, timeout=30):
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "UniversalHarness/1.0", **(headers or {})})
        try:
            with self.opener(request, timeout=timeout) as response:
                body = response.read(25_000_000)
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(f"upstream HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"upstream connection failed: {exc}") from exc
        return body

    def _request_json(self, base, path, query=None, headers=None):
        url = base.rstrip("/") + "/" + path.lstrip("/")
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        try:
            value = json.loads(self._request(url, headers=headers).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("upstream returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("upstream returned an unexpected JSON shape")
        return value

    def _output_path(self, output, prefix):
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        relative = output or f"trading_data/{prefix}/{stamp}.json"
        if not relative.lower().endswith(".json"):
            raise ValueError("output must be a relative .json path")
        target = confined(self.root, relative)
        if target.exists():
            raise FileExistsError("output already exists; choose a new path")
        return target

    @staticmethod
    def _atomic_create(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _json_bytes(value)
        fd, temporary = tempfile.mkstemp(prefix=".collect-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                raise FileExistsError("output already exists")
            os.replace(temporary, path)
            metadata = {
                "dataset": value.get("dataset"), "collected_at": value.get("collected_at"),
                "record_count": value.get("record_count"), "warnings": value.get("warnings", []),
                "data_quality": value.get("data_quality"),
                "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            }
            if value.get("dataset") in ("stock_edge_backtest", "stock_signal_search"):
                metadata["backtest"] = {
                    "verdict": value.get("verdict"),
                    "selected_strategy": value.get("selected_strategy"),
                    "holdout": value.get("holdout") or {},
                    "candidates": [{
                        "strategy": row.get("strategy"),
                        "train": row.get("train") or {},
                        "test": row.get("test") or {},
                    } for row in (value.get("candidates") or [])],
                }
            if str(value.get("dataset") or "").startswith("kalshi_"):
                metadata["markets"] = [{
                    "ticker": (row.get("market") or {}).get("ticker"),
                    "title": (row.get("market") or {}).get("title"),
                    "status": (row.get("market") or {}).get("status"),
                    "yes_ask": TradingTools._market_ask(row.get("market") or {}, "yes"),
                    "no_ask": TradingTools._market_ask(row.get("market") or {}, "no"),
                    "close_time": (row.get("market") or {}).get("close_time"),
                } for row in (value.get("markets") or [])[:100]]
            path.with_suffix(path.suffix + ".meta").write_bytes(_json_bytes(metadata))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": str(path), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    def trading_data_status(self):
        if not self.data_root.exists():
            return {"datasets": [], "count": 0, "live_trading_enabled": False, "kalshi_paper": None}
        datasets = []
        for path in sorted(self.data_root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            if path == self.kalshi_paper_path:
                continue
            try:
                stat = path.stat()
                meta_path = path.with_suffix(path.suffix + ".meta")
                doc = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
                datasets.append({
                    "path": str(path.relative_to(self.root)),
                    "bytes": stat.st_size,
                    "sha256": doc.get("sha256"),
                    "collected_at": doc.get("collected_at"),
                    "dataset": doc.get("dataset"),
                    "record_count": doc.get("record_count"),
                    "warnings": doc.get("warnings", []),
                    "data_quality": doc.get("data_quality"),
                    "backtest": doc.get("backtest"),
                    "markets": doc.get("markets"),
                })
            except (OSError, ValueError, json.JSONDecodeError):
                datasets.append({"path": str(path.relative_to(self.root)), "error": "unreadable dataset"})
        paper = None
        try:
            if self.kalshi_paper_path.is_file():
                account = json.loads(self.kalshi_paper_path.read_text(encoding="utf-8"))
                paper = {key: account.get(key) for key in (
                    "created_at", "initial_bankroll_usd", "cash_usd", "realized_pnl_usd", "open_positions", "settled_positions"
                )}
        except (OSError, ValueError, json.JSONDecodeError):
            paper = {"error": "paper account is unreadable"}
        return {"datasets": datasets, "count": len(datasets), "live_trading_enabled": False,
                "kalshi_paper": paper}

    @staticmethod
    def _atomic_replace(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = value.encode("utf-8") if isinstance(value, str) else _json_bytes(value)
        fd, temporary = tempfile.mkstemp(prefix=".paper-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _paper_account(self):
        if not self.kalshi_paper_path.is_file():
            raise RuntimeError("start the Kalshi paper account first")
        value = json.loads(self.kalshi_paper_path.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1:
            raise RuntimeError("unsupported Kalshi paper account schema")
        return value

    def _sync_kalshi_paper_report(self, account):
        configured = os.environ.get("OBSIDIAN_VAULT")
        if not configured:
            return None
        vault = Path(configured).expanduser().resolve()
        if not vault.is_dir():
            return None
        target = vault / "Trading Research" / "Kalshi Paper Experiment.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["---", "tags: [trading-research, kalshi, paper]", "---", "", "# Kalshi Paper Experiment", "",
                 "This ledger uses virtual funds only. It cannot place a live order.", "",
                 f"- Initial bankroll: ${float(account['initial_bankroll_usd']):.2f}",
                 f"- Virtual cash: ${float(account['cash_usd']):.2f}",
                 f"- Realized P&L: ${float(account.get('realized_pnl_usd') or 0):.2f}",
                 f"- Open positions: {len(account.get('open_positions') or [])}",
                 f"- Settled positions: {len(account.get('settled_positions') or [])}", "",
                 "## Positions", "", "| Ticker | Side | Contracts | Cost | Status | Result | P&L | Decision source | Rationale |",
                 "|---|---|---:|---:|---|---|---:|---|---|"]
        for row in (account.get("open_positions") or []) + (account.get("settled_positions") or []):
            rationale = str(row.get("rationale") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {row.get('ticker','')} | {row.get('side','')} | {row.get('contracts',0)} | "
                         f"${float(row.get('cost_usd') or 0):.2f} | {row.get('status','')} | "
                         f"{row.get('official_result','')} | ${float(row.get('pnl_usd') or 0):.2f} | "
                         f"{row.get('decision_source','')} | {rationale} |")
        self._atomic_replace(target, "\n".join(lines) + "\n")
        return str(target)

    def kalshi_paper_status(self):
        if not self.kalshi_paper_path.is_file():
            return {"configured": False, "live_trading_enabled": False}
        return {**self._paper_account(), "configured": True, "live_trading_enabled": False}

    def start_kalshi_paper(self, bankroll_usd=10):
        if self.kalshi_paper_path.exists():
            raise FileExistsError("a Kalshi paper account already exists; it was not overwritten")
        bankroll = round(float(bankroll_usd), 4)
        account = {"schema_version": 1, "dataset": "kalshi_paper_account", "created_at": _iso(_utc_now()),
                   "initial_bankroll_usd": bankroll, "cash_usd": bankroll, "realized_pnl_usd": 0.0,
                   "open_positions": [], "settled_positions": [],
                   "warnings": ["Virtual funds only; this account cannot place a live Kalshi order.",
                                "Fees are user-supplied estimates and may differ by market."]}
        self._atomic_replace(self.kalshi_paper_path, account)
        return {**account, "obsidian_report": self._sync_kalshi_paper_report(account)}

    @staticmethod
    def _market_ask(market, side):
        dollars = market.get(side + "_ask_dollars")
        if dollars not in (None, ""):
            return float(dollars)
        cents = market.get(side + "_ask")
        if cents is not None:
            return float(cents) / 100
        return None

    def record_kalshi_paper_fill(self, ticker, side, max_spend_usd, rationale,
                                 decision_source, fee_per_contract_usd=0.02):
        account = self._paper_account()
        ticker = str(ticker).strip().upper()
        if not ticker or not str(rationale).strip() or not str(decision_source).strip():
            raise ValueError("ticker, rationale, and decision_source are required")
        payload = self._request_json(KALSHI_DATA, f"/markets/{ticker}")
        market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
        if str(market.get("status") or "").lower() not in {"open", "active"}:
            raise ValueError("market is not open")
        ask = self._market_ask(market, side)
        if ask is None or not 0 < ask < 1:
            raise RuntimeError("current public ask is unavailable")
        fee = float(fee_per_contract_usd)
        budget = min(float(max_spend_usd), float(account["cash_usd"]))
        contracts = int(budget // (ask + fee))
        if contracts < 1:
            raise ValueError("paper budget is too small for one contract at the current ask plus estimated fee")
        cost = round(contracts * (ask + fee), 4)
        position = {"id": "KP-" + hashlib.sha256(f"{ticker}:{side}:{time.time_ns()}".encode()).hexdigest()[:12],
                    "ticker": ticker, "side": side, "contracts": contracts, "ask_usd": ask,
                    "estimated_fee_usd": round(contracts * fee, 4), "cost_usd": cost,
                    "opened_at": _iso(_utc_now()), "rationale": str(rationale).strip()[:2000],
                    "decision_source": str(decision_source).strip()[:200], "status": "open"}
        account["cash_usd"] = round(float(account["cash_usd"]) - cost, 4)
        account["open_positions"].append(position)
        self._atomic_replace(self.kalshi_paper_path, account)
        return {"position": position, "cash_usd": account["cash_usd"], "live_order_placed": False,
                "obsidian_report": self._sync_kalshi_paper_report(account)}

    def reconcile_kalshi_paper(self):
        account = self._paper_account()
        remaining, settled_now, errors = [], [], []
        for position in account.get("open_positions") or []:
            try:
                payload = self._request_json(KALSHI_DATA, f"/markets/{position['ticker']}")
                market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
                result = str(market.get("result") or "").lower()
                if result not in {"yes", "no"}:
                    remaining.append(position)
                    continue
                payout = float(position["contracts"]) if result == position["side"] else 0.0
                pnl = round(payout - float(position["cost_usd"]), 4)
                closed = {**position, "status": "settled", "official_result": result,
                          "payout_usd": round(payout, 4), "pnl_usd": pnl, "settled_at": _iso(_utc_now())}
                account["cash_usd"] = round(float(account["cash_usd"]) + payout, 4)
                account["realized_pnl_usd"] = round(float(account.get("realized_pnl_usd") or 0) + pnl, 4)
                account["settled_positions"].append(closed)
                settled_now.append(closed)
            except Exception as exc:
                remaining.append(position)
                errors.append({"ticker": position.get("ticker"), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
        account["open_positions"] = remaining
        self._atomic_replace(self.kalshi_paper_path, account)
        return {"settled": settled_now, "still_open": len(remaining), "errors": errors,
                "cash_usd": account["cash_usd"], "realized_pnl_usd": account["realized_pnl_usd"],
                "live_order_placed": False, "obsidian_report": self._sync_kalshi_paper_report(account)}

    def _sp100_universe(self):
        raw = self._request(ISHARES_OEF_HOLDINGS).decode("utf-8-sig", errors="replace")
        lines = raw.splitlines()
        header_index = next((i for i, line in enumerate(lines) if line.startswith("Ticker,")), None)
        if header_index is None:
            raise RuntimeError("could not parse the OEF holdings feed")
        rows = list(csv.DictReader(io.StringIO("\n".join(lines[header_index:]))))
        universe = []
        for row in rows:
            if row.get("Asset Class") != "Equity" or row.get("Currency") != "USD":
                continue
            ticker = (row.get("Ticker") or "").strip().replace(" ", ".")
            if ticker and ticker.replace(".", "").isalnum():
                universe.append({"symbol": ticker, "name": row.get("Name"), "sector": row.get("Sector"), "weight_pct": row.get("Weight (%)")})
        if len(universe) < 90:
            raise RuntimeError("OEF holdings feed did not contain the expected equity universe")
        return universe

    def _paged_stock_bars(self, symbols, timeframe, start, end, feed, batch_size=None):
        """Fetch every batch independently so one alphabetical symbol cannot starve the rest."""
        symbols = list(symbols)
        size = max(1, int(batch_size or len(symbols) or 1))
        bars, pages, truncated = {}, 0, False
        for offset in range(0, len(symbols), size):
            batch = symbols[offset:offset + size]
            page_token, batch_pages = None, 0
            while batch_pages < 50:
                payload = self._request_json(ALPACA_DATA, "/v2/stocks/bars", {
                    "symbols": ",".join(batch), "timeframe": timeframe, "start": _iso(start), "end": _iso(end),
                    "limit": 10000, "adjustment": "all", "feed": feed, "page_token": page_token,
                    "sort": "asc",
                }, self._alpaca_credentials())
                pages += 1
                batch_pages += 1
                for symbol, rows in (payload.get("bars") or {}).items():
                    bars.setdefault(symbol, []).extend(rows or [])
                page_token = payload.get("next_page_token")
                if not page_token:
                    break
            truncated = truncated or bool(page_token)
        return bars, pages, truncated

    def collect_sp100_stock_bars(self, timeframe="1Day", lookback_days=730, max_symbols=30,
                                 min_realized_vol=0.20, min_dollar_volume=50_000_000,
                                 feed="sip", output=None):
        target = self._output_path(output, "sp100_stocks")
        now = _utc_now()
        # Free SIP access excludes the latest 15 minutes. A five-minute cushion
        # avoids clock skew while retaining consolidated historical coverage.
        end = now - timedelta(minutes=20) if feed == "sip" else now
        universe = self._sp100_universe()
        symbols = [row["symbol"] for row in universe]
        screen_days = max(90, int(lookback_days) if timeframe == "1Day" else min(int(lookback_days), 365))
        daily, screen_pages, screen_truncated = self._paged_stock_bars(
            symbols, "1Day", end - timedelta(days=screen_days), end, feed
        )
        by_symbol = {row["symbol"]: row for row in universe}
        screened = []
        for symbol in symbols:
            rows = daily.get(symbol) or []
            rv = self._realized_vol(rows)
            dollar_volumes = [float(row.get("c") or 0) * float(row.get("v") or 0) for row in rows[-20:]]
            avg_dollar_volume = statistics.fmean(dollar_volumes) if dollar_volumes else 0
            if rv is not None and rv >= float(min_realized_vol) and avg_dollar_volume >= float(min_dollar_volume):
                screened.append((rv, avg_dollar_volume, symbol))
        screened.sort(reverse=True)
        chosen = [symbol for _, _, symbol in screened[:int(max_symbols)]]
        if timeframe == "1Day":
            collected, pages, truncated = {}, screen_pages, screen_truncated
            cutoff = end - timedelta(days=int(lookback_days))
            for symbol in chosen:
                collected[symbol] = [row for row in daily.get(symbol, []) if str(row.get("t") or "") >= _iso(cutoff)]
        else:
            collected, pages, truncated = self._paged_stock_bars(
                chosen, timeframe, end - timedelta(days=int(lookback_days)), end, feed, batch_size=1
            )
        screen_map = {symbol: {"realized_vol_annualized": rv, "average_daily_dollar_volume": adv}
                      for rv, adv, symbol in screened}
        records = []
        for symbol in chosen:
            rows = collected.get(symbol) or []
            records.append({**by_symbol[symbol], **screen_map[symbol], "bar_count": len(rows), "bars": rows})
        warnings = [
            "Research and paper testing only; this collector cannot place orders.",
            "Universe selection and volatility screening use information available at collection time; historical constituent survivorship bias remains.",
            "Strategy evaluation must include out-of-sample testing, costs and slippage; a backtest does not establish a durable edge.",
        ]
        if feed == "sip":
            warnings.append("The end timestamp is intentionally at least 20 minutes old for free delayed consolidated SIP access.")
        else:
            warnings.append("IEX is a single-exchange feed and is not suitable for validating market-wide scalping liquidity.")
        result = {
            "schema_version": 1, "dataset": "sp100_stocks", "collected_at": _iso(now),
            "record_count": sum(row["bar_count"] for row in records),
            "parameters": {"timeframe": timeframe, "lookback_days": lookback_days, "max_symbols": max_symbols,
                "min_realized_vol": min_realized_vol, "min_dollar_volume": min_dollar_volume, "feed": feed,
                "effective_end": _iso(end)},
            "provenance": {"universe": ISHARES_OEF_HOLDINGS, "bars": ALPACA_DATA + "/v2/stocks/bars"},
            "data_quality": {"feed": feed, "delayed_minutes": 20 if feed == "sip" else 0,
                "pages_collected": pages, "pagination_truncated": truncated, "paper_research_only": True},
            "warnings": warnings, "universe_count": len(universe), "eligible_count": len(screened),
            "symbols": records,
        }
        artifact = self._atomic_create(target, result)
        return {"artifact": {**artifact, "path": str(target.relative_to(self.root))},
                "summary": {k: result[k] for k in ("dataset", "collected_at", "record_count", "universe_count", "eligible_count", "warnings")}}

    @staticmethod
    def _strategy_trades(bars, strategy, cost_bps):
        closes = [float(row.get("c") or 0) for row in bars]
        highs = [float(row.get("h") or row.get("c") or 0) for row in bars]
        trades, i = [], 100
        while i < len(bars) - 11:
            sma20 = statistics.fmean(closes[i - 19:i + 1])
            sma50 = statistics.fmean(closes[i - 49:i + 1])
            sma100 = statistics.fmean(closes[i - 99:i + 1])
            window20 = closes[i - 19:i + 1]
            std20 = statistics.stdev(window20) if len(set(window20)) > 1 else 0
            z20 = (closes[i] - sma20) / std20 if std20 else 0
            if strategy == "momentum_20d":
                signal, hold = closes[i] > sma20 and closes[i] / closes[i - 20] - 1 > 0.03, 5
            elif strategy == "trend_pullback":
                signal, hold = sma20 > sma50 and sma50 > sma100 and sma50 < closes[i] < sma20, 5
            elif strategy == "mean_reversion_uptrend":
                signal, hold = closes[i] > sma100 and z20 <= -1.5, 5
            elif strategy == "breakout_20d":
                signal, hold = closes[i] > max(highs[i - 20:i]), 10
            else:
                raise ValueError("unknown strategy")
            if not signal:
                i += 1
                continue
            entry_index, exit_index = i + 1, min(i + 1 + hold, len(bars) - 1)
            entry = float(bars[entry_index].get("o") or bars[entry_index].get("c") or 0)
            exit_price = float(bars[exit_index].get("c") or 0)
            if entry > 0 and exit_price > 0:
                trades.append({"entry_time": bars[entry_index].get("t"), "exit_time": bars[exit_index].get("t"),
                    "return": exit_price / entry - 1 - float(cost_bps) / 10000, "hold_bars": hold})
            i = exit_index + 1
        return trades

    @staticmethod
    def _metrics(returns):
        if not returns:
            return {"trades": 0, "expectancy_bps": 0, "win_rate": 0, "profit_factor": 0,
                    "cumulative_return": 0, "max_drawdown": 0, "trade_score": 0}
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        equity, peak, max_drawdown = 1.0, 1.0, 0.0
        for value in returns:
            equity *= max(0, 1 + value)
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1)
        mean = statistics.fmean(returns)
        std = statistics.stdev(returns) if len(returns) > 1 else 0
        return {
            "trades": len(returns), "expectancy_bps": mean * 10000,
            "win_rate": len(wins) / len(returns),
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else (999 if wins else 0),
            "cumulative_return": equity - 1, "max_drawdown": max_drawdown,
            "trade_score": mean / std * math.sqrt(len(returns)) if std else 0,
        }

    def _write_obsidian_report(self, title, content):
        configured = os.environ.get("OBSIDIAN_VAULT")
        if not configured:
            raise RuntimeError("OBSIDIAN_VAULT is not configured")
        vault = Path(configured).expanduser().resolve()
        if not vault.is_dir():
            raise RuntimeError("configured Obsidian vault does not exist")
        folder = vault / "Trading Research"
        folder.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r"[^A-Za-z0-9 _-]+", "", title).strip()[:80] or "Stock edge report"
        path = folder / f"{_utc_now().strftime('%Y-%m-%d %H%M%S')} - {safe_title}.md"
        path.write_text(content, encoding="utf-8", errors="strict")
        return {"path": str(path), "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def backtest_stock_edges(self, dataset, cost_bps=10, train_fraction=0.70,
                             minimum_test_trades=20, report_title="S&P 100 stock edge test"):
        source = confined(self.root, dataset)
        if not source.is_file() or source.suffix.lower() != ".json":
            raise ValueError("dataset must be a collected JSON file")
        if source.stat().st_size > 250_000_000:
            raise ValueError("dataset exceeds the 250 MB backtest limit")
        raw = source.read_bytes()
        document = json.loads(raw)
        if document.get("dataset") != "sp100_stocks":
            raise ValueError("backtest_stock_edges requires an sp100_stocks dataset")
        if (document.get("data_quality") or {}).get("pagination_truncated"):
            raise ValueError("dataset pagination was truncated; recollect it before backtesting")
        strategies = ["momentum_20d", "trend_pullback", "mean_reversion_uptrend", "breakout_20d"]
        evaluated = []
        for strategy in strategies:
            training, testing, symbol_rows = [], [], []
            for item in document.get("symbols") or []:
                bars = item.get("bars") or []
                if len(bars) < 125:
                    continue
                split = max(101, min(len(bars) - 12, int(len(bars) * float(train_fraction))))
                split_time = str(bars[split].get("t") or "")
                trades = self._strategy_trades(bars, strategy, cost_bps)
                train_returns = [row["return"] for row in trades if str(row.get("entry_time") or "") < split_time]
                test_returns = [row["return"] for row in trades if str(row.get("entry_time") or "") >= split_time]
                training.extend(train_returns)
                testing.extend(test_returns)
                symbol_rows.append({"symbol": item.get("symbol"), "train_trades": len(train_returns),
                                    "test_trades": len(test_returns), "test_expectancy_bps": self._metrics(test_returns)["expectancy_bps"]})
            evaluated.append({"strategy": strategy, "train": self._metrics(training),
                              "test": self._metrics(testing), "symbols": symbol_rows})
        ranked = sorted(evaluated, key=lambda row: row["train"]["trade_score"], reverse=True)
        selected = ranked[0] if ranked else None
        holdout = (selected or {}).get("test") or self._metrics([])
        passes = bool(holdout["trades"] >= int(minimum_test_trades) and holdout["expectancy_bps"] > 0
                      and holdout["profit_factor"] > 1 and holdout["trade_score"] > 0.5)
        verdict = "PROMISING — requires independent confirmation" if passes else "NO VALIDATED EDGE"
        now = _utc_now()
        result = {
            "schema_version": 1, "dataset": "stock_edge_backtest", "collected_at": _iso(now),
            "record_count": len(evaluated), "source_dataset": str(source.relative_to(self.root)),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "parameters": {"cost_bps": cost_bps, "train_fraction": train_fraction,
                           "minimum_test_trades": minimum_test_trades},
            "selection_rule": "highest training trade_score; holdout examined once",
            "selected_strategy": (selected or {}).get("strategy"), "holdout": holdout,
            "verdict": verdict, "candidates": evaluated,
            "warnings": [
                "A promising holdout is a research lead, not proof of a durable or tradable edge.",
                "The current universe has survivorship bias because present-day S&P 100 constituents are used historically.",
                "Corporate actions, borrow constraints, taxes, market impact and order queue position are not modeled.",
                "Do not authorize live trading from this report; repeat on a later untouched period and paper trade first.",
            ],
        }
        target = self._output_path(None, "stock_backtests")
        artifact = self._atomic_create(target, result)
        lines = [
            "---", "tags: [trading-research, backtest, stocks]", f"created: {_iso(now)}", "---", "",
            f"# {report_title}", "", f"**Verdict:** {verdict}", "",
            f"Selected on training data: **{result['selected_strategy'] or 'none'}**", "",
            "## Untouched holdout", "",
            f"- Trades: {holdout['trades']}", f"- Expectancy: {holdout['expectancy_bps']:.2f} bps/trade",
            f"- Win rate: {holdout['win_rate']:.1%}", f"- Profit factor: {holdout['profit_factor']:.2f}",
            f"- Cumulative return: {holdout['cumulative_return']:.1%}", f"- Maximum drawdown: {holdout['max_drawdown']:.1%}",
            f"- Trade score: {holdout['trade_score']:.2f}", "", "## Candidate comparison", "",
            "| Strategy | Train trades | Train expectancy | Test trades | Test expectancy | Test PF |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in ranked:
            lines.append(f"| {row['strategy']} | {row['train']['trades']} | {row['train']['expectancy_bps']:.2f} bps | {row['test']['trades']} | {row['test']['expectancy_bps']:.2f} bps | {row['test']['profit_factor']:.2f} |")
        lines.extend(["", "## Reproducibility", "", f"- Dataset: `{result['source_dataset']}`",
            f"- Dataset SHA-256: `{result['source_sha256']}`", f"- Round-trip cost assumption: {cost_bps:.2f} bps",
            f"- Train fraction: {train_fraction:.0%}", "", "## Limitations", ""])
        lines.extend(f"- {warning}" for warning in result["warnings"])
        report = self._write_obsidian_report(report_title, "\n".join(lines) + "\n")
        return {"artifact": {**artifact, "path": str(target.relative_to(self.root))},
                "obsidian_report": report, "verdict": verdict, "selected_strategy": result["selected_strategy"],
                "holdout": holdout}

    def _holdout_ledger_path(self):
        return confined(self.root, "trading_data/holdout_ledger.jsonl")

    def _holdout_examined(self, source_sha256, period):
        path = self._holdout_ledger_path()
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (entry.get("source_sha256"), entry.get("holdout_start"), entry.get("holdout_end")) == (
                    source_sha256, period["start"], period["end"]):
                return entry
        return None

    def search_stock_signals(self, dataset, cost_bps=10, slippage_bps=5, train_fraction=0.5,
                             validation_fraction=0.25, minimum_train_trades=60,
                             minimum_validation_trades=30, minimum_holdout_trades=30,
                             portfolio_slots=10, reveal_holdout=False, report_title="S&P 100 signal search"):
        source = confined(self.root, dataset)
        if not source.is_file() or source.suffix.lower() != ".json":
            raise ValueError("dataset must be a collected JSON file")
        if source.stat().st_size > 250_000_000:
            raise ValueError("dataset exceeds the 250 MB backtest limit")
        raw = source.read_bytes()
        document = json.loads(raw)
        if document.get("dataset") != "sp100_stocks":
            raise ValueError("search_stock_signals requires an sp100_stocks dataset")
        if (document.get("data_quality") or {}).get("pagination_truncated"):
            raise ValueError("dataset pagination was truncated; recollect it before searching")
        configured = os.environ.get("OBSIDIAN_VAULT")
        if not configured or not Path(configured).expanduser().is_dir():
            raise RuntimeError("OBSIDIAN_VAULT must point to an existing vault before a signal search runs")
        parameters = {"cost_bps": cost_bps, "slippage_bps": slippage_bps, "train_fraction": train_fraction,
                      "validation_fraction": validation_fraction, "minimum_train_trades": minimum_train_trades,
                      "minimum_validation_trades": minimum_validation_trades,
                      "minimum_holdout_trades": minimum_holdout_trades, "portfolio_slots": portfolio_slots,
                      "minimum_neighbor_fraction": 0.5}
        search = signal_search.SignalSearch(document, **parameters)
        selection = search.select()
        chosen = selection["selected"]
        source_sha = hashlib.sha256(raw).hexdigest()
        fingerprint = hashlib.sha256(_json_bytes({
            "source_sha256": source_sha, "parameters": parameters, "selected": chosen and chosen["key"],
            "train": chosen and chosen["train"], "validation": chosen and chosen["validation"]})).hexdigest()
        holdout = None
        if reveal_holdout:
            if chosen is None:
                holdout = {"examined": False, "reason": "no candidate passed selection; the holdout was not examined"}
            else:
                period = selection["periods"]["holdout"]
                prior = self._holdout_examined(source_sha, period)
                if prior:
                    raise RuntimeError("the holdout for this dataset was already examined at "
                                       f"{prior.get('examined_at')}; it is no longer untouched. Collect newer data for a fresh holdout.")
                ledger = self._holdout_ledger_path()
                ledger.parent.mkdir(parents=True, exist_ok=True)
                # Record the look BEFORE computing it, so a crash cannot leave a silent second look.
                with ledger.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"source_sha256": source_sha, "holdout_start": period["start"],
                                             "holdout_end": period["end"], "examined_at": _iso(_utc_now()),
                                             "selected": chosen["key"], "selection_fingerprint": fingerprint},
                                            sort_keys=True) + "\n")
                holdout = search.examine_holdout(selection)
        verdict = signal_search.final_verdict(selection, holdout)
        now = _utc_now()
        result = {
            "schema_version": 1, "dataset": "stock_signal_search", "collected_at": _iso(now),
            "record_count": selection["configs_tested"], "source_dataset": str(source.relative_to(self.root)),
            "source_sha256": source_sha, "parameters": parameters, "verdict": verdict,
            "selected_strategy": signal_search.describe(chosen["config"]) if chosen else None,
            "holdout": holdout["selected"]["holdout"] if holdout and holdout.get("examined") else {},
            "selection_fingerprint": fingerprint, "flags": selection["flags"], "warnings": selection["flags"],
            "data_quality": {"paper_research_only": True, "symbols_used": selection["symbols_used"],
                             "excluded_symbols": len(selection["excluded_symbols"])},
            "selection": selection, "holdout_examination": holdout,
        }
        target = self._output_path(None, "signal_search")
        artifact = self._atomic_create(target, result)
        report = self._write_obsidian_report(report_title, signal_search.render_report(report_title, result))
        return {"artifact": {**artifact, "path": str(target.relative_to(self.root))}, "obsidian_report": report,
                "verdict": verdict, "selected_strategy": result["selected_strategy"],
                "selection_fingerprint": fingerprint, "holdout_examined": bool(holdout and holdout.get("examined")),
                "eligible_configs": selection["eligible_count"], "flags": selection["flags"]}

    @staticmethod
    def _realized_vol(bars):
        closes = [float(row["c"]) for row in bars if row.get("c") not in (None, 0)]
        if len(closes) < 10:
            return None
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        return statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None

    @staticmethod
    def _option_row(symbol, snapshot):
        quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
        bar = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}
        bid = quote.get("bp") if quote.get("bp") is not None else quote.get("bid_price")
        ask = quote.get("ap") if quote.get("ap") is not None else quote.get("ask_price")
        mid = (float(bid) + float(ask)) / 2 if bid is not None and ask is not None and float(ask) + float(bid) > 0 else None
        spread_pct = (float(ask) - float(bid)) / mid if mid and float(ask) >= float(bid) else None
        contract = re.match(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$", symbol)
        expiration = option_type = strike = None
        if contract:
            try:
                expiration = datetime.strptime(contract.group(2), "%y%m%d").date().isoformat()
                option_type = "call" if contract.group(3) == "C" else "put"
                strike = int(contract.group(4)) / 1000
            except ValueError:
                pass
        return {
            "symbol": symbol,
            "expiration": expiration,
            "option_type": option_type,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "volume": int(bar.get("v") or bar.get("volume") or 0),
            "implied_volatility": snapshot.get("impliedVolatility", snapshot.get("implied_volatility")),
            "greeks": snapshot.get("greeks"),
            "latest_trade": snapshot.get("latestTrade", snapshot.get("latest_trade")),
            "quote_timestamp": quote.get("t", quote.get("timestamp")),
        }

    def collect_sp100_options(self, style="swing", max_underlyings=10, min_realized_vol=0.25,
                              feed="indicative", stock_feed="iex", min_option_volume=1,
                              max_spread_pct=0.35, output=None):
        credentials = self._alpaca_credentials()
        target = self._output_path(output, "sp100_options")
        universe = self._sp100_universe()
        symbols = [row["symbol"] for row in universe]
        now = _utc_now()
        bars_response = self._request_json(ALPACA_DATA, "/v2/stocks/bars", {
            "symbols": ",".join(symbols), "timeframe": "1Day", "start": _iso(now - timedelta(days=60)),
            "end": _iso(now), "limit": 10000, "adjustment": "all", "feed": stock_feed,
        }, credentials)
        bars_by_symbol = bars_response.get("bars") or {}
        scored = []
        by_symbol = {row["symbol"]: row for row in universe}
        for symbol in symbols:
            bars = bars_by_symbol.get(symbol) or []
            rv = self._realized_vol(bars)
            if rv is not None and rv >= float(min_realized_vol):
                scored.append((rv, symbol, bars))
        scored.sort(reverse=True)
        selected, errors = [], []
        dte = (0, 14) if style == "scalp" else (14, 60)
        for rv, symbol, bars in scored[:int(max_underlyings)]:
            close = float(bars[-1]["c"])
            try:
                snapshots, page_token, page_count = {}, None, 0
                while page_count < 10:
                    chain = self._request_json(ALPACA_DATA, f"/v1beta1/options/snapshots/{symbol}", {
                        "feed": feed,
                        "expiration_date_gte": (now.date() + timedelta(days=dte[0])).isoformat(),
                        "expiration_date_lte": (now.date() + timedelta(days=dte[1])).isoformat(),
                        "strike_price_gte": round(close * 0.80, 2),
                        "strike_price_lte": round(close * 1.20, 2),
                        "limit": 1000, "page_token": page_token,
                    }, credentials)
                    page_count += 1
                    snapshots.update(chain.get("snapshots") or {})
                    page_token = chain.get("next_page_token")
                    if not page_token:
                        break
                options = []
                for option_symbol, snapshot in snapshots.items():
                    row = self._option_row(option_symbol, snapshot)
                    if row["volume"] < int(min_option_volume):
                        continue
                    if row["spread_pct"] is None or row["spread_pct"] > float(max_spread_pct):
                        continue
                    options.append(row)
                selected.append({
                    **by_symbol[symbol], "realized_vol_annualized": rv, "underlying_close": close,
                    "underlying_last_bar": bars[-1].get("t"), "option_count": len(options), "options": options,
                    "pages_collected": page_count, "pagination_truncated": bool(page_token),
                })
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
        warnings = [
            "Research data only; no order-placement capability is present.",
            "Realized volatility is a backward-looking screen, not a prediction or trading signal.",
        ]
        if feed == "indicative":
            warnings.append("Alpaca indicative option data is delayed/modified and is not suitable for live scalping decisions.")
        result = {
            "schema_version": 1, "dataset": "sp100_options", "collected_at": _iso(now),
            "record_count": sum(row["option_count"] for row in selected),
            "parameters": {"style": style, "max_underlyings": max_underlyings, "min_realized_vol": min_realized_vol,
                           "feed": feed, "stock_feed": stock_feed, "min_option_volume": min_option_volume,
                           "max_spread_pct": max_spread_pct, "dte": list(dte), "strike_band_pct": 20},
            "provenance": {"universe": ISHARES_OEF_HOLDINGS, "stock_bars": ALPACA_DATA + "/v2/stocks/bars",
                           "option_chains": ALPACA_DATA + "/v1beta1/options/snapshots/{underlying}"},
            "data_quality": {"option_feed": feed, "stock_feed": stock_feed, "paper_research_only": True},
            "warnings": warnings, "universe_count": len(universe), "volatile_underlying_count": len(scored),
            "underlyings": selected, "errors": errors,
        }
        artifact = self._atomic_create(target, result)
        return {"artifact": {**artifact, "path": str(target.relative_to(self.root))}, "summary": {k: result[k] for k in ("dataset", "collected_at", "record_count", "universe_count", "volatile_underlying_count", "warnings")}, "errors": errors}

    @staticmethod
    def _kalshi_match(market, kind):
        text = " ".join(str(market.get(k) or "") for k in ("ticker", "series_ticker", "title", "subtitle", "event_ticker")).lower()
        if kind == "bitcoin_15m":
            return ("bitcoin" in text or "btc" in text) and any(term in text for term in ("15 min", "15-min", "15m", "fifteen minute"))
        ticker = str(market.get("ticker") or market.get("series_ticker") or "").upper()
        category = str(market.get("category") or "").lower()
        weather_words = bool(re.search(r"\b(weather|temperatures?|rain|snow|precipitation|hurricanes?)\b", text))
        weather_ticker = ticker.startswith(("KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXTEMP", "KXHURRICANE"))
        return "weather" in category and (weather_words or weather_ticker)

    def _kalshi_open_markets(self, kind, max_markets):
        if kind == "bitcoin_15m":
            payload = self._request_json(KALSHI_DATA, "/markets", {
                "series_ticker": "KXBTC15M", "status": "open", "limit": min(1000, max_markets),
            })
            direct = []
            for market in payload.get("markets") or []:
                row = dict(market)
                row.setdefault("series_ticker", "KXBTC15M")
                if not row.get("series_ticker"):
                    row["series_ticker"] = "KXBTC15M"
                direct.append(row)
            if direct:
                return direct[:max_markets], 1
        matches, cursor, pages = [], None, 0
        while pages < 20:
            payload = self._request_json(KALSHI_DATA, "/events", {
                "status": "open", "limit": 200, "with_nested_markets": "true", "cursor": cursor,
            })
            pages += 1
            for event in payload.get("events") or []:
                event_context = {k: event.get(k) for k in ("event_ticker", "series_ticker", "title", "subtitle", "category")}
                event_matches = self._kalshi_match(event_context, kind)
                for market in event.get("markets") or []:
                    enriched = dict(market)
                    enriched.setdefault("series_ticker", event.get("series_ticker"))
                    enriched.setdefault("category", event.get("category"))
                    enriched["event"] = event_context
                    if event_matches or self._kalshi_match(enriched, kind):
                        matches.append(enriched)
                        if len(matches) >= 1000:
                            break
                if len(matches) >= 1000:
                    break
            cursor = payload.get("cursor")
            if not cursor or len(matches) >= 1000:
                break
        matches.sort(key=lambda row: str(row.get("close_time") or "9999"))
        return matches[:max_markets], pages

    def collect_kalshi_markets(self, kind, max_markets=25, include_orderbooks=True, lookback_hours=48, output=None):
        target = self._output_path(output, "kalshi_" + kind)
        now = _utc_now()
        markets, pages = self._kalshi_open_markets(kind, int(max_markets))
        records, errors = [], []
        for market in markets:
            ticker = market.get("ticker")
            record = {"market": market}
            if not ticker:
                continue
            if include_orderbooks:
                try:
                    book_payload = self._request_json(KALSHI_DATA, f"/markets/{ticker}/orderbook", {"depth": 100})
                    fixed_point = book_payload.get("orderbook_fp")
                    record["orderbook"] = fixed_point if fixed_point is not None else book_payload.get("orderbook")
                    record["orderbook_format"] = "fixed_point_dollars" if fixed_point is not None else "legacy_cents"
                except Exception as exc:
                    errors.append({"ticker": ticker, "component": "orderbook", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            series = market.get("series_ticker")
            if series:
                try:
                    candle = self._request_json(KALSHI_DATA, f"/series/{series}/markets/{ticker}/candlesticks", {
                        "start_ts": int((now - timedelta(hours=int(lookback_hours))).timestamp()),
                        "end_ts": int(now.timestamp()), "period_interval": 1,
                    })
                    record["candlesticks"] = candle.get("candlesticks") or []
                except Exception as exc:
                    errors.append({"ticker": ticker, "component": "candlesticks", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            records.append(record)
        warnings = [
            "Research data only; no Kalshi authentication or order-placement capability is present.",
            "Prediction-market prices are not probabilities with certainty and can reflect spread, liquidity and participant bias.",
        ]
        if kind == "weather":
            warnings.append("Settlement must follow each market's stored rules and named official source; third-party forecasts are not settlement truth.")
        result = {
            "schema_version": 1, "dataset": "kalshi_" + kind, "collected_at": _iso(now),
            "record_count": len(records), "parameters": {"kind": kind, "max_markets": max_markets,
                "include_orderbooks": include_orderbooks, "lookback_hours": lookback_hours},
            "provenance": {"api_base": KALSHI_DATA, "markets": KALSHI_DATA + "/markets?series_ticker=KXBTC15M",
                "events": KALSHI_DATA + "/events?with_nested_markets=true",
                "orderbooks": KALSHI_DATA + "/markets/{ticker}/orderbook",
                "candlesticks": KALSHI_DATA + "/series/{series}/markets/{ticker}/candlesticks"},
            "data_quality": {"public_api": True, "paper_research_only": True, "pages_scanned": pages},
            "warnings": warnings, "markets": records, "errors": errors,
        }
        artifact = self._atomic_create(target, result)
        return {"artifact": {**artifact, "path": str(target.relative_to(self.root))}, "summary": {k: result[k] for k in ("dataset", "collected_at", "record_count", "warnings")}, "errors": errors}
