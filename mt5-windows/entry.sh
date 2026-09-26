#!/usr/bin/env bash
# Runs before dockur/windows' own entrypoint. dockur copies /oem into the
# Windows install image on first boot (it lands at C:\OEM and install.bat is
# run at first logon), but container env vars never reach the guest -- so
# this is the only point where MT5_* settings can be handed to Windows.
set -euo pipefail

mkdir -p /oem

# Bundled provisioning files; -n keeps anything a user mounted over /oem.
cp -rn /opt/tv-mt5/oem/. /oem/

umask 077
{
  echo "MT5_LOGIN=${MT5_LOGIN:-}"
  echo "MT5_PASSWORD=${MT5_PASSWORD:-}"
  echo "MT5_SERVER=${MT5_SERVER:-}"
  echo "MT5_SYMBOL=${MT5_SYMBOL:-EURUSD}"
} > /oem/mt5.env

exec /run/entry.sh "$@"
