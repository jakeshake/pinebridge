# Starts MT5 and makes sure the EA is actually listening on its ZeroMQ port.
# Installed next to terminal64.exe by setup-mt5.ps1 and run at every logon.
#
# Normal boots: start MT5 plainly -- its saved profile restores the chart
# with the EA on it. Passing the /config start file every boot would open
# yet another chart with another copy of the EA each time (only one copy can
# bind the port; the rest fail), so the start file is only used when the EA
# isn't listening -- first run, a lost profile, or an MT5 update whose
# post-update recompile raced the attach (confirmed live: the start-config
# attach ran 3s into a fresh install's first full recompile and failed with
# "not found from start config").
param([switch]$Attach)

$dir = $PSScriptRoot
$exe = Join-Path $dir "terminal64.exe"
$cfg = Join-Path $dir "config\startup.ini"
$port = 5555

function Test-Listening {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Listening([int]$seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        if (Test-Listening) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

if (-not $Attach) {
    if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $exe -ArgumentList "/portable" -WorkingDirectory $dir
    }
    if (Wait-Listening 120) { exit 0 }
    Write-Host "EA not listening on port $port after 2 minutes -- attaching it via the start config."
}

for ($i = 1; $i -le 3; $i++) {
    Get-Process terminal64 -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 5
    Start-Process -FilePath $exe -ArgumentList "/portable", "/config:`"$cfg`"" -WorkingDirectory $dir
    if (Wait-Listening 180) {
        Write-Host "EA listening on port $port (attempt $i)."
        exit 0
    }
    Write-Host "Attempt $i`: EA still not listening on port $port."
}
Write-Host "Giving up -- attach TradingViewZeroMQExecutor by hand (see docs/mt5-ea-setup.md)."
exit 1
