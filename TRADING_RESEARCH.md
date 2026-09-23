# Paper-only market edge laboratory

Stock research is now the primary purpose of this deployment. The active path
is deliberately narrow:

1. Retrieve the current S&P 100/OEF universe.
2. Screen it by realized volatility and average dollar volume.
3. Collect consolidated stock bars ending at least 20 minutes ago.
4. Execute fixed rules with next-bar entries, non-overlapping trades,
   costs, a training segment, and an untouched holdout segment.
5. Save machine-readable evidence under `trading_data/stock_backtests/` and a
   readable Markdown report in `Trading Research/` inside the configured
   Obsidian vault.

This path contains no LLM call. It does not invent a thesis, explain why a
pattern should work, or decide that a result is trustworthy. An operator or an
outside model connected through MCP supplies those judgments; the local system
only executes explicit tools and returns evidence.

The main execution tools are:

- `collect_sp100_stock_bars` — external market-data collection; exact grant
  required for autonomous agents.
- `backtest_stock_edges` — deterministic local evaluation and Obsidian report.
- `trading_data_status` — read-only research history.
- `search_stock_signals` — bounded stock signal/trailing-stop search using
  training and validation before a one-time holdout reveal.
- `collect_sp100_options` — volatility/liquidity-filtered option snapshots.
- `collect_kalshi_markets` — Bitcoin 15-minute or weather market snapshots.
- `scan_market_edges` — deterministic options hypothesis ranking or Kalshi
  complete-set pricing checks; it never labels either result a validated edge.

The options collector remains secondary to the stock workflow. The Command Center exposes public Bitcoin
15-minute and weather collection plus a $10 virtual Kalshi ledger. A paper fill
must name its ticker, side, decision source, and rationale; the executor reads a
current public ask and later reconciles against Kalshi's official result. Nothing
in this subsystem can place, modify, or cancel a live order.

## Claude context and local SLM roles

Claude Desktop is configured for the compact `markets` MCP domain by default.
It receives four stable tools rather than all underlying schemas. It can discover
an exact schema when needed and invoke every preserved capability through the
same permission gateway. This reduces the initial tool payload from roughly
60,500 characters (141 tools) to roughly 2,100 characters (four tools).

`slm_assist` uses only a configured local provider for literal compression,
metric extraction, or request classification. Its output is always marked
unverified. The SLM cannot establish an edge, choose a trade, select parameters,
or reveal a holdout; Claude or the operator reasons over evidence while the
deterministic executors calculate and log results.

The paper ledger is stored at `trading_data/kalshi_paper/account.json` and mirrored
to `Trading Research/Kalshi Paper Experiment.md` in Obsidian.

## One-time Alpaca setup

Revoke and regenerate any Alpaca credential that has ever appeared in source.
Then stop the harness and run:

```powershell
Set-Location "$HOME\OneDrive\Documents\Scripts\Code\Agent"
.\START_PILOT.cmd trading
```

The prompts do not echo. Credentials are stored in the private managed-secret
store and passed only to the core process. Restart in the lean profile:

```powershell
.\STOP_ASSISTANT.cmd
.\START_TRADING_RESEARCH.cmd
```

`START_OVERNIGHT.cmd` now launches the same lean stock-research profile.

## Lean profile

Trading-research mode keeps the core, permission notifier, MCP bridge, system
monitor, and operations report. It does not launch the assistant worker, an
Ollama server, generic discovery, memory consolidation, autonomous
self-improvement scheduling, or DSH Web. An Ollama process that you started
separately is not stopped, but this profile does not call it. This saves model
context, RAM, and background CPU without deleting reversible capabilities from
source.

## What the first backtest means

The current candidate family contains 20-day momentum, trend pullback,
uptrend mean reversion, and 20-day breakout. Candidate selection uses only the
training segment; the selected rule is then scored on the later holdout.

The report says `PROMISING` only when the selected holdout has enough trades,
positive net expectancy after costs, profit factor above one, and a positive
trade score. That is a research lead—not permission to trade. Present-day
constituents introduce survivorship bias, and the simulator does not model
taxes, borrow constraints, market impact, or queue position. A promising result
must survive a later untouched period and paper trading.
