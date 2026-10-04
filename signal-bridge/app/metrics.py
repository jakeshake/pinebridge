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


def fill_side(action):
    """Direction of the fill an action produces, for slippage: +1 buys, -1
    sells. Closing a long is a sell (filling lower is adverse); closing a
    short is a buy. A bare CLOSE can be either, so it isn't measured."""
    if action == "CLOSELONG":
        return -1
    if action == "CLOSESHORT":
        return 1
    return side_of(action)


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


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def max_drawdown(net_pnls):
    """Largest peak-to-trough fall of cumulative net PnL, in the order
    given (close order), starting from 0. Returned as a positive amount."""
    equity = peak = worst = 0.0
    for pnl in net_pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def trade_stats(trades):
    """KPIs over closed trades (dicts with net_pnl, pips, closed_at).
    Win = net PnL above zero, loss = below zero; a breakeven trade counts
    toward trades and expectancy but neither average. Profit factor =
    gross wins / gross losses (None when there are no losing trades)."""
    nets = [t["net_pnl"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    ordered = sorted(trades, key=lambda t: t.get("closed_at") or 0)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(nets) if nets else None,
        "net_pnl": sum(nets),
        "expectancy": _avg(nets),
        "expectancy_pips": _avg(t.get("pips") for t in trades),
        "avg_win": _avg(wins),
        "avg_loss": _avg(losses),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "max_drawdown": max_drawdown(t["net_pnl"] for t in ordered) if trades else None,
    }
