# Installing with Docker (Linux, Proxmox, NAS, ...)

Unraid users: use the Apps tab instead, see [unraid-install.md](unraid-install.md).

Pinebridge runs anywhere Docker Engine runs **on an x86-64 Linux host with
KVM**. The MT5 side is a real Windows 11 VM
([dockur/windows](https://github.com/dockur/windows)) inside a container,
so the host must be able to run VMs. No KVM? See
[Bring your own MT5](#bring-your-own-mt5-windows-pc-vm-or-vps) at the end
of this page.

| Your setup | Route |
|---|---|
| Linux PC or server with Docker Engine (Debian, Ubuntu, Fedora, ...) | [Docker Compose](#docker-compose) |
| Proxmox VE | [Proxmox](#proxmox-ve): a Docker VM (recommended), an LXC container, or your own Windows VM |
| Docker Desktop on Windows or macOS, a Raspberry Pi / ARM box, or no KVM | [Bring your own MT5](#bring-your-own-mt5-windows-pc-vm-or-vps) (bridge in Docker, MT5 on Windows) |

> Tested so far on Unraid only. The Docker Compose route uses the same
> images and settings; Proxmox and bring-your-own-MT5 follow the same
> design but haven't been run end to end by the project yet. Reports in
> [issues](https://github.com/jakeshake/pinebridge/issues) are welcome.

## Docker Compose

### 1. Check the host

```bash
ls -l /dev/kvm            # must exist; if not, enable VT-x/AMD-V (SVM) in BIOS
uname -m                  # must be x86_64
docker compose version    # Docker Engine with the compose plugin
```

### 2. Get the files and fill in `.env`

```bash
mkdir pinebridge && cd pinebridge
curl -fsSLO https://raw.githubusercontent.com/jakeshake/pinebridge/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/jakeshake/pinebridge/main/.env.example
nano .env
```

Set `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (from your broker, **start with
a demo account**) and a `WINDOWS_PASSWORD`. Leave `WEBHOOK_SECRET` blank to
have one generated.

### 3. Start it

```bash
docker compose up -d
```

- Open `http://<host>:8006` to watch Windows install, then MT5. The first
  boot takes roughly 15-70 minutes, mostly the Windows download. Don't click
  around inside the VM while the setup script runs. It's done when MT5 is
  open with the EA on a chart and its **Experts** tab says
  `Waiting for signals from Flask...`.
- `docker compose logs pinebridge-bridge` prints the **TradingView alert
  setup** block: your webhook URL (with the generated secret) and the alert
  Message to use.
- The dashboard is at `http://<host>:8081`. Within about 30 seconds of MT5
  finishing it should say **EA reporting**.

### 4. Give TradingView a public HTTPS URL

TradingView only sends webhooks to a public `https://` address. Pick one:

- **Cloudflare Tunnel** (no open ports, works behind CGNAT): create a tunnel
  in the Cloudflare Zero Trust dashboard, put its token in `.env` as
  `TUNNEL_TOKEN`, and add a public hostname with service
  `http://pinebridge-bridge:5000`. Then run
  `docker compose --profile tunnel up -d`. Full walkthrough:
  [cloudflare-tunnel-setup.md](cloudflare-tunnel-setup.md).
- **A reverse proxy you already run** (Caddy, Traefik, Nginx Proxy Manager,
  ...): proxy an HTTPS hostname to `http://<host>:5080`.

Only ever expose the **webhook** port (5080). Never publish the dashboard
(8081), the EA ports (5555, 5556), noVNC (8006) or RDP (3389).

### 5. Point your TradingView alert at it

Webhook URL `https://<your-hostname>/webhook?secret=<secret>`, condition
**Order fills and alert() function calls**, and Message:

```
{{strategy.order.alert_message}},tv_price={{strategy.order.price}},tv_time={{timenow}}
```

Your strategy has to build its messages in the
[Pinebridge alert format](tradingview-alert-format.md).

### Updating

```bash
docker compose pull && docker compose up -d
```

The Windows VM and MT5 keep their state in `./data/windows`. A new EA
version inside the image only reaches an already-installed VM if you copy
and recompile it, see [mt5-ea-setup.md](mt5-ea-setup.md).

### If the dashboard says "EA silent"

The EA sends its reports to `tcp://172.17.0.1:5556`, which is the Docker
host on a standard Docker Engine install. On Podman, rootless Docker, or a
host without the `docker0` bridge, open the EA's inputs in MT5 (via noVNC)
and set `ReportAddress` to `tcp://<host LAN IP>:5556`.

## Proxmox VE

### Recommended: Docker inside a Linux VM

1. On the Proxmox host, check nested virtualization is on:
   `cat /sys/module/kvm_intel/parameters/nested` (or `kvm_amd`) should print
   `Y` or `1`. If not, add `options kvm-intel nested=1` (or
   `options kvm-amd nested=1`) to `/etc/modprobe.d/kvm.conf` and reboot.
2. Create a Debian/Ubuntu VM with **CPU type `host`**, at least 4 cores,
   12 GB RAM and 100 GB disk (it holds a whole Windows VM).
3. Inside it, install Docker Engine, check `ls /dev/kvm`, and follow
   [Docker Compose](#docker-compose) above.

### LXC container

Docker in an LXC can work, but the container needs the host's KVM and TUN
devices. In `/etc/pve/lxc/<id>.conf`, with `nesting=1` enabled:

```
lxc.cgroup2.devices.allow: c 10:232 rwm
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/kvm dev/kvm none bind,optional,create=file
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

This gives the container direct access to the host's KVM. The VM route
above is simpler and better isolated.

### Your own Windows VM

You can also make a normal Proxmox Windows VM, install MT5 there, and run
only the bridge in Docker. See the next section.

## Bring your own MT5 (Windows PC, VM or VPS)

Use this when the host can't run the `pinebridge-mt5` container (Docker
Desktop on Windows or macOS, ARM boards, no KVM), or when you'd rather use
an MT5 you already run. The bridge runs in Docker; MT5 and the EA run on any
Windows machine on the same network.

MetaTrader 5 for macOS isn't supported: the EA needs Windows DLLs.

1. **Install MT5** from your broker and log in (demo account first).
2. **ZeroMQ library**: download
   [ding9736/MQL5-ZeroMQ](https://github.com/ding9736/MQL5-ZeroMQ/archive/refs/heads/main.zip).
   In MT5, **File > Open Data Folder**, then copy `ZeroMQ.mqh` and the
   `Core` folder into `MQL5\Include\ZeroMQ\`, and the `Libraries\*.dll`
   files into `MQL5\Libraries\`.
3. Install the [Microsoft Visual C++ x64 redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe).
4. **The EA**: copy
   [`TradingViewZeroMQExecutor.mq5`](../mt5-windows/oem/TradingViewZeroMQExecutor.mq5)
   into `MQL5\Experts\`, open it in MetaEditor and compile (F7). It must say
   **0 errors**.
5. **Tools > Options > Expert Advisors**: tick **Allow Algo Trading** and
   **Allow DLL imports**.
6. Open a chart, drag the EA onto it, and in its **Inputs** set
   `ReportAddress` to `tcp://<Docker host IP>:5556`. On the **Dependencies**
   tab tick **Allow DLL imports**.
7. Let the bridge reach the EA through the Windows firewall (in an
   administrator PowerShell):
   ```powershell
   New-NetFirewallRule -DisplayName "MT5 ZeroMQ (Pinebridge)" -Direction Inbound -Protocol TCP -LocalPort 5555 -Action Allow
   ```
8. **Run only the bridge**: in `.env` set `ZMQ_HOST=<Windows machine IP>`,
   then `docker compose up -d pinebridge-bridge` (add `--profile tunnel`
   and `pinebridge-tunnel` if you use the tunnel). Docker Desktop users can
   run this on the same Windows PC: use its LAN IP as `ZMQ_HOST`.

ZeroMQ traffic between the bridge and the EA isn't encrypted. Keep both on
the same LAN. For a Windows VPS, connect it to your network with a VPN (for
example Tailscale or WireGuard) rather than opening port 5555 to the
internet.

The Windows PC has to stay on and logged in, with MT5 running, for trades
to go through.
