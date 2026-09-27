# Installing on Unraid

This repo ships Unraid container templates in `unraid-templates/`, so you
fill in a form instead of writing `docker run` commands. Nothing else needs
downloading -- the images carry all the provisioning files.

## 1. Add the templates

Open a terminal on Unraid (the `>_` icon, top right) and run:

```bash
cd /boot/config/plugins/dockerMan/templates-user && for t in mt5-windows signal-bridge cloudflared; do wget -qO "my-tv-mt5-bridge_$t.xml" "https://raw.githubusercontent.com/jakeshake/tv-mt5-bridge/main/unraid-templates/$t.xml"; done
```

Safe to re-run to pick up template updates. (Unraid saves each container's
filled-in settings separately as `my-<container name>.xml`, so these file
names deliberately don't match any container name -- otherwise re-running
this would wipe your saved settings.)

(Community Applications' "template repositories" setting isn't present in
current CA releases, so this is the supported way to add third-party
templates until these are listed in CA itself.)

## 2. Install the three containers

Go to **Docker > Add Container**, pick each `tv-mt5-bridge_...` template from
the **Template** dropdown, fill it in, and hit Apply:

1. **mt5-windows** -- your broker's `MT5_LOGIN` / `MT5_PASSWORD` /
   `MT5_SERVER`, and check `RAM_SIZE` / `CPU_CORES` fit your hardware.
   Change the Windows `PASSWORD` under *Show more settings*. Needs
   virtualization enabled in BIOS.
2. **tv-mt5-signal-bridge** -- `ZMQ_HOST` = your Unraid server's LAN IP,
   `WEBHOOK_SECRET` = a long random string (`openssl rand -hex 32` in the
   Unraid terminal makes one). If host port 5000 is already taken (Frigate
   and others use it), change **WebUI Port** to a free one like 5002 and
   use that port everywhere below. Note Unraid's Apply log prints the
   docker command with the secret visible -- clear that screen when done.
3. **tv-mt5-cloudflared** -- `TUNNEL_TOKEN` from
   [cloudflare-tunnel-setup.md](cloudflare-tunnel-setup.md). Point the
   tunnel's public hostname at `http://<unraid-ip>:5000` (or your chosen
   port).

## 3. Wait for first boot

mt5-windows installs Windows and then MT5 by itself -- roughly 20-40
minutes the first time. Watch it from its WebUI (port 8006). It's done when
MT5 is open with the EA attached to a chart and the **Experts** tab at the
bottom shows `Waiting for signals from Flask...`. See
[mt5-ea-setup.md](mt5-ea-setup.md) if it doesn't get there.

## 4. Verify

- `http://<unraid-ip>:5000/health` shows `"status": "healthy"`.
- Send a test alert per
  [tradingview-alert-format.md](tradingview-alert-format.md) and check it
  shows up in signal-bridge's log (Docker tab > signal-bridge > Logs) and in
  MT5's Experts tab.

Prefer one compose file? Unraid's **Compose Manager** plugin can run this
repo's `docker-compose.yml` instead of the three templates.
