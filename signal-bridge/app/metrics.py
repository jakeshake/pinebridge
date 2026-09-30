"""Pure execution-quality maths for the dashboard (no I/O, unit tested).

Sign convention: slippage is positive when it cost you (adverse) and
negative when it helped (favorable), for both buys and sells.
"""
from datetime import datetime, timezone

LONG_ACTIONS = {"BUY", "ARM_LONG"}
SHORT_ACTIONS = {"SELL", "ARM_SHORT"}


def side_of(action):
    """+1 for long entries, -1 for short entries, 0 for anything else."""
    if action in LONG_ACTIONS:
        return 1
    if action in SHORT_ACTIONS:
        return -1
    return 0


def price_to_money(price_diff, tick_size, tick_value, volume):
    """Convert a price distance into account currency using the broker's
    tick size/value for the symbol (as reported by the EA)."""
    if not tick_size or not tick_value or volume is None:
        return None
    return price_diff / tick_size * tick_value * volume


def slippage(side, ref_price, fill_price, pip_size, tick_size=None, tick_value=None, volume=None):
    """Slippage of a fill against a reference price, adverse positive.

    Returns None when either price or the side is missing."""
    if not side or ref_price is None or fill_price is None:
        return None
    diff = (fill_price - ref_price) * side
    return {
        "price": diff,
        "pips": diff / pip_size if pip_size else None,
        "money": price_to_money(diff, tick_size, tick_value, volume),
    }


def expected_pnl(side, entry_ref, exit_ref, tick_size, tick_value, volume):
    """PnL of the same trade at TradingView's prices, in account currency."""
    if not side or entry_ref is None or exit_ref is None:
        return None
    return price_to_money((exit_ref - entry_ref) * side, tick_size, tick_value, volume)


def parse_tv_time(value):
    """TradingView's {{timenow}} ("2026-09-28T12:34:56Z") or epoch seconds /
    milliseconds -> epoch seconds (float), or None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip()
    try:
        num = float(text)
        return num / 1000.0 if num > 1e11 else num
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
