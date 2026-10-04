# Pinebridge Dashboard

A web page served by pinebridge-bridge that shows what actually happened
to your TradingView alerts:

![The Pinebridge Dashboard after a night of live demo-account alerts](screenshots/dashboard.png)


- **Runs**: "Start new run" resets the stats to a named test (e.g. "SFLOW v2
  test") without deleting anything. See [Runs](#runs).
- **Filter chips** for pair and strategy, applying to the whole page.
- **KPI tiles**: net PnL, win rate, expectancy (per trade, in money and
  pips), profit factor, average win and loss, largest win and loss, max
  drawdown, signals, market fill rate, average slippage (vs TradingView and
  vs the broker's quote), average latency, equity, and the cost of execution.
- **Equity curve**, from the EA's account heartbeat. The y-axis always
  spans at least 0.5% of equity, so a few cents of drift look flat instead of
  filling the chart. With a pair or strategy chip selected it shows that
  slice's cumulative net PnL instead (account equity can't be split).
- **Slippage distribution** of market entries against TradingView's price.
- **By pair / by strategy** tables: trades, win %, net PnL, expectancy,
  profit factor, average entry slippage vs TradingView, and open positions.
  Click a row to filter.
- **Open positions**, **closed trades** and **recent signals**. Each signal
  shows its status (sent, executed, failed, rejected, not delivered) and the
  EA's result or error code, e.g. `10031 - no connection` or
  `10045 - FIFO close rule`.
- **Download CSV**: a zip with `trades.csv` and `signals.csv` for the
  current run, pair and strategy.

## Opening it

Unraid: **Docker > pinebridge-bridge > WebUI**, which opens
`http://<unraid-ip>:8081/`. Docker Compose: `http://<host>:8081/`.

**Keep it on your LAN.** The dashboard shows account data (balance, equity,
positions), so it runs on its own port (`DASHBOARD_PORT`, default 8081),
never the webhook port. Your Cloudflare tunnel points at the webhook port
only. **Don't** add a public hostname for 8081. Set `DASHBOARD_PASSWORD` to
require a password (any username) if other people use your network.

The page is fully self-contained: no CDN, fonts or external scripts.

## How the data gets there

```
TradingView --webhook--> bridge --signal (with signal_id)--> EA
                           ^                                  |
                           +----- execution reports ----------+
                                 (ZeroMQ, port 5556)
```

- The bridge gives every webhook a `signal_id`, passes it to the EA, and
  records the signal in SQLite (`pinebridge.db` in the bridge's Config
  folder).
- The EA (v3.5+) connects back to the bridge's report port (`ReportAddress`
  input, default `tcp://172.17.0.1:5556`) and sends three kinds of report:
  - **ack**: the result of each signal, echoing its `signal_id`, with the
    order ticket, requested and fill price, volume and retcode.
  - **deal**: every deal on the EA's magic number, including SL/TP closes
    done by the broker.
  - **account**: every `HeartbeatSec` (30 s): balance, equity, margin,
    broker connection, algo-trading permission, open positions.
- Reporting never holds up trading. The EA sends without waiting and drops
  reports if the bridge is unreachable.
- Signals and deals are never deleted. `RETENTION_DAYS` (default 90) only
  prunes the account heartbeats behind the equity chart, at startup.

`/health` on the webhook port also shows `ea_last_report_seconds_ago`,
`broker_connected` and `algo_trading_allowed`. It shows no account data.

### EA report address

`tcp://172.17.0.1:5556` works when pinebridge-mt5 and pinebridge-bridge run
on the same Unraid server with default networking. If they don't, open the
EA's properties (double-click its name on the chart) and set
**ReportAddress** to `tcp://<bridge server LAN IP>:5556`.

With Docker Compose, the Windows VM can't resolve container names, so use
the Docker host's LAN IP.

## What the numbers mean

- **Slippage vs TV** = fill price minus TradingView's price, signed so that
  **positive is adverse** (it cost you) and negative is favorable, for both
  buys and sells. It's shown in pips and in account currency, using the
  broker's tick value. TradingView's price comes from TradingView's data
  feed, not your broker's quotes, so this includes the difference between
  the two feeds as well as real slippage. See the `tv_price` field in
  [tradingview-alert-format.md](tradingview-alert-format.md).
- **Broker slippage** = fill minus the price MT5 quoted when the EA sent the
  order. This is pure execution slippage.
- **Exits** (`closelong`/`closeshort`, EA v3.6+) are measured the same way,
  against the alert's `close_price` (or `tv_price`). The fill is the
  volume-weighted close price of all the legs closed. Closing a long is a
  sell, so filling lower is adverse; closing a short is a buy. Exit slippage
  appears in the signals table. The histogram and the "Avg slippage" tiles
  stay entries-only. A bare `close` (both directions) isn't measured.
- **TV → bridge** needs `tv_time` in the alert. It includes TradingView's
  own alert delay, usually a second or more.
- **Bridge → EA** is the round trip from pushing the signal until the EA
  reported the result, including the broker's execution time.
- **Expected PnL** prices each closed trade at TradingView's entry and exit
  prices. **Cost of execution** = expected − actual PnL (actual includes
  commission and swap). A trade closed by the broker's own SL/TP has no
  exit signal, so it isn't counted here.
- **Market fill rate** = market entries (`long`/`short`) that filled ÷
  market entries received. Resting limits are left out, because most of
  them are cancelled by design.
- **Win rate** = closed positions with positive net PnL ÷ closed positions.
- **Closed trades** come from MT5's deal history, grouped by position ID:
  entry and exit are the volume-weighted prices of the opening and closing
  deals, net = gross + commission + swap. Trades the broker closed itself
  (SL, TP, stop out) are included; their "Closed by" says so.
- **Expectancy** = average net PnL per closed trade (and average pips).
  **Average win/loss** leave out breakeven trades. **Profit factor** = gross
  wins ÷ gross losses ("–" until there's a losing trade). **Max drawdown**
  is the largest peak-to-trough fall of cumulative net PnL over the closed
  trades in view, in close order.

## Runs

**Start new run** asks for a name and starts a run from now. From then on
every tile, chart and table shows that run by default. The **Run** menu
switches to an earlier run or to **All time**.

- Nothing is deleted. A run only sets the dashboard's time window.
- A trade belongs to the run it was **opened** in, so a position carried
  over from the previous test doesn't count toward the new one.
- Before the first run, the dashboard shows everything.
- The run, pair and strategy you're looking at are kept in the page URL, so
  a bookmark reopens the same view.

## Strategies

Pine scripts send `strategy=<tag>` on every message (see
[the alert format](tradingview-alert-format.md#strategy-tag-strategytag)).
The bridge stores it with each signal and works out each trade's strategy
in this order:

1. the entry signal's tag, matched to the position by order ticket;
2. the magic number on the position's opening deal (EA v3.7+ opens tagged
   orders with `MagicNumber × 1000 + slot`), which also covers trades whose
   entry ack never reached the bridge;
3. the order comment on the opening deal;
4. a backfill rule (below);
5. otherwise "untagged".

### Backfilling older trades

Trades from before the scripts sent a tag show as "untagged". If you know
which strategy ran on each pair, add a rule per pair:

```
docker exec -u bridge pinebridge-bridge python -m app.backfill EURUSD=igt AUDUSD=sflow-v2
docker exec -u bridge pinebridge-bridge python -m app.backfill --list
```

Each rule applies to untagged trades and signals on that symbol from before
`--before` (default: now). It's stored as a rule, so the raw signals stay as
received, and a tag in the data itself always wins.

## Download

**Download CSV** (`/api/export`) gives a zip with two files, for the run,
pair and strategy you're looking at:

- `trades.csv`: one row per closed trade: times (UTC), symbol, strategy,
  side, lots, entry, exit, pips, gross, commission, swap, net, exit reason,
  expected PnL at TradingView's prices, entry slippage, signal IDs.
- `signals.csv`: the raw signal log, every column the bridge stores, plus
  the resolved strategy.

Text cells that start like a spreadsheet formula are prefixed with `'`.

## Settings (pinebridge-bridge)

| Variable | Default | |
|---|---|---|
| `DASHBOARD_PORT` | `8081` | `0` turns the dashboard off |
| `DASHBOARD_PASSWORD` | empty | basic-auth password, any username |
| `REPORT_PORT` | `5556` | where the EA sends reports; `0` turns them off |
| `RETENTION_DAYS` | `90` | days of account heartbeats (equity chart) to keep; `0` keeps everything. Signals and deals are never pruned |

EA inputs: `EnableReports`, `ReportAddress`, `HeartbeatSec`.

If either dashboard port is already in use, the bridge logs it and keeps
the webhook running. The dashboard is never allowed to stop trading.
