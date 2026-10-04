# Changelog

## Unreleased: runs, per-trade PnL and strategy attribution

- **Runs:** "Start new run" (named, confirmed) resets the dashboard to a new
  test without deleting anything. The Run menu switches to earlier runs or
  All time. A trade belongs to the run it was opened in.
- **Signals and deals are never deleted any more.** `RETENTION_DAYS` now
  only prunes the account heartbeats behind the equity chart.
- **Closed trades table** from MT5's deal history, grouped by position:
  open/close time, symbol, strategy, side, lots, entry, exit, pips, gross,
  commission + swap, net (green/red), duration and what closed it. Broker
  SL/TP closes are included.
- **New tiles:** expectancy (money and pips), average win and loss, profit
  factor, largest win and loss, max drawdown.
- **By pair / by strategy** tables and **filter chips** that filter the
  whole dashboard.
- **Download CSV:** `/api/export` gives `trades.csv` and `signals.csv` for
  the current run, pair and strategy.
- **Strategy attribution:** the bridge stores each alert's `strategy=<tag>`
  ("untagged" without one), puts the tag in the MT5 order comment, and asks
  the EA for a per-strategy magic number. `python -m app.backfill
  SYMBOL=tag` attributes older, untagged trades by pair.
- **EA v3.7:** tagged entries open with magic `MagicNumber × 1000 + slot`.
  The EA treats that whole family as its own (closes, modify, limit orders,
  reports), closes with the position's own magic, and adds magic and order
  comment to deal reports and the heartbeat. Untagged signals behave as
  before. Recompile, then remove the EA from the chart and attach it again.
  Older EAs ignore the new fields; attribution then relies on the entry
  signal and the order comment.

## Unreleased: dashboard follow-ups

- **EA v3.6:** close acks report the exit fill (volume-weighted across the
  closed legs) and the quote at send, so exits show fill price and slippage
  (closing a long: lower is adverse). Recompile the EA, then remove it from
  the chart and attach it again. Compiling alone doesn't reload an EA that's
  already running.
- **Equity chart:** the y-axis has a minimum span of 0.5% of equity,
  centred on the data.

## Unreleased: Pinebridge Dashboard

- **Dashboard** on the bridge's new LAN-only port 8081 (`DASHBOARD_PORT`,
  optional `DASHBOARD_PASSWORD`). It shows signals, fills, slippage against
  TradingView's price and the broker's quote, latency, win rate, net and
  expected PnL, and the equity curve. See [docs/dashboard.md](docs/dashboard.md).
- **EA v3.5**: execution reports back to the bridge on port 5556: a
  per-signal ack with `signal_id`, every deal (including broker SL/TP
  closes), and an account heartbeat. New inputs: `EnableReports`,
  `ReportAddress`, `HeartbeatSec`.
- **Bridge:** every webhook gets a `signal_id`, and signals, reports and
  account snapshots are stored in SQLite in `/config` (`RETENTION_DAYS`).
- **`/health`** adds `ea_last_report_seconds_ago`, `broker_connected` and
  `algo_trading_allowed`.
- **Optional `tv_price` / `tv_time` alert fields** for slippage and latency.

## EA v3.4

- Failed closes are retried after broker disconnects.
- Broker disconnect/reconnect notifications.
- Oldest-first (FIFO) closing.
- Readable text for errors 10045/10046.
- `modify` updates every position leg.
