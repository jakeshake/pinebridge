<p align="center"><img src="docs/icons/pinebridge.png" alt="Pinebridge" width="160"></p>

# Pinebridge

**TradingView alerts to MetaTrader 5, self-hosted.**

<a href="https://buymeacoffee.com/jakeshake"><img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=&slug=jakeshake&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy me a coffee" height="40"></a>

Tired of paying a monthly fee just to get your TradingView alerts into
MetaTrader 5? Or renting a VPS only so MT5 stays online around the clock?
If you have an Unraid server, you already own the hardware for both.
Pinebridge receives your TradingView strategy alerts and places the trades
in MT5, which runs in a Windows container on your own server. No
signal-relay subscription, no VPS bill, and your broker login stays on your
own server instead of with a third-party service.

> **This executes real trades on a real or demo account with no human in
> the loop.** Read the [risk notice](DISCLAIMER.md) before using this with a live
> account, and test on a demo account first.

## How it works

```
TradingView alert -> Cloudflare Tunnel -> pinebridge-bridge (Flask) -> ZeroMQ -> MT5 EA -> your broker
```

Three containers:

| Container | What it does |
|---|---|
| `pinebridge-tunnel` | Optional: exposes the webhook to the internet via a Cloudflare Tunnel — no port-forwarding. Skip it if you already run a tunnel. |
| `pinebridge-bridge` | Flask app: authenticates, parses, and translates TradingView alerts, then pushes them to MT5 over ZeroMQ |
| `pinebridge-mt5` | A Windows 11 VM ([dockur/windows](https://github.com/dockur/windows)) running MetaTrader 5 + the `TradingViewZeroMQExecutor` Expert Advisor |

The **Pinebridge Dashboard** (port 8081, LAN only) shows every signal and
fill, slippage against TradingView's price, latency, win rate, PnL and
equity. See [docs/dashboard.md](docs/dashboard.md).

See [docs/architecture.md](docs/architecture.md) for the full data-flow
diagram and the reasoning behind each piece.

## Quick start (Docker Compose)

1. `cp .env.example .env` and fill in the values (see comments in the file).
2. Set up a Cloudflare Tunnel per
   [docs/cloudflare-tunnel-setup.md](docs/cloudflare-tunnel-setup.md) and
   put the token in `.env`.
3. `docker compose up -d`
4. Watch `pinebridge-mt5` provision itself at `http://<host>:8006` (noVNC) —
   see [docs/mt5-ea-setup.md](docs/mt5-ea-setup.md) for what's automated
   and how to verify it worked.
5. Point a TradingView alert at your tunnel's `/webhook` URL using the
   format in [docs/tradingview-alert-format.md](docs/tradingview-alert-format.md).

## Quick start (Unraid)

See [docs/unraid-install.md](docs/unraid-install.md): add the templates with
one terminal command, then install the containers from **Docker > Add
Container**. Coming from the old `tv-mt5-bridge` names? See
[Moving from tv-mt5-bridge](docs/unraid-install.md#moving-from-tv-mt5-bridge-the-old-name).

## Support

Questions and help: the [Unraid forum support thread](https://forums.unraid.net/topic/200736-support-jakeshake-pinebridge-tradingview-alerts-%E2%86%92-metatrader-5/).
Bugs and feature requests: [GitHub issues](https://github.com/jakeshake/pinebridge/issues).

## Security

- The webhook requires a shared secret (`WEBHOOK_SECRET`), sent either in
  the webhook URL (`/webhook?secret=...`) or as a `secret=` field in the
  alert body — TradingView can't send custom headers, so this is the auth
  mechanism. Use the URL form for strategies that send messages from Pine
  `alert()` calls. **Don't disable this** for
  anything reachable from the internet.
- Public exposure goes through a Cloudflare Tunnel, not a forwarded port.
- No broker or Windows credentials are ever baked into the images — they're
  supplied via `.env` / Unraid template fields at runtime.

## Repo layout

- `signal-bridge/` — source of the `pinebridge-bridge` image (Flask + ZeroMQ webhook receiver)
- `mt5-windows/` — source of the `pinebridge-mt5` image, built on dockur/windows; `oem/` holds the
  provisioning scripts + EA source baked into it
- `unraid-templates/` — Unraid container templates (the ones listed in Community Applications)
- `extras/` — the optional `pinebridge-tunnel` template (`.template`, not `.xml`, so the CA scanner does not list it: CA already lists cloudflared)
- `docs/` — setup guides for each piece

## Status

Tested on real Unraid hardware with a Forex.com demo account:
- **Install:** a fresh first boot installs MT5, compiles the EA and
  attaches it.
- **Live alerts end to end:** market entries and exits, resting limit
  orders (arm/cancel), double-down adds and SL/TP moves, all from live
  TradingView alerts through a Cloudflare Tunnel.
- **Failure handling:** close retries after a broker disconnect, and
  FIFO-compliant closing on a US account.

See [docs/mt5-ea-setup.md](docs/mt5-ea-setup.md) for what was found along
the way. Issues and PRs welcome.

## Support the project

If Pinebridge saves you a VPS or a signal subscription, consider
[buying me a coffee](https://buymeacoffee.com/jakeshake).

<a href="https://buymeacoffee.com/jakeshake"><img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=&slug=jakeshake&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy me a coffee" height="40"></a>

## License

[MIT](LICENSE). Please also read the [automated trading risk notice](DISCLAIMER.md).
