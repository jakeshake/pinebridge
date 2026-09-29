# TradingView Alert Format

Set your TradingView alert's "Message" field to a flat `key=value,...`
string (a JSON object body also works, with the same keys).

## Required for every alert

| Field | Meaning |
|---|---|
| `signal` | One of the signal types below |
| `symbol` | Instrument symbol, e.g. `EURUSD` |
| `secret` | Must match the `WEBHOOK_SECRET` you configured. May be left out of the body if the webhook URL carries it instead: `https://.../webhook?secret=YOUR_SECRET` |

## Entries: `signal=long` / `short` / `buy` / `sell`

| Field | Meaning |
|---|---|
| `qty` | Size in base-currency units (e.g. `100000` = 1.0 standard lot). Alternatively send `size` directly in lots. |
| `entry_price` | Price at signal time, used to convert `sl_price`/`tp_price` into pip distances |
| `sl_price`, `tp_price` | Absolute stop-loss / take-profit prices. Alternatively send `sl_pips`/`tp_pips` directly. |
| `comment` | Optional; tags the trade in MT5 |

```
signal=long,symbol=EURUSD,qty=100000,entry_price=1.0850,sl_price=1.0800,tp_price=1.0950,secret=YOUR_SECRET
```

## Exits

`signal=closelong` / `closeshort` / `close` / `closealllong` / `closeallshort`
/ `closeall` / `exitlong` / `exitshort` / `exit`

```
signal=closelong,symbol=EURUSD,secret=YOUR_SECRET
```

## Modify an open position's SL/TP

```
signal=modify,symbol=EURUSD,sl_price=1.0820,tp_price=1.0980,secret=YOUR_SECRET
```

## Resting limit orders

Rest a limit order at `limit_price`, or cancel the one resting for a zone.
The EA keeps at most one resting order per symbol and direction: a new arm
replaces the old one, and a cancel only removes the order whose `zone_id`
matches.

| Field | Meaning |
|---|---|
| `qty` | Size in base-currency units, as for entries |
| `limit_price` | Price to rest the order at (required for arms) |
| `sl_price`, `tp_price` | Optional absolute SL/TP, converted to pips from `limit_price` |
| `zone_id`, `zone_src` | The zone the order belongs to, e.g. `41821` / `OB` |

```
signal=armlong,symbol=EURUSD,qty=1000,limit_price=1.08250,tp_price=1.08650,zone_id=41821,zone_src=OB
signal=cancellong,symbol=EURUSD,zone_id=41821,zone_src=OB
```

`armshort` / `cancelshort` are the short-side equivalents. Set the EA input
`EnableLimitOrders=false` to ignore all four.

On entries, `dd=1` marks a double-down add and `zone_id` tags the zone.
Unless `dd=1`, the EA skips a `long`/`short` entry when a position opened
by a resting limit is already open in that direction (the resting order
already filled for that move), and otherwise deletes any resting limit in
that direction before placing the market order.

## IGT (ICT/GANN Hybrid) strategy alerts

The IGT Pine strategy builds every message itself, in the formats above,
so the alert needs no hand-written message:

1. On the chart running the strategy, create an alert with **Condition**
   set to the strategy and **Order fills and alert() function calls**.
2. **Message**: `{{strategy.order.alert_message}}`
3. **Webhook URL**: `https://your-tunnel-hostname/webhook?secret=YOUR_SECRET`.
   The secret has to be in the URL: the arm, cancel and modify messages
   come from Pine `alert()` calls, which TradingView sends exactly as the
   script built them, so nothing typed in the Message box reaches them.

Only an alert on the strategy itself, with that Message, sends bridge
signals. An alert on another script, or on a non-order condition (a price
crossing, a drawing), sends its own text or the literal
`{{strategy.order.alert_message}}`. The bridge rejects those with
`Alert has no signal= field` in its log.

The `symbol` field is TradingView's ticker (`syminfo.ticker`, e.g.
`EURUSD`), so it must match the MT5 symbol name exactly. The bridge has no
symbol mapping yet, so brokers that add a suffix (`EURUSD.a`, `EURUSDm`)
won't work with it as-is.

## Testing without TradingView

```bash
curl -X POST https://your-tunnel-hostname/webhook \
  -d "signal=long,symbol=EURUSD,qty=100000,entry_price=1.0850,sl_price=1.0800,tp_price=1.0950,secret=YOUR_SECRET"
```

Or use `GET /test` on pinebridge-bridge for a self-describing summary of
supported signals, and `POST /test` (optionally `?send=1`) to dry-run the
parser without touching the real `/webhook` route.

## A note on extending this

This schema is intentionally minimal. If your own strategy needs extra
fields (e.g. zone IDs, resting limit orders, regime tags), the parser
already passes unknown key=value fields through untouched -- you only need
to add a handler in `signal-bridge/app/translate.py` via
`translate.register_handler("your_signal_type", your_function)` rather
than modifying the generic path.
