# Provisions MT5 + the ZeroMQ EA inside the dockur/windows VM.
#
# NOTE ON CONFIDENCE: run live six times against a real dockur/windows
# instance (Jake's Unraid box, 2026-09-13/14), each run fixing a real bug
# the previous one exposed. Run 6 was FULLY CLEAN end to end: MT5
# installed, the EA compiled, the startup ini auto-attached it to a chart,
# and its own [ZMQ] log confirmed "OK: ZeroMQ PULL socket bound to:
# tcp://*:5555" / "Waiting for signals from Flask..." with zero errors in
# either the Journal or Experts tabs. The firewall rule below (bug #7) is
# the one thing added after that clean run and hasn't itself been
# re-verified live, since Windows only prompts for it once per install:
#   1. mt5setup.exe's /auto flag does NOT make it fully silent -- it still
#      shows a license-agreement screen and a finish screen that each need
#      a click. First fix (tracking Start-Process's PID) was WRONG: under
#      UAC elevation that PID is the non-elevated launcher stub, which
#      exits as soon as it hands off to the real installer, so the script
#      sailed past the dialog without clicking it. Fixed to poll for the
#      setup window by title and for terminal64.exe's existence instead --
#      CONFIRMED working when run unattended from an already-elevated
#      shell with no UAC prompt in the way, which is meant to mirror
#      install.bat's normal SYSTEM/elevated execution context.
#   2. The ding9736/MQL5-ZeroMQ repo's actual layout is Core/*.mqh +
#      ZeroMQ.mqh at the repo root (the library's own README describes an
#      older "ZeroMQ folder" layout that no longer matches) -- fixed and
#      CONFIRMED (files land at $InstallDir\MQL5\Include\ZeroMQ\ZeroMQ.mqh
#      and \Core\*.mqh).
#   3. BIG one: mt5setup.exe's /dir= argument is silently IGNORED -- it
#      always installs to the fixed "C:\Program Files\MetaTrader 5"
#      location below regardless of what /dir says (confirmed via the
#      installer's own per-user data-folder origin.txt, which recorded
#      that exact path even though /dir asked for C:\MT5). $InstallDir is
#      no longer a folder this script invents; it's the real install
#      location, found dynamically if the default ever changes.
#   4. Another one: metaeditor64.exe, run WITHOUT /portable, resolves the
#      EA's #include <ZeroMQ/ZeroMQ.mqh> (angle brackets always resolve
#      against the terminal's *assigned data folder*, never against
#      $InstallDir directly) against the normal %AppData% data folder --
#      not $InstallDir\MQL5\Include, where step 2 actually put the ZeroMQ
#      files. Compile failed with "error 106: file ... not found" even
#      though the file genuinely existed, just not where MetaEditor was
#      looking. Fixed by adding /portable to the compile invocation too,
#      matching step 6's already-portable terminal64 launch.
#   5. The installer's "Finish" screen auto-launches its own non-portable
#      terminal64 (using %AppData%, not portable mode) and opens a browser
#      to an MQL5.com registration page. The stray terminal64 is killed
#      below before our own portable launch; the browser tab is harmless
#      and left alone.
#   6. On run 4, the startup ini DID successfully auto-attach the EA
#      (Journal: "expert TradingViewZeroMQExecutor (EURUSD,M1) loaded
#      successfully") -- real progress, steps 1-5 all confirmed working.
#      But MT5's Experts log then showed it immediately failing to
#      initialize and getting removed: "cannot load '...\libzmq.dll'
#      [126]" (both DLLs verified present, correct size, right folder --
#      error 126 is Windows' "a dependency of this DLL is missing", not
#      "file not found"). libzmq.dll is a native C++ build needing the
#      Visual C++ runtime, which a fresh Windows image doesn't ship with.
#      Fixed by installing the official redistributable before the EA
#      ever tries to load it -- CONFIRMED fixed on run 6 (see above).
#   7. Binding the ZeroMQ PULL socket triggers a "Windows Security --
#      allow this app on public/private networks?" firewall prompt on
#      first launch (clicked through manually during run 6). Nothing here
#      can click that unattended for real users, and if it's never
#      answered the port may stay blocked for connections from outside
#      the VM. Added an inbound firewall rule for port 5555 ahead of time
#      so Windows never needs to ask -- CONFIRMED on the fresh-install run
#      (port reachable from outside the VM with nobody clicking anything).
#   8. The first fully fresh install from the Unraid template (2026-09-26)
#      got MT5, the ZeroMQ library and the EA in place but did NOT attach
#      the EA: a brand-new VM has no MQL5 standard library until MT5's first
#      portable run deploys it, so the compile failed and the start-config
#      attach raced that first run's full recompile. Earlier runs reused a
#      VM where MT5 had already run, which hid this. Fixed with a warm-up
#      run before compiling, plus launch-mt5.ps1, which checks the EA is
#      actually listening and re-attaches if not. The old Startup shortcut
#      also passed the attach config on every boot, stacking a new chart +
#      EA copy per reboot (only one can bind the port) -- the launcher only
#      uses it when needed. Not yet re-verified on a fresh install.
# Watch provision.log and the Experts tab (not just the main Journal) on
# first boot, and confirm Algo Trading / DLL imports end up enabled
# (Tools > Options > Expert Advisors inside the terminal) before trusting
# this fully unattended. See docs/mt5-ea-setup.md for the manual fallback
# steps.

$ErrorActionPreference = "Stop"

# CONFIRMED LIVE (third run, 2026-09-13): mt5setup.exe's /dir= argument is
# silently IGNORED by this installer build -- it always installs the
# terminal64.exe/metaeditor64.exe binaries to the fixed default location
# below regardless of what /dir says (verified via the installer's own
# data-folder origin.txt, which recorded this exact path). The original
# script assumed /dir would let us pick C:\MT5; that assumption was wrong,
# so /dir is no longer passed at all and this fixed path is used instead.
$InstallDir   = "C:\Program Files\MetaTrader 5"
$MqlDir       = Join-Path $InstallDir "MQL5"
$ExpertsDir   = Join-Path $MqlDir "Experts"
$IncludeDir   = Join-Path $MqlDir "Include"
$LibrariesDir = Join-Path $MqlDir "Libraries"
$ConfigDir    = Join-Path $InstallDir "config"

# ---------------------------------------------------------------------------
# 1. Install the MT5 terminal (generic MetaQuotes build; you log into your
#    own broker account inside it afterwards -- no broker-specific installer
#    is assumed here).
# ---------------------------------------------------------------------------
Write-Host "Downloading MT5 installer..."
$mt5Setup = "C:\OEM\mt5setup.exe"
Invoke-WebRequest -Uri "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $mt5Setup

Write-Host "Installing MT5 (expected at $InstallDir) ..."
Start-Process -FilePath $mt5Setup -ArgumentList "/auto"

# /auto still shows a license-agreement screen and a finish screen that
# each need a click (confirmed live). Poll for the setup window BY TITLE
# rather than tracking Start-Process's returned PID: under UAC elevation
# that PID is the non-elevated launcher stub, which exits as soon as it
# hands off to the real (elevated) installer process, so HasExited
# becomes true almost immediately and the loop would exit having clicked
# nothing (confirmed live -- the script sailed past this step while the
# real dialog sat waiting on screen). Detect real completion via
# terminal64.exe existing instead of any process handle.
#
# This assumes install.bat itself runs elevated/as SYSTEM (dockur/windows'
# normal first-logon provisioning context) so no UAC consent prompt is in
# the way -- SendKeys cannot click that dialog (it runs on the secure
# desktop). If this hangs, check the noVNC/RDP console for a stuck UAC
# prompt.
$wshell = New-Object -ComObject WScript.Shell
$terminalExe = Join-Path $InstallDir "terminal64.exe"
$deadline = (Get-Date).AddMinutes(5)
while (-not (Test-Path $terminalExe) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ($wshell.AppActivate("MetaTrader 5 Setup")) {
        Start-Sleep -Milliseconds 500
        $wshell.SendKeys("{ENTER}")
    }
}
if (-not (Test-Path $terminalExe)) {
    # Fallback in case a future installer build changes its default path
    # again -- search Program Files rather than assume and fail outright.
    $found = Get-ChildItem "C:\Program Files*" -Filter terminal64.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $InstallDir = $found.DirectoryName
        $MqlDir = Join-Path $InstallDir "MQL5"
        $ExpertsDir = Join-Path $MqlDir "Experts"
        $IncludeDir = Join-Path $MqlDir "Include"
        $LibrariesDir = Join-Path $MqlDir "Libraries"
        $ConfigDir = Join-Path $InstallDir "config"
        $terminalExe = $found.FullName
        Write-Host "terminal64.exe not at the expected default path -- found it at $InstallDir instead."
    } else {
        throw "MT5 installer did not finish within 5 minutes and terminal64.exe wasn't found anywhere under Program Files -- check the noVNC/RDP console for a stuck dialog."
    }
}
Start-Sleep -Seconds 3

New-Item -ItemType Directory -Force -Path $ExpertsDir, $IncludeDir, $LibrariesDir, $ConfigDir | Out-Null

# The installer's "Finish" screen auto-launches a non-portable terminal64
# and opens a browser registration page (confirmed live) -- neither is
# wanted here since step 6 launches our own portable-mode instance.
Get-Process terminal64 -ErrorAction SilentlyContinue | Stop-Process -Force

# ---------------------------------------------------------------------------
# 2. Fetch the ding9736/MQL5-ZeroMQ library (headers + libzmq/libsodium DLLs)
#    at provision time rather than vendoring a third-party binary in this
#    repo. Pin to a commit/tag if you want fully reproducible provisioning.
# ---------------------------------------------------------------------------
Write-Host "Downloading MQL5-ZeroMQ library..."
$zmqZip = "C:\OEM\mql5-zeromq.zip"
$zmqExtract = "C:\OEM\mql5-zeromq"
Invoke-WebRequest -Uri "https://github.com/ding9736/MQL5-ZeroMQ/archive/refs/heads/main.zip" -OutFile $zmqZip
Expand-Archive -Path $zmqZip -DestinationPath $zmqExtract -Force

$zmqRoot = Get-ChildItem $zmqExtract | Select-Object -First 1

# The repo's ZeroMQ.mqh (#include <ZeroMQ/ZeroMQ.mqh> from the EA) lives at
# the repo root and itself does #include "Core/Whatever.mqh" relative to
# its own folder -- so both need to land together under Include\ZeroMQ\.
$zmqIncludeDest = Join-Path $IncludeDir "ZeroMQ"
New-Item -ItemType Directory -Force -Path $zmqIncludeDest | Out-Null
Copy-Item -Path (Join-Path $zmqRoot.FullName "ZeroMQ.mqh") -Destination $zmqIncludeDest -Force
Copy-Item -Path (Join-Path $zmqRoot.FullName "Core") -Destination $zmqIncludeDest -Recurse -Force
Copy-Item -Path (Join-Path $zmqRoot.FullName "Libraries\*.dll") -Destination $LibrariesDir -Force

# CONFIRMED LIVE (fifth run, 2026-09-14): the EA loaded but its own Experts
# log showed "cannot load '...\libzmq.dll' [126]" (both DLLs verified
# present, correct size, right folder -- error 126 is Windows' "a required
# module for this DLL couldn't be found", i.e. one of libzmq.dll's own
# dependencies is missing) followed by "unresolved import function call".
# libzmq.dll is a native C++ build and needs the Visual C++ runtime
# (vcruntime140.dll / msvcp140.dll), which a fresh Windows image doesn't
# ship with. Installing the official redistributable before the EA ever
# tries to load the DLL. This exact fix has NOT yet had its own live run.
Write-Host "Installing Visual C++ runtime (libzmq.dll dependency)..."
$vcRedist = "C:\OEM\vc_redist.x64.exe"
Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcRedist
Start-Process -FilePath $vcRedist -ArgumentList "/install", "/quiet", "/norestart" -Wait

# ---------------------------------------------------------------------------
# 3. Drop in the EA source + default settings preset.
# ---------------------------------------------------------------------------
Copy-Item -Path "C:\OEM\TradingViewZeroMQExecutor.mq5" -Destination $ExpertsDir -Force
$presetsDir = Join-Path $MqlDir "Presets"
New-Item -ItemType Directory -Force -Path $presetsDir | Out-Null
Copy-Item -Path "C:\OEM\TradingViewZeroMQExecutor.set" -Destination (Join-Path $presetsDir "TradingViewZeroMQExecutor.set") -Force

# ---------------------------------------------------------------------------
# 4. Compile the EA headlessly via MetaEditor.
# ---------------------------------------------------------------------------
# CONFIRMED LIVE (fourth run, 2026-09-14): without /portable here, MetaEditor
# resolves the EA's #include <ZeroMQ/ZeroMQ.mqh> (angle brackets -- always
# relative to the terminal's assigned MQL5\Include, never to $InstallDir)
# against the terminal's normal %AppData% data folder instead of
# $InstallDir\MQL5\Include, where step 2 actually put the ZeroMQ files --
# compile failed with "error 106: file ... not found" even though the file
# genuinely existed, just in the folder MetaEditor wasn't looking in.
# /portable makes it use $InstallDir\MQL5 instead, matching step 2's target
# and step 6's /portable launch.
#
# CONFIRMED LIVE (fresh-install run from the Unraid template, 2026-09-26):
# on a brand-new VM the portable MQL5 folder has no standard library yet --
# MT5 only deploys it ("updating ...\MQL5 folder -- 453 files updated") the
# first time terminal64 runs in portable mode, then kicks off a full
# recompile. Compiling before that fails on the EA's #include
# <Trade/Trade.mqh>, and attaching the EA straight away races that first
# recompile ("not found from start config"). Earlier test runs reused a VM
# where MT5 had already run once, which hid both. So: one warm-up run first.
Write-Host "Warm-up MT5 run (deploys the MQL5 standard library)..."
$tradeMqh = Join-Path $IncludeDir "Trade\Trade.mqh"
Start-Process -FilePath $terminalExe -ArgumentList "/portable" -WorkingDirectory $InstallDir
$warmupEnd = (Get-Date).AddMinutes(5)
while (-not (Test-Path $tradeMqh) -and (Get-Date) -lt $warmupEnd) { Start-Sleep -Seconds 5 }
if (-not (Test-Path $tradeMqh)) { Write-Host "WARNING: $tradeMqh never appeared -- compile will probably fail." }
Start-Sleep -Seconds 90   # let the warm-up run's full recompile finish
Get-Process terminal64 -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() | Out-Null }
Start-Sleep -Seconds 15
Get-Process terminal64 -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host "Compiling EA..."
$metaEditor = Join-Path $InstallDir "metaeditor64.exe"
Start-Process -FilePath $metaEditor -ArgumentList "/compile:`"$ExpertsDir\TradingViewZeroMQExecutor.mq5`"", "/portable", "/log:`"C:\OEM\compile.log`"" -Wait
if (Test-Path (Join-Path $ExpertsDir "TradingViewZeroMQExecutor.ex5")) {
    Write-Host "EA compiled."
} else {
    Write-Host "WARNING: no TradingViewZeroMQExecutor.ex5 after compiling -- compile.log follows:"
    Get-Content "C:\OEM\compile.log" -ErrorAction SilentlyContinue | Write-Host
}

# CONFIRMED LIVE (sixth run, 2026-09-14): the EA binding its ZeroMQ PULL
# socket triggers an interactive "Windows Security -- allow this app on
# public/private networks?" firewall prompt on first launch. Clicking
# through it manually worked, but nothing here can click it unattended
# for real users, and if it's never answered the port may stay blocked
# for connections from outside the VM (i.e. from signal-bridge). Adding
# an inbound firewall rule for the port ahead of time so Windows never
# needs to ask.
Write-Host "Pre-authorizing ZeroMQ port 5555 through Windows Firewall..."
New-NetFirewallRule -DisplayName "MT5 ZeroMQ (tv-mt5-bridge)" -Direction Inbound -Protocol TCP -LocalPort 5555 -Action Allow -ErrorAction SilentlyContinue | Out-Null

# ---------------------------------------------------------------------------
# 5. Startup config: enable Algo Trading + DLL imports, auto-attach the EA.
#    Container env vars are NOT visible inside the Windows guest, so the
#    image's entrypoint (mt5-windows/entry.sh) writes them to C:\OEM\mt5.env
#    before dockur builds the install image. Read them from there, then
#    delete the file so the broker password doesn't linger in C:\OEM.
# ---------------------------------------------------------------------------
$mt5Settings = @{}
$envFile = "C:\OEM\mt5.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $key, $value = $line -split "=", 2
        if ($key) { $mt5Settings[$key.Trim()] = "$value".Trim() }
    }
    Remove-Item $envFile -Force
} else {
    Write-Host "WARNING: $envFile not found -- MT5 will start without a broker login."
}
$mt5Login    = $mt5Settings["MT5_LOGIN"]
$mt5Password = $mt5Settings["MT5_PASSWORD"]
$mt5Server   = $mt5Settings["MT5_SERVER"]
$mt5Symbol   = if ($mt5Settings["MT5_SYMBOL"]) { $mt5Settings["MT5_SYMBOL"] } else { "EURUSD" }

$configPath = Join-Path $ConfigDir "startup.ini"
@"
[Common]
Login=$mt5Login
Password=$mt5Password
Server=$mt5Server

[Experts]
AllowLiveTrading=1
AllowDllImport=1
Enabled=1

[StartUp]
Expert=TradingViewZeroMQExecutor
Symbol=$mt5Symbol
Period=M1
"@ | Out-File -FilePath $configPath -Encoding ascii

# ---------------------------------------------------------------------------
# 6. Launch on every login via launch-mt5.ps1, in portable mode (MQL5 data
#    under $InstallDir\MQL5 instead of a randomly-hashed AppData folder).
#    The launcher only uses startup.ini when the EA isn't already listening,
#    so reboots don't stack duplicate charts/EAs -- see its header.
# ---------------------------------------------------------------------------
$launcher = Join-Path $InstallDir "launch-mt5.ps1"
Copy-Item -Path "C:\OEM\launch-mt5.ps1" -Destination $launcher -Force

$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "MT5.lnk"
$wshell = New-Object -ComObject WScript.Shell
$shortcut = $wshell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$shortcut.WorkingDirectory = $InstallDir
$shortcut.Save()

Write-Host "Launching MT5 and attaching the EA..."
& $launcher -Attach
if ($LASTEXITCODE -eq 0) {
    Write-Host "Provisioning complete -- EA is listening on port 5555."
} else {
    Write-Host "Provisioning finished, but the EA never started listening on port 5555. See docs/mt5-ea-setup.md."
}
