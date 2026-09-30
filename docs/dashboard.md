# Pinebridge Dashboard

A web page served by pinebridge-bridge that shows what actually happened
to your TradingView alerts:

- **KPI tiles**: signals today and in total, market fill rate, win rate,
  net PnL, average slippage (vs TradingView and vs the broker's quote),
  average latency, equity, and the cost of execution.
- **Equity curve**, from the EA's account heartbeat.
- **Slippage distribution** of market entries against TradingView's price.
- **Open positions** and **recent signals**. Each signal shows its status
  (sent, executed, failed, rejected, not delivered) and the EA's result or
  error code, e.g. `10031 - no connection` or `10045 - FIFO close rule`.

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
- `RETENTION_DAYS` (default 90) prunes old history at startup.

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

## Settings (pinebridge-bridge)

| Variable | Default | |
|---|---|---|
| `DASHBOARD_PORT` | `8081` | `0` turns the dashboard off |
| `DASHBOARD_PASSWORD` | empty | basic-auth password, any username |
| `REPORT_PORT` | `5556` | where the EA sends reports; `0` turns them off |
| `RETENTION_DAYS` | `90` | `0` keeps everything |

EA inputs: `EnableReports`, `ReportAddress`, `HeartbeatSec`.

If either dashboard port is already in use, the bridge logs it and keeps
the webhook running. The dashboard is never allowed to stop trading.
