#!/bin/sh
# Host-mounted /logs and /config (e.g. Unraid appdata) is usually created root-owned by
# the Docker daemon, so fix ownership as root, then drop to the app user.
set -e

if [ "$(id -u)" = "0" ]; then
  mkdir -p "${LOG_DIR:-/logs}" "${CONFIG_DIR:-/config}"
  chown bridge:bridge "${LOG_DIR:-/logs}" "${CONFIG_DIR:-/config}" 2>/dev/null || true
  exec setpriv --reuid=bridge --regid=bridge --init-groups "$@"
fi

exec "$@"
