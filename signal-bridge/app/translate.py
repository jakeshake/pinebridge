"""Translates a parsed TradingView alert into the JSON message the MT5 EA
(TradingViewZeroMQExecutor.mq5) expects on its ZeroMQ PULL socket.

Generic core only: BUY/SELL/CLOSE*/MODIFY. This is deliberately dispatched
through SIGNAL_HANDLERS (signal type -> handler function) rather than one
big if/elif chain, so alert formats with extra fields register their own
handlers without touching the generic path. The resting-limit-order
signals (armlong/armshort/cancellong/cancelshort, sent by the IGT Pine
script's "Resting Limit Order Entries" feature) are registered below.
"""
from datetime import datetime, timezone

from . import config

ACTION_MAP = {
    # Entries
    "long": "BUY",
    "short": "SELL",
    "buy": "BUY",
    "sell": "SELL",
    # Exits (single-position style)
    "closelong": "CLOSELONG",
    "closeshort": "CLOSESHORT",
    "close": "CLOSE",
    # Exits (all-positions style) -- aliases
    "closealllong": "CLOSELONG",
    "closeallshort": "CLOSESHORT",
    "closeall": "CLOSE",
    # Alternative naming some strategies use
    "exitlong": "CLOSELONG",
    "exitshort": "CLOSESHORT",
    "exit": "CLOSE",
}

VALID_ACTIONS = {
    "BUY", "SELL", "CLOSE", "CLOSELONG", "CLOSESHORT", "MODIFY",
    "ARM_LONG", "ARM_SHORT", "CANCEL_LONG", "CANCEL_SHORT",
}

# Signal types handled by a dedicated function instead of the generic
# entry/exit path below. Populated by register_handler(); empty by default.
SIGNAL_HANDLERS = {}


def register_handler(signal_type, handler):
    """Plug in a handler for a signal type the generic path doesn't cover.

    `handler(parsed_data, symbol, pip_size) -> dict` must return a fully
    formed EA message (with an 'action' key). Intended for an extensions
    module to call at import time -- see docs/tradingview-alert-format.md
    for the advanced/extended format this is meant to support later.
    """
    SIGNAL_HANDLERS[signal_type] = handler


def get_units_per_lot(symbol):
    return config.UNITS_PER_LOT.get(symbol.upper(), config.UNITS_PER_LOT["DEFAULT_FX"])


def get_pip_size(symbol):
    """JPY forex pairs = 0.01, other forex = 0.0001. Extend for
    crypto/indices if you trade those through this bridge -- their
    pip/tick conventions aren't covered by this heuristic."""
    symbol = symbol.upper()
    return 0.01 if "JPY" in symbol else 0.0001


def qty_to_lots(parsed_data, symbol):
    """Prefer a direct lot count ('risk_lots'/'size') if present, else
    convert TradingView's 'qty' (base-currency units) into lots."""
    if "risk_lots" in parsed_data or "size" in parsed_data:
        return float(parsed_data.get("risk_lots", parsed_data.get("size", 0.01)))
    if "qty" in parsed_data:
        units_per_lot = get_units_per_lot(symbol)
        return float(parsed_data["qty"]) / units_per_lot if units_per_lot else 0.01
    return 0.01


def price_to_pips(ref_price, target_price, pip_size):
    if ref_price is None or target_price is None:
        return 0.0
    return round(abs(float(ref_price) - float(target_price)) / pip_size, 2)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _translate_modify(parsed_data, symbol, pip_size):
    return {
        "action": "MODIFY",
        "symbol": symbol,
        "sl_price": parsed_data.get("sl_price"),
        "tp_price": parsed_data.get("tp_price"),
        "timestamp": _now(),
    }


SIGNAL_HANDLERS["modify"] = _translate_modify


def _zone_id(parsed_data):
    """Pine's zone/OB id, or -1 when the alert has none (or it's malformed)."""
    try:
        return int(parsed_data.get("zone_id", -1))
    except (TypeError, ValueError):
        return -1


def _translate_arm(parsed_data, symbol, pip_size):
    """Rest a limit order at limit_price. SL/TP pips are measured from the
    limit price itself, which is where the order will fill."""
    is_long = str(parsed_data.get("signal", "")).lower() == "armlong"
    limit_price = parsed_data.get("limit_price")
    if limit_price is None:
        raise ValueError("ARM signal is missing limit_price")
    return {
        "action": "ARM_LONG" if is_long else "ARM_SHORT",
        "symbol": symbol,
        "size": qty_to_lots(parsed_data, symbol),
        "limit_price": float(limit_price),
        "sl_pips": price_to_pips(limit_price, parsed_data.get("sl_price"), pip_size),
        "tp_pips": price_to_pips(limit_price, parsed_data.get("tp_price"), pip_size),
        "zone_id": _zone_id(parsed_data),
        "zone_src": str(parsed_data.get("zone_src", "")),
        "comment": str(parsed_data.get("comment", "TV-Signal")),
        "timestamp": _now(),
    }


def _translate_cancel(parsed_data, symbol, pip_size):
    is_long = str(parsed_data.get("signal", "")).lower() == "cancellong"
    return {
        "action": "CANCEL_LONG" if is_long else "CANCEL_SHORT",
        "symbol": symbol,
        "zone_id": _zone_id(parsed_data),
        "zone_src": str(parsed_data.get("zone_src", "")),
        "timestamp": _now(),
    }


for _sig in ("armlong", "armshort"):
    SIGNAL_HANDLERS[_sig] = _translate_arm
for _sig in ("cancellong", "cancelshort"):
    SIGNAL_HANDLERS[_sig] = _translate_cancel


def translate(parsed_data):
    """Parsed alert dict -> EA message dict. Raises ValueError if the
    signal type isn't recognized by any registered handler or the
    generic entry/exit map."""
    signal_type = str(parsed_data.get("signal", "")).lower()
    symbol = str(parsed_data.get("symbol", "")).upper()
    pip_size = get_pip_size(symbol)

    handler = SIGNAL_HANDLERS.get(signal_type)
    if handler is not None:
        return handler(parsed_data, symbol, pip_size)

    if signal_type not in ACTION_MAP:
        raise ValueError(f"Unrecognized signal type: {signal_type!r}")

    action = ACTION_MAP[signal_type]
    size = qty_to_lots(parsed_data, symbol)
    entry_price = parsed_data.get("entry_price")

    if "sl_pips" in parsed_data:
        sl_pips = float(parsed_data.get("sl_pips", 0))
    else:
        sl_pips = price_to_pips(entry_price, parsed_data.get("sl_price"), pip_size)

    if "tp_pips" in parsed_data:
        tp_pips = float(parsed_data.get("tp_pips", 0))
    else:
        tp_pips = price_to_pips(entry_price, parsed_data.get("tp_price"), pip_size)

    ea_signal = {
        "action": action,
        "symbol": symbol,
        "size": size,
        "sl_pips": sl_pips,
        "tp_pips": tp_pips,
        "comment": str(parsed_data.get("comment", "TV-Signal")),
        "timestamp": _now(),
    }
    # Entries only: lets the EA recognise a double-down add (dd=1) and
    # reconcile a bar-close entry against a resting limit that already
    # filled on the same zone.
    if action in ("BUY", "SELL"):
        ea_signal["zone_id"] = _zone_id(parsed_data)
        ea_signal["dd"] = 1 if str(parsed_data.get("dd", 0)).lower() in ("1", "true") else 0
    return ea_signal


def describe(ea_signal):
    """Human-readable one-liner for logging."""
    action = ea_signal.get("action", "")
    symbol = ea_signal.get("symbol", "")
    if action == "MODIFY":
        return f"{action} {symbol} sl={ea_signal.get('sl_price')} tp={ea_signal.get('tp_price')}"
    if action.startswith("CANCEL_"):
        return f"{action} {symbol} zone={ea_signal.get('zone_src')}#{ea_signal.get('zone_id')}"
    if action.startswith("ARM_"):
        return (
            f"{action} {symbol} size={ea_signal.get('size')} @ {ea_signal.get('limit_price')} "
            f"sl={ea_signal.get('sl_pips')}pips tp={ea_signal.get('tp_pips')}pips "
            f"zone={ea_signal.get('zone_src')}#{ea_signal.get('zone_id')}"
        )
    return (
        f"{action} {symbol} size={ea_signal.get('size')} "
        f"sl={ea_signal.get('sl_pips')}pips tp={ea_signal.get('tp_pips')}pips"
    )
