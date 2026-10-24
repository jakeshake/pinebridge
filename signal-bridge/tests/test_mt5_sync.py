"""Pinebridge vs MT5 history: server time zone, broker swap deals,
closes that found nothing to close, and exit reasons from the alert.

Modelled on a Forex.com account (server time UTC+3, swap booked as a
separate balance deal with comment ">Swap #<position>")."""
import sqlite3

import pytest

from app.store import Store

H = 3600
OFFSET = 3 * H                 # Forex.com server: UTC+3
T0 = 1_761_000_000.0           # an arbitrary UTC instant


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def _entry(store, pos, t_utc, side="sell", price=158.142, strategy="kxf-v3", acked=True):
    sid = f"in{pos}"
    store.record_signal(sid, {"signal": "short" if side == "sell" else "long", "symbol": "USDJPY",
                              "strategy": strategy, "tv_price": price},
                        {"action": "SELL" if side == "sell" else "BUY", "symbol": "USDJPY"})
    store._write("UPDATE signals SET received_at=? WHERE signal_id=?", (t_utc - 3, sid))
    if acked:
        store.apply_ack({"signal_id": sid, "ok": True, "order": pos, "fill_price": price, "volume": 0.01})
        store._write("UPDATE signals SET acked_at=?, sent_at=? WHERE signal_id=?", (t_utc + 0.2, t_utc, sid))
    return sid


def _deal(store, deal, pos, entry, t_utc, received_delay=0.15, **kw):
    """A deal stamped in server time, reported `received_delay` s later."""
    r = {"deal": deal, "order": kw.pop("order", pos if entry == "in" else pos + 1), "position_id": pos,
         "symbol": "USDJPY", "side": kw.pop("side", "sell" if entry == "in" else "buy"),
         "entry": entry, "volume": 0.01, "price": kw.pop("price", 158.142), "profit": kw.pop("profit", 0),
         "commission": 0, "swap": 0, "time": t_utc + OFFSET, "reason": kw.pop("reason", "ea"),
         "magic": 777777878, "comment": "kxf-v3"}
    r.update(kw)
    store.add_deal(r, received_at=t_utc + received_delay)


def _trade(store, pos):
    return next(t for t in store.positions() if t["position_id"] == pos)


# ---- server time zone ------------------------------------------------------------

def test_live_deals_are_shown_in_utc_not_server_time(store):
    _entry(store, 5831308, T0)
    _deal(store, 1, 5831308, "in", T0)
    _deal(store, 2, 5831308, "out", T0 + 12 * H, price=157.062, profit=6.81)
    t = _trade(store, 5831308)
    assert t["opened_at"] == pytest.approx(T0)
    assert t["closed_at"] == pytest.approx(T0 + 12 * H)
    assert t["duration_s"] == pytest.approx(12 * H)


def test_offset_is_measured_by_the_bridge_not_taken_from_the_ea(store):
    # An EA whose Windows time zone is wrong reports offset 0; the arrival
    # time still gives the real +3 h.
    _deal(store, 1, 9, "in", T0, server_offset=0)
    assert _trade(store, 9)["opened_at"] == pytest.approx(T0)


def test_old_rows_are_corrected_from_their_entry_acks(tmp_path):
    """Deals stored before the fix: raw server time in `time`, no offset.
    Entry deals anchor on the bridge's UTC ack time; a broker close with no
    ack of its own takes the nearest anchor."""
    path = str(tmp_path / "old.db")
    s = Store(path)
    _entry(s, 7, T0)
    with sqlite3.connect(path) as db:   # as an EA v3.7 + old bridge wrote them
        db.execute("INSERT INTO deals(deal, order_ticket, position_id, symbol, side, entry, volume, price,"
                   " profit, commission, swap, time, reason) VALUES"
                   " (70, 7, 7, 'USDJPY', 'sell', 'in', 0.01, 158.142, 0, 0, 0, ?, 'ea'),"
                   " (71, 8, 7, 'USDJPY', 'buy', 'out', 0.01, 157.062, 6.81, 0, 0, ?, 'sl')",
                   (T0 + OFFSET, T0 + 12 * H + OFFSET))
        db.execute("UPDATE deals SET time_raw=NULL, server_offset=NULL, offset_trusted=NULL")
    t = _trade(Store(path), 7)
    assert t["opened_at"] == pytest.approx(T0)
    assert t["closed_at"] == pytest.approx(T0 + 12 * H)


def test_late_report_does_not_overwrite_a_measured_offset(store):
    _deal(store, 1, 11, "in", T0)
    # The same deal re-sent a day later by a history resync with a stale offset.
    _deal(store, 1, 11, "in", T0, received_delay=86400, backfill=True, server_offset=2 * H)
    assert _trade(store, 11)["opened_at"] == pytest.approx(T0)


def test_runs_use_the_corrected_open_time(store):
    # Opened 2 h before a run starts: in server time it looked 1 h inside it.
    _entry(store, 12, T0)
    _deal(store, 1, 12, "in", T0)
    _deal(store, 2, 12, "out", T0 + H, price=158.0, profit=0.9)
    store._write("INSERT INTO runs(name, started_at) VALUES ('new', ?)", (T0 + 2 * H,))
    assert store.overview(store.filters())["trades"] == []


# ---- broker swap ---------------------------------------------------------------

def test_separate_swap_deals_land_on_their_position(store):
    _entry(store, 5828054, T0, side="buy", price=158.007)
    _deal(store, 1, 5828054, "in", T0, side="buy", price=158.007)
    _deal(store, 2, 5828054, "adjust", T0 + 6 * H, order=0, side=None, price=0, kind="swap",
          reason="swap", swap=0.13, magic=0, comment=">Swap #5828054")
    _deal(store, 3, 5828054, "out", T0 + 12 * H, side="sell", price=158.146, profit=0.87)
    t = _trade(store, 5828054)
    assert t["swap"] == pytest.approx(0.13)
    assert t["net_pnl"] == pytest.approx(1.00)
    assert t["actual_pnl"] == pytest.approx(0.87)   # swap stays out of execution cost
    assert t["lots"] == 0.01 and t["pips"] == pytest.approx(13.9)
    assert t["closed_at"] == pytest.approx(T0 + 12 * H) and t["side"] == "buy"


def test_swap_on_an_open_position_does_not_close_it(store):
    _deal(store, 1, 5839784, "in", T0)
    _deal(store, 2, 5839784, "adjust", T0 + 10 * H, order=0, side=None, price=0, kind="swap",
          swap=-0.11, magic=0, comment=">Swap #5839784")
    t = _trade(store, 5839784)
    assert t["closed"] is False and t["swap"] == pytest.approx(-0.11)


# ---- signals -------------------------------------------------------------------

def test_close_that_closed_nothing_is_not_executed(store):
    store.record_signal("c1", {"signal": "closeshort", "symbol": "USDJPY", "strategy": "kxf-v3"},
                        {"action": "CLOSESHORT", "symbol": "USDJPY"})
    store.apply_ack({"signal_id": "c1", "ok": True, "retcode": 0,
                     "detail": "Close summary - Closed: 0, Failed: 0", "closed_tickets": []})
    row = store.overview(store.filters("all"))["signals"][0]
    assert row["status"] == "no position"


def test_close_that_closed_something_is_executed(store):
    store.record_signal("c2", {"signal": "closelong", "symbol": "USDJPY"},
                        {"action": "CLOSELONG", "symbol": "USDJPY"})
    store.apply_ack({"signal_id": "c2", "ok": True, "retcode": 10009,
                     "detail": "Close summary - Closed: 1, Failed: 0", "closed_tickets": [42]})
    assert store.overview(store.filters("all"))["signals"][0]["status"] == "executed"


def test_exit_reason_from_the_alert(store):
    _entry(store, 21, T0)
    _deal(store, 1, 21, "in", T0)
    store.record_signal("x21", {"signal": "closeshort", "symbol": "USDJPY", "reason": "time"},
                        {"action": "CLOSESHORT", "symbol": "USDJPY"})
    store.apply_ack({"signal_id": "x21", "ok": True, "detail": "Close summary - Closed: 1, Failed: 0",
                     "closed_tickets": [21]})
    _deal(store, 2, 21, "out", T0 + 12 * H, price=158.0, profit=0.9)
    assert _trade(store, 21)["exit_reason"] == "ea:time"


def test_exit_without_a_reason_is_plain_signal(store):
    _entry(store, 22, T0)
    _deal(store, 1, 22, "in", T0)
    _deal(store, 2, 22, "out", T0 + H, price=158.0, profit=0.9)
    assert _trade(store, 22)["exit_reason"] == "ea"


def test_heartbeat_position_open_time_is_utc(store):
    _entry(store, 30, T0)
    _deal(store, 1, 30, "in", T0)
    store.add_account({"equity": 1, "positions": [{"ticket": 30, "symbol": "USDJPY",
                                                   "opened_at": int(T0 + OFFSET)}]})
    pos = store.overview(store.filters("all"))["positions"][0]
    assert pos["opened_at"] == pytest.approx(int(T0 + OFFSET) - OFFSET)


def test_old_close_that_closed_nothing_is_relabelled_on_upgrade(tmp_path):
    path = str(tmp_path / "old.db")
    s = Store(path)
    s.record_signal("c3", {"signal": "closeshort", "symbol": "USDJPY"},
                    {"action": "CLOSESHORT", "symbol": "USDJPY"})
    with sqlite3.connect(path) as db:   # as the old bridge stored it
        db.execute("UPDATE signals SET status='executed', detail='Close summary - Closed: 0, Failed: 0'"
                   " WHERE signal_id='c3'")
    rows = Store(path).overview(Store(path).filters("all"))["signals"]
    assert rows[0]["status"] == "no position"
