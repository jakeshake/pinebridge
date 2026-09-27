# Cloudflare Tunnel Setup

This step happens in your Cloudflare account/dashboard and can't be
automated by this repo -- you need your own (free) Cloudflare account and a
domain added to it.

1. Go to the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/)
   > **Networks > Tunnels**.
2. **Create a tunnel**, choose "Cloudflared" as the connector.
3. Give it a name (e.g. `tv-mt5-bridge`) and copy the **tunnel token** shown
   during setup -- this is a long string starting with `ey...`. Put it in
   your `.env` as `TUNNEL_TOKEN`.
4. Under **Public Hostname**, add a route:
   - Subdomain: anything you like, e.g. `tv-webhook`
   - Domain: a domain already on your Cloudflare account
   - Service type: `HTTP`
   - Service URL: `signal-bridge:5000` with docker-compose, or
     `<your-unraid-ip>:5000` on Unraid (container names don't resolve on
     Unraid's default bridge network)
5. Save. Your public webhook URL is now
   `https://tv-webhook.yourdomain.com/webhook`.
6. In TradingView, use that full URL (with `/webhook`) plus your secret as
   the alert webhook URL:
   `https://tv-webhook.yourdomain.com/webhook?secret=YOUR_SECRET`.

You do not need to open any ports on your router -- `cloudflared` makes an
outbound-only connection to Cloudflare's edge.

## Hardening

- Consider adding a Cloudflare **WAF rule** or **Access policy** scoped to
  the `/webhook` path if you want an extra layer beyond the shared secret.
- Rotate `WEBHOOK_SECRET` (and update your TradingView alerts) if you ever
  suspect it's leaked.
