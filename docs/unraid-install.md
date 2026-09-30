# Installing on Unraid

This repo ships Unraid container templates in `unraid-templates/`, so you
fill in a form instead of writing `docker run` commands. Nothing else needs
downloading -- the images carry all the provisioning files.

## 1. Add the templates

Open a terminal on Unraid (the `>_` icon, top right) and run:

```bash
cd /boot/config/plugins/dockerMan/templates-user && for t in mt5 bridge tunnel; do wget -qO "my-pinebridge_$t.xml" "https://raw.githubusercontent.com/jakeshake/pinebridge/main/unraid-templates/pinebridge-$t.xml"; done
```

Safe to re-run to pick up template updates. (Unraid saves each container's
filled-in settings separately as `my-<container name>.xml`, so these file
names deliberately don't match any container name -- otherwise re-running
this would wipe your saved settings.)

(Community Applications' "template repositories" setting isn't present in
current CA releases, so this is the supported way to add third-party
templates until these are listed in CA itself.)

## 2. Install the containers

Go to **Docker > Add Container**, pick each `pinebridge_...` template from
the **Template** dropdown, fill it in, and hit Apply:

1. **pinebridge-mt5** -- your broker's `MT5_LOGIN` / `MT5_PASSWORD` /
   `MT5_SERVER`, and check `RAM_SIZE` / `CPU_CORES` fit your hardware.
   Change the Windows `PASSWORD` under *Show more settings*. Needs
   virtualization enabled in BIOS.
2. **pinebridge-bridge** -- the defaults work as they are:
   - `ZMQ_HOST` defaults to `172.17.0.1`, which is this server as seen from
     a container on Unraid's default bridge network, where pinebridge-mt5
     publishes the EA's port 5555.
   - Leave `WEBHOOK_SECRET` blank and one is generated on first start and
     saved as `webhook_secret` in the container's Config path
     (`/mnt/user/appdata/pinebridge-bridge/config`). The first start's
     log prints the full TradingView webhook URL with it.
   - Optionally set `PUBLIC_URL` to your tunnel's https address so that
     URL is exact.
   - The webhook listens on host port **5080**. Change **WebUI Port** if
     something else already uses it, and use your port everywhere below.
3. **pinebridge-tunnel** (optional) -- only if you don't already run a
   Cloudflare tunnel. See [cloudflare-tunnel-setup.md](cloudflare-tunnel-setup.md)
   for the three ways to get a public HTTPS URL.

## 3. Wait for first boot

pinebridge-mt5 downloads and installs Windows, then MT5, by itself -- anywhere
from about 15 minutes to over an hour the first time, mostly depending on
how fast the Windows download is. Don't click around inside the VM while
it's working; the setup script drives some installer windows itself. Watch it from its WebUI (port 8006). It's done when
MT5 is open with the EA attached to a chart and the **Experts** tab at the
bottom shows `Waiting for signals from Flask...`. See
[mt5-ea-setup.md](mt5-ea-setup.md) if it doesn't get there.

## 4. Verify

- `http://<unraid-ip>:5080/health` shows `"status": "healthy"`.
- pinebridge-bridge's log (Docker tab > pinebridge-bridge > Logs) shows the
  **TradingView alert setup** block: the Webhook URL and the Message to
  paste into your alert.
- Send a test alert per
  [tradingview-alert-format.md](tradingview-alert-format.md) and check it
  shows up in pinebridge-bridge's log (Docker tab > pinebridge-bridge > Logs) and in
  MT5's Experts tab.

## Moving from tv-mt5-bridge (the old name)

The project was renamed to Pinebridge. The images, templates and default
appdata paths now use `pinebridge-*` names:

| Before | Now |
|---|---|
| `ghcr.io/jakeshake/tv-mt5-bridge-windows` | `ghcr.io/jakeshake/pinebridge-mt5` |
| `ghcr.io/jakeshake/tv-mt5-bridge-signal-bridge` | `ghcr.io/jakeshake/pinebridge-bridge` |
| containers `mt5-windows` / `tv-mt5-signal-bridge` / `tv-mt5-cloudflared` | `pinebridge-mt5` / `pinebridge-bridge` / `pinebridge-tunnel` |

Old image names don't redirect. They are still published alongside the new
ones for a transition period, so an existing install keeps updating, but
switch when convenient:

1. Edit each container and set **Repository** to the new image. You can
   also change **Name**, but that's optional.
2. **Keep your existing appdata paths.** Leave the mt5 container's
   **Storage** path on your current folder (e.g.
   `/mnt/user/appdata/mt5-windows/storage`). If you point it at the new
   default folder, it starts with an empty disk and installs Windows from
   scratch. The bridge's Logs path can stay too. If you add the new
   **Config** path, a generated secret isn't used while `WEBHOOK_SECRET`
   is set.
3. Nothing changes on the TradingView side: the webhook URL (tunnel
   hostname, port and secret) stays the same.
4. Optional: remove the old downloaded templates from the Template
   dropdown with
   `rm /boot/config/plugins/dockerMan/templates-user/my-tv-mt5-bridge_*.xml`.
   These are only the downloaded templates; your saved container settings
   are in `my-<container name>.xml` and aren't touched.

## Updating

Unraid's **Apply** reuses the image it already has for a `:latest` tag. To
pick up a new release, use **Check for Updates** / **Update** on the Docker
tab, or **Force Update** in the container's menu (Advanced View). Otherwise
the container can be recreated on an old image. This happened once after an
update: the bridge ran a day-old image that rejected the webhook secret
with 401 until a forced update. To stay on a known build, set
**Repository** to a specific tag: every commit is published as
`ghcr.io/jakeshake/pinebridge-bridge:<commit sha>`.

## Troubleshooting

Stuck? Ask in the [Unraid forum support thread](https://forums.unraid.net/topic/200736-support-jakeshake-pinebridge-tradingview-alerts-%E2%86%92-metatrader-5/). Include the
pinebridge-bridge log (with your webhook secret removed) and the MT5
**Experts** tab.

- **Every order fails with `10027 - auto trading disabled by client`**
  after switching MT5 accounts: the EA's own Allow Algo Trading / Allow DLL
  imports boxes reset. See [mt5-ea-setup.md](mt5-ea-setup.md).
- **`10045` / `10046` on a US account** (e.g. Forex.com US): NFA FIFO
  rules. The EA closes oldest-first and explains both codes in its log;
  see [mt5-ea-setup.md](mt5-ea-setup.md).
- **Bridge log says `Alert has no signal= field`**: the alert isn't on the
  strategy with Message `{{strategy.order.alert_message}}`. See
  [tradingview-alert-format.md](tradingview-alert-format.md).

Prefer one compose file? Unraid's **Compose Manager** plugin can run this
repo's `docker-compose.yml` instead of the three templates.
