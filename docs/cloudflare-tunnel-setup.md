# Cloudflare Tunnel Setup

TradingView only sends webhooks to a public HTTPS address, so pinebridge-bridge
needs one. A Cloudflare Tunnel provides it without opening router ports.
Pick one route:

- **A. You already run a Cloudflare tunnel** (e.g. a cloudflared container
  serving other apps): add a public hostname to that tunnel with service
  `http://<unraid-ip>:5080` (pinebridge-bridge's WebUI Port). There's nothing
  to install. Skip to step 5 below.
- **B. No tunnel yet (recommended)**: create one below, then run it with its
  token in either the `pinebridge-tunnel` template (added by the one-liner
  in [unraid-install.md](unraid-install.md); it lives in `extras/pinebridge-tunnel.template`) or any
  cloudflared app from Community Applications.
- **C. Just trying it out**: a quick tunnel needs no account or domain:
  ```
  docker run -d --name pinebridge-quick-tunnel cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://<unraid-ip>:5080
  docker logs pinebridge-quick-tunnel 2>&1 | grep trycloudflare.com
  ```
  **Test only**: the `https://....trycloudflare.com` URL changes whenever
  the container restarts, which silently breaks your TradingView alert.

Creating the tunnel happens in your Cloudflare account and can't be
automated by this repo. You need a free Cloudflare account with a domain
on it.

1. Go to the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/)
   > **Networks > Tunnels**.
2. **Create a tunnel**, choose "Cloudflared" as the connector.
3. Give it a name (e.g. `pinebridge`) and copy the **tunnel token** shown
   during setup -- this is a long string starting with `ey...`. Put it in
   your `.env` as `TUNNEL_TOKEN`.
4. Under **Public Hostname**, add a route:
   - Subdomain: anything you like, e.g. `tv-webhook`
   - Domain: a domain already on your Cloudflare account
   - Service type: `HTTP`
   - Service URL: `pinebridge-bridge:5000` with docker-compose, or
     `<your-unraid-ip>:5080` on Unraid (container names don't resolve on
     Unraid's default bridge network)
5. Save. Your public webhook URL is now
   `https://tv-webhook.yourdomain.com/webhook`.
6. In TradingView, use that full URL (with `/webhook`) plus your secret as
   the alert webhook URL:
   `https://tv-webhook.yourdomain.com/webhook?secret=YOUR_SECRET`.
   Set pinebridge-bridge's `PUBLIC_URL` to `https://tv-webhook.yourdomain.com`
   and its log prints this exact URL.

You do not need to open any ports on your router -- `cloudflared` makes an
outbound-only connection to Cloudflare's edge.

## Hardening

- Consider adding a Cloudflare **WAF rule** or **Access policy** scoped to
  the `/webhook` path if you want an extra layer beyond the shared secret.
- Rotate `WEBHOOK_SECRET` (and update your TradingView alerts) if you ever
  suspect it's leaked.
