"""All runtime configuration, sourced from environment variables.

Nothing here should ever be a hardcoded LAN IP, port, or secret -- this
container is meant to run unmodified on any trader's own network.
"""
import logging
import os
import secrets


def _env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Flask
FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))

# ZeroMQ -- the MT5 EA binds a PULL socket and listens; this service
# connects to it as a PUSH socket. ZMQ_HOST must point at wherever the
# pinebridge-mt5 container/VM is reachable. The default is the Docker host's
# address on the default bridge network, where pinebridge-mt5 publishes 5555,
# so a stock Unraid install needs no change here.
ZMQ_HOST = os.environ.get("ZMQ_HOST") or "172.17.0.1"
ZMQ_PORT = int(os.environ.get("ZMQ_PORT", "5555"))

# Shared-secret auth. TradingView alerts can only send a raw text body (no
# custom headers), so the secret travels as a field in the alert payload
# itself: `...,secret=<value>`. REQUIRED once this is exposed to the
# internet -- leaving it unset disables the check, which is only meant for
# local testing before you put a tunnel in front of this.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
REQUIRE_SECRET = _env_bool("REQUIRE_WEBHOOK_SECRET", True)

# Where a generated secret is kept so it survives restarts and updates.
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SECRET_FILE = os.path.join(CONFIG_DIR, "webhook_secret")

# Optional public base URL (e.g. https://tv-webhook.example.com), only used
# to print the exact TradingView webhook URL at startup.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# How WEBHOOK_SECRET was obtained: "env", "file", "generated",
# "ephemeral" (generated but couldn't be saved) or "" (none needed).
SECRET_SOURCE = ""

# Logging
LOG_DIR = os.environ.get("LOG_DIR", "/logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Units of base currency per 1.0 lot, keyed by symbol. Standard forex lot =
# 100,000 units. Add entries here for non-forex instruments (crypto,
# indices, metals) you trade through this bridge -- they don't use the
# 100,000 convention and will silently mis-size if left on the default.
UNITS_PER_LOT = {
    "DEFAULT_FX": 100000,
}


def load_or_create_secret():
    """Fill WEBHOOK_SECRET when the template left it blank: reuse the one
    saved in CONFIG_DIR, or generate one and save it there. Sets
    SECRET_SOURCE. Called once at startup, before validate()."""
    global WEBHOOK_SECRET, SECRET_SOURCE
    if WEBHOOK_SECRET:
        SECRET_SOURCE = "env"
        return
    if not REQUIRE_SECRET:
        return
    try:
        with open(SECRET_FILE, encoding="utf-8") as fh:
            saved = fh.read().strip()
        if saved:
            WEBHOOK_SECRET, SECRET_SOURCE = saved, "file"
            return
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.getLogger(__name__).warning(f"Can't read {SECRET_FILE}: {exc}")

    WEBHOOK_SECRET = secrets.token_hex(32)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(WEBHOOK_SECRET + "\n")
        SECRET_SOURCE = "generated"
    except OSError:
        SECRET_SOURCE = "ephemeral"


def webhook_url(show_secret):
    base = PUBLIC_URL or "https://<your-tunnel-hostname>"
    if not REQUIRE_SECRET:
        return f"{base}/webhook"
    shown = WEBHOOK_SECRET if show_secret else "<your secret>"
    return f"{base}/webhook?secret={shown}"


def validate():
    """Fail fast on startup instead of silently degrading."""
    problems = []
    if REQUIRE_SECRET and not WEBHOOK_SECRET:
        problems.append(
            "No webhook secret: WEBHOOK_SECRET is empty and none could be "
            f"loaded from or generated into {SECRET_FILE}."
        )
    return problems
