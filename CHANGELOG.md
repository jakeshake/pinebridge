# Changelog

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
