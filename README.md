# tv-mt5-bridge

Self-hosted TradingView → MetaTrader 5 bridge. Turns a TradingView alert
into a live trade on your own MT5 account, running entirely on your own
hardware (e.g. an Unraid box) instead of a paid third-party bridge service.

> **This executes real trades on a real or demo account with no human in
> the loop.** Read the [risk notice](LICENSE) before using this with a live
> account, and test on a demo account first.

## How it works

```
TradingView alert -> Cloudflare Tunnel -> signal-bridge (Flask) -> ZeroMQ -> MT5 EA -> your broker
```

Three containers:

| Container | What it does |
|---|---|
| `cloudflared` | Exposes the webhook to the internet via a Cloudflare Tunnel — no port-forwarding |
| `signal-bridge` | Flask app: authenticates, parses, and translates TradingView alerts, then pushes them to MT5 over ZeroMQ |
| `mt5-windows` | A Windows 11 VM ([dockur/windows](https://github.com/dockur/windows)) running MetaTrader 5 + the `TradingViewZeroMQExecutor` Expert Advisor |

See [docs/architecture.md](docs/architecture.md) for the full data-flow
diagram and the reasoning behind each piece.

## Quick start (Docker Compose)

1. `cp .env.example .env` and fill in the values (see comments in the file).
2. Set up a Cloudflare Tunnel per
   [docs/cloudflare-tunnel-setup.md](docs/cloudflare-tunnel-setup.md) and
   put the token in `.env`.
3. `docker compose up -d`
4. Watch `mt5-windows` provision itself at `http://<host>:8006` (noVNC) —
   see [docs/mt5-ea-setup.md](docs/mt5-ea-setup.md) for what's automated
   and how to verify it worked.
5. Point a TradingView alert at your tunnel's `/webhook` URL using the
   format in [docs/tradingview-alert-format.md](docs/tradingview-alert-format.md).

## Quick start (Unraid)

See [docs/unraid-install.md](docs/unraid-install.md) — add this repo as a
Community Applications template repository and install the three apps from
the **Apps** tab.

## Security

- The webhook requires a shared secret (`WEBHOOK_SECRET`) sent as a
  `secret=` field in the alert body — TradingView can't send custom
  headers, so this is the auth mechanism. **Don't disable this** for
  anything reachable from the internet.
- Public exposure goes through a Cloudflare Tunnel, not a forwarded port.
- No broker or Windows credentials are ever baked into the images — they're
  supplied via `.env` / Unraid template fields at runtime.

## Repo layout

- `signal-bridge/` — the Flask + ZeroMQ webhook receiver
- `mt5-windows/` — image built on dockur/windows; `oem/` holds the
  provisioning scripts + EA source baked into it
- `unraid-templates/` — Unraid container templates
- `docs/` — setup guides for each piece

## Status

The MT5 provisioning script (`mt5-windows/oem/setup-mt5.ps1`) has been run
end-to-end on real Unraid hardware: MT5 installs, the EA compiles and
auto-attaches, and it binds its ZeroMQ socket. See
[docs/mt5-ea-setup.md](docs/mt5-ea-setup.md) for what was found along the
way. Not yet tested: a completely fresh first boot of the packaged
`tv-mt5-bridge-windows` image straight from the Unraid template, and a live
TradingView → MT5 trade through the whole chain. Issues and PRs welcome.

## License

[MIT](LICENSE), plus a trading-risk notice — please read it.
