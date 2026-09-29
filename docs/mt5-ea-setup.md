# MT5 + EA Setup (pinebridge-mt5 container)

The `pinebridge-mt5` image (`mt5-windows/Dockerfile`) is
[dockur/windows](https://github.com/dockur/windows) with this repo's
`mt5-windows/oem/` files baked in. dockur boots a real Windows VM via
KVM/QEMU, copies those files to `C:\OEM` during its unattended install, and
runs `install.bat` in a visible window at first logon (as the auto-logged-on
admin account). Its output goes to `C:\OEM\install.log`.

Container environment variables are **not** visible inside the Windows
guest, so the image's entrypoint (`mt5-windows/entry.sh`) writes the
`MT5_*` settings to `C:\OEM\mt5.env` before dockur builds the install image;
`setup-mt5.ps1` reads that file and deletes it. The broker password does
still end up in `$InstallDir\config\startup.ini`, which MT5 reads at every
launch. Note this only happens on the **first** boot of a fresh `/storage`
-- changing `MT5_*` later won't reach an already-installed VM (update the
login inside MT5 instead, or wipe `/storage` to reinstall).

## Prerequisites

- A host with `/dev/kvm` available (Unraid: enable virtualization in BIOS;
  the same requirement as running Unraid's own VMs).
- Your own Windows 11 license. dockur/windows can install from Microsoft's
  public evaluation image, but you're responsible for licensing it for
  anything beyond evaluation use.
- Your MT5 broker login, password, and server name (from your broker, not
  from this project).

## What gets automated (`mt5-windows/oem/setup-mt5.ps1`)

1. Downloads and installs the generic MetaQuotes MT5 terminal. It actually
   lands at the fixed `C:\Program Files\MetaTrader 5` location (see below
   for why), with a live fallback search if a future installer build ever
   changes that default.
2. Downloads the [ding9736/MQL5-ZeroMQ](https://github.com/ding9736/MQL5-ZeroMQ)
   library and places the headers/DLLs where the EA expects them, and
   installs the Visual C++ runtime those DLLs need (see below for why).
3. Copies in `TradingViewZeroMQExecutor.mq5` and its default `.set` preset,
   then compiles the EA headlessly via MetaEditor.
4. Writes a startup config (`$InstallDir\config\startup.ini`) that enables
   Algo Trading + DLL imports and attaches the EA to a chart, using
   `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`/`MT5_SYMBOL` (from `.env` or the
   Unraid template, via `C:\OEM\mt5.env` as described above).
5. Creates a Startup-folder shortcut so MT5 launches automatically on every
   boot with that config, in portable mode (so its MQL5 data folder is
   `$InstallDir\MQL5`, next to the binaries).

**Verified live across six separate runs** (2026-09-13/14, against a real
dockur/windows instance on Jake's Unraid box), the last one **fully
clean**: MT5 installed, the EA compiled, the startup ini auto-attached it
to a chart, and its own `[ZMQ]` log confirmed
`OK: ZeroMQ PULL socket bound to: tcp://*:5555` / `Waiting for signals
from Flask...` with zero errors in either the Journal or Experts tabs.
Seven concrete bugs were found and fixed along the way by actually
running this, plus an eighth on the first from-scratch Unraid install
(all since confirmed on a wiped reinstall):

- **`mt5setup.exe /auto` is not fully silent.** It still shows a
  license-agreement screen and a finish screen that each need a click. The
  script handles this with a SendKeys loop that depends on `install.bat`
  running elevated/as SYSTEM already (dockur/windows' normal first-logon
  context) -- SendKeys cannot dismiss an actual UAC *consent* prompt (that
  runs on the secure desktop). Confirmed working end-to-end when run
  unattended from an already-elevated shell (no UAC prompt in the way). If
  provisioning seems to hang, open the noVNC viewer and check for a stuck
  UAC dialog first.
- **The ding9736/MQL5-ZeroMQ repo's layout** is `Core/*.mqh` + `ZeroMQ.mqh`
  at the repo root, not the `Include/ZeroMQ/` layout its own README
  describes -- confirmed and fixed; verified the files land at
  `$InstallDir\MQL5\Include\ZeroMQ\ZeroMQ.mqh` and `\Core\*.mqh`.
- **`mt5setup.exe`'s `/dir=` argument is silently ignored.** No matter what
  path you pass, this installer build always puts terminal64.exe and
  metaeditor64.exe at `C:\Program Files\MetaTrader 5` (confirmed by reading
  the installer's own per-user data-folder `origin.txt`, which recorded
  that real path even though `/dir` asked for something else). The script
  no longer tries to redirect the install location -- it targets the real
  default path directly, with a one-time search fallback if that default
  ever changes in a future installer build.
- **`metaeditor64.exe` needs `/portable` too, not just the terminal
  launch.** The EA's `#include <ZeroMQ/ZeroMQ.mqh>` (angle brackets) always
  resolves against whatever MQL5 data folder the terminal/editor is
  currently assigned -- without `/portable` that's the normal `%AppData%`
  data folder, not `$InstallDir\MQL5\Include` where step 2 actually placed
  the ZeroMQ files. Compile failed with `error 106: file ... not found`
  even though the file genuinely existed, just not where MetaEditor was
  looking. Confirmed fixed -- the EA compiled and auto-attached correctly
  on the next run.
- **`libzmq.dll` needs the Visual C++ runtime, which a fresh Windows image
  doesn't have.** With everything else finally working, MT5's **Experts**
  log tab (not the main Journal -- check both) showed the EA loading, then
  immediately failing: `cannot load '...\libzmq.dll' [126]`. Both DLLs
  were verified present, correct size, right folder -- error 126 means a
  *dependency* of the DLL is missing, not the DLL itself. libzmq.dll is a
  native C++ build needing `vcruntime140.dll`/`msvcp140.dll`. Fixed by
  installing the official `vc_redist.x64.exe` before the EA ever tries to
  load it. **Confirmed fixed** -- the next run's Experts log showed the EA
  binding its PULL socket successfully with zero errors.
- **Binding the ZeroMQ socket triggers a Windows Firewall prompt** ("allow
  this app on public/private networks?") on first launch -- confirmed
  live, clicked through manually. Nothing here can click that unattended
  for real users, and if it's never answered the port may stay blocked
  for connections from outside the VM (i.e. from pinebridge-bridge). Added an
  inbound firewall rule for port 5555 ahead of time so Windows never needs
  to ask. **Confirmed** on the fresh Unraid install: pinebridge-bridge reached
  port 5555 with no prompt ever clicked.

- **A truly fresh VM has no MQL5 standard library until MT5's first run.**
  The first from-scratch install from the Unraid template compiled and
  attached the EA before that existed, so the attach failed ("not found
  from start config") and the EA had to be attached by hand. Earlier test
  runs had reused a VM where MT5 had already run once, which hid this. Now
  a warm-up run comes first, and `launch-mt5.ps1` (run at every logon)
  checks the EA is really listening on port 5555 and re-attaches it if not.
  The old Startup shortcut also re-ran the attach config on every boot,
  stacking another chart and EA copy per reboot. **Confirmed fixed**
  (2026-09-26): a wiped `/storage` reinstall from the Unraid template came
  up with the EA bound to port 5555 and no manual steps at all.

After first boot:

1. Open the noVNC viewer at `http://<host>:8006` to watch the Windows
   desktop directly.
2. Check `C:\OEM\provision.log` and `C:\OEM\compile.log` for errors.
3. Confirm in the terminal itself (**Tools > Options > Expert Advisors**)
   that "Allow Algo Trading" and "Allow DLL imports" are both checked, and
   that the EA shows a green face icon on its chart (not a red X).
4. Check the **Experts** tab at the bottom of the terminal (not just
   Journal) -- that's where the EA's own ZeroMQ startup log and any DLL
   load errors actually show up.

### Orders fail with `10027 - auto trading disabled by client`

Logging in to a different broker account (File > Login to Trade Account)
can reset the chart workspace. The EA re-attaches, but its own
**Common > Allow Algo Trading** box can end up unticked, and **Allow DLL
imports** can reset too, while the toolbar's Algo Trading button still
shows as on. The EA then starts, but every order is rejected with
`10027`. Fix: double-click the EA's name in the chart's top-right corner
(or right-click the chart > Expert list > Properties), tick both boxes on
the **Common** tab and click OK. Also re-check **Tools > Options > Expert
Advisors**.

### Broker disconnects

While MT5 is disconnected from the broker, every trade request fails
(`10031 - no connection`). The EA logs `WARNING: ... lost connection` and
`OK: ... reconnected` in the Experts tab when this happens, and pushes the
same message to your phone if you've set a MetaQuotes ID under
**Tools > Options > Notifications** (input `NotifyConnection`).

A close that fails for any reason is queued and retried every
`CloseRetrySec` seconds while connected, until it succeeds, the position
disappears (hit its broker SL/TP), or `CloseRetryMaxMin` passes. Only the
exact tickets that failed are retried. Entries, arms and modifies are not
retried: they would be stale by the time the connection returns.

US accounts (e.g. Forex.com US) follow NFA FIFO rules, even when MT5
reports a hedging account:

- `10045 - FIFO close rule`: positions on a symbol must be closed oldest
  first. The EA closes (and retries) oldest-first, so this should only
  appear if something else holds an older position on the same symbol.
- `10046 - hedge prohibited`: the account refuses a position opposite an
  open one. It usually means an earlier close failed and the old position
  is still open.

### If you're troubleshooting manually via noVNC

The noVNC session's keyboard forwarding drops the Shift modifier for
typed/pasted keystrokes in at least this setup -- `:`, uppercase letters,
and other shifted characters can arrive as their unshifted equivalent (e.g.
`:` becomes `;`, `|` becomes `\`). PowerShell commands are case-insensitive
so lowercase-only commands still work, but avoid typing colons or pipes
directly; instead `cd \` to root then use relative paths, and use the
noVNC sidebar's clipboard panel (or open Chrome inside the VM and use
"Save As" / "Open in Terminal" to avoid typing paths at all) rather than
typing a full command with special characters at the console.

## Manual fallback

If the automated startup config doesn't take effect, do this once by hand
inside the VM (via the noVNC viewer or RDP on port 3389):

1. Open MT5, log into your broker account (Login/Password/Server).
2. **Tools > Options > Expert Advisors**: check "Allow Algo Trading" and
   "Allow DLL imports".
3. Open a chart for your symbol, drag `TradingViewZeroMQExecutor` from the
   Navigator's Expert Advisors list onto it, load
   `MQL5/Presets/TradingViewZeroMQExecutor.set` in the Inputs tab.
4. Confirm "Algo Trading" is toggled on in the toolbar.

## Networking

pinebridge-bridge connects to `ZMQ_HOST:ZMQ_PORT` (default `pinebridge-mt5:5555`).
With the default Docker bridge network in `docker-compose.yml`, compose's
built-in DNS resolves `pinebridge-mt5` to the right container automatically --
no extra networking setup needed for a single-host deployment.
