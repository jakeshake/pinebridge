import json

import pytest

from app import metrics, reports, server
from app.dashboard import create_app
from app.store import Store


# ---- slippage / PnL maths ---------------------------------------------------

def test_side_of():
    assert metrics.side_of("BUY") == metrics.side_of("ARM_LONG") == 1
    assert metrics.side_of("SELL") == metrics.side_of("ARM_SHORT") == -1
    assert metrics.side_of("CLOSELONG") == 0


@pytest.mark.parametrize(
    "side,ref,fill,pips",
    [
        (1, 1.08500, 1.08512, 1.2),    # buy filled higher: adverse
        (1, 1.08500, 1.08490, -1.0),   # buy filled lower: favorable
        (-1, 1.08500, 1.08488, 1.2),   # sell filled lower: adverse
        (-1, 1.08500, 1.08507, -0.7),  # sell filled higher: favorable
    ],
)
def test_slippage_sign_convention(side, ref, fill, pips):
    s = metrics.slippage(side, ref, fill, 0.0001)
    assert s["pips"] == pytest.approx(pips)


def test_slippage_money_uses_tick_value():
    # EURUSD: tick 0.00001 worth $1 per lot; 1.2 pips adverse on 0.5 lots = $6.
    s = metrics.slippage(1, 1.08500, 1.08512, 0.0001, tick_size=0.00001, tick_value=1.0, volume=0.5)
    assert s["money"] == pytest.approx(6.0)


def test_slippage_missing_inputs():
    assert metrics.slippage(1, None, 1.1, 0.0001) is None
    assert metrics.slippage(0, 1.1, 1.1, 0.0001) is None
    assert metrics.slippage(1, 1.1, 1.1, 0.0001)["money"] is None


def test_expected_pnl():
    # Short 1 lot from 1.0850 to 1.0830 at TV prices = +20 pips = $200.
    assert metrics.expected_pnl(-1, 1.0850, 1.0830, 0.00001, 1.0, 1.0) == pytest.approx(200.0)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-28T12:00:00Z", 1790596800.0),
        ("1790596800", 1790596800.0),
        (1790596800000, 1790596800.0),
        ("not a time", None),
        (None, None),
    ],
)
def test_parse_tv_time(value, expected):
    assert metrics.parse_tv_time(value) == expected


# ---- store: correlation by signal_id, positions, overview ----------------------

@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def _entry(store, sid="s1", signal="long", action="BUY", tv=1.08500):
    parsed = {"signal": signal, "symbol": "EURUSD", "entry_price": tv, "tv_time": "2026-09-28T12:00:00Z"}
    store.record_signal(sid, parsed, {"action": action, "symbol": "EURUSD", "size": 0.1})
    store.mark_sent(sid, True)


def test_ack_joins_by_signal_id(store):
    _entry(store)
    reports.handle(store, json.dumps({
        "type": "ack", "signal_id": "s1", "ok": True, "retcode": 10009, "detail": "OK: BUY EXECUTED!",
        "symbol": "EURUSD", "order": 5001, "requested_price": 1.08505, "fill_price": 1.08512,
        "volume": 0.1, "tick_size": 0.00001, "tick_value": 1.0, "ea_ms": 42, "closed_tickets": [],
    }))
    row = store.overview()["signals"][0]
    assert row["status"] == "executed"
    assert row["slip_tv_pips"] == pytest.approx(1.2)
    assert row["slip_broker_pips"] == pytest.approx(0.7)
    assert row["slip_tv_money"] == pytest.approx(1.2)
    assert row["bridge_to_ea_ms"] is not None and row["bridge_to_ea_ms"] >= 0


def test_failed_ack_and_rejected_signal(store):
    _entry(store)
    reports.handle(store, json.dumps({"type": "ack", "signal_id": "s1", "ok": False, "retcode": 10031,
                                      "detail": "FAIL: TRADE FAILED: 10031 - no connection"}))
    store.record_signal("s2", {"signal": ""}, None, "rejected", "Alert has no signal= field")
    rows = {r["signal_id"]: r for r in store.overview()["signals"]}
    assert rows["s1"]["status"] == "failed" and rows["s1"]["retcode"] == 10031
    assert rows["s2"]["status"] == "rejected"


def test_round_trip_position_expected_vs_actual(store):
    # Entry: long at TV 1.08500, filled 1.08512 as position/order 5001.
    _entry(store)
    reports.handle(store, json.dumps({"type": "ack", "signal_id": "s1", "ok": True, "order": 5001,
                                      "fill_price": 1.08512, "volume": 1.0, "tick_size": 0.00001,
                                      "tick_value": 1.0, "requested_price": 1.08510}))
    # Exit signal at TV 1.08700 closed ticket 5001.
    store.record_signal("s2", {"signal": "closelong", "symbol": "EURUSD", "close_price": 1.08700},
                        {"action": "CLOSELONG", "symbol": "EURUSD"})
    reports.handle(store, json.dumps({"type": "ack", "signal_id": "s2", "ok": True, "closed_tickets": [5001]}))
    for d in (
        {"deal": 1, "order": 5001, "position_id": 5001, "entry": "in", "volume": 1.0, "price": 1.08512,
         "profit": 0, "commission": -3.5, "swap": 0, "time": 1.0},
        {"deal": 2, "order": 5002, "position_id": 5001, "entry": "out", "volume": 1.0, "price": 1.08690,
         "profit": 178.0, "commission": -3.5, "swap": 0, "time": 2.0, "reason": "ea"},
    ):
        reports.handle(store, json.dumps({"type": "deal", "symbol": "EURUSD", "side": "buy", **d}))

    [pos] = store.positions()
    assert pos["closed"] and pos["actual_pnl"] == pytest.approx(171.0)
    assert pos["expected_pnl"] == pytest.approx(200.0)   # 20 pips at TV prices
    k = store.overview()["kpis"]
    assert k["win_rate"] == 1.0 and k["closed_trades"] == 1
    assert k["net_pnl"] == pytest.approx(171.0)
    assert k["expected_pnl"] - k["actual_pnl_compared"] == pytest.approx(29.0)
    assert k["market_fill_rate"] == 1.0


def test_broker_sl_close_without_signal_has_no_expected(store):
    _entry(store)
    reports.handle(store, json.dumps({"type": "ack", "signal_id": "s1", "ok": True, "order": 7,
                                      "fill_price": 1.085, "volume": 0.1}))
    for d in ({"deal": 1, "entry": "in", "profit": 0}, {"deal": 2, "entry": "out", "profit": -12.0, "reason": "sl"}):
        store.add_deal({"order": 7, "position_id": 7, "symbol": "EURUSD", "volume": 0.1, "time": d["deal"], **d})
    [pos] = store.positions()
    assert pos["closed"] and pos["expected_pnl"] is None and pos["exit_reason"] == "sl"
    assert store.overview()["kpis"]["win_rate"] == 0.0


def test_account_heartbeat_and_prune(store):
    reports.handle(store, json.dumps({"type": "account", "balance": 50000, "equity": 49990.5,
                                      "connected": True, "positions": [{"ticket": 1}], "currency": "USD"}))
    o = store.overview()
    assert o["kpis"]["equity"] == 49990.5 and o["positions"] == [{"ticket": 1}]
    assert o["ea"]["connected"] is True
    _entry(store)
    store._write("UPDATE signals SET received_at = 0")
    store.prune(1)
    assert store.overview()["signals"] == []


def test_malformed_and_unknown_reports_are_ignored(store):
    reports.handle(store, "not json")
    reports.handle(store, json.dumps({"type": "mystery"}))
    assert store.overview()["signals"] == []


# ---- dashboard app vs webhook app ---------------------------------------------

def test_dashboard_is_not_served_on_the_webhook_port():
    client = server.app.test_client()
    assert client.get("/").status_code == 404
    assert client.get("/api/overview").status_code == 404


def test_dashboard_serves_page_and_api(store):
    client = create_app(store, password="").test_client()
    page = client.get("/")
    assert page.status_code == 200 and b"Pinebridge Dashboard" in page.data
    # No external resources: the only outside URL is the plain donation link.
    import re
    external = set(re.findall(rb'(?:src|href)="(https?://[^"]+)"', page.data))
    assert external == {b"https://buymeacoffee.com/jakeshake"}
    assert b"<script src" not in page.data and b"@import" not in page.data
    assert client.get("/api/overview").get_json()["kpis"]["signals_total"] == 0
    assert client.get("/pinebridge.svg").status_code == 200


def test_dashboard_password(store):
    client = create_app(store, password="hunter2").test_client()
    assert client.get("/api/overview").status_code == 401
    ok = client.get("/api/overview", headers={"Authorization": "Basic " + __import__("base64")
                                              .b64encode(b"admin:hunter2").decode()})
    assert ok.status_code == 200


def test_webhook_assigns_signal_id_and_records_it(monkeypatch):
    sent = []
    monkeypatch.setattr(server.zmq_client, "send", lambda s: (sent.append(s), (True, 0.1))[1])
    r = server.app.test_client().post(
        "/webhook?secret=test-secret",
        data="signal=long,symbol=EURUSD,qty=1000,entry_price=1.085,tv_time=2026-09-28T12:00:00Z",
    )
    assert r.status_code == 200
    sid = sent[0]["signal_id"]
    assert len(sid) == 12
    rows = {s["signal_id"]: s for s in server.store.overview()["signals"]}
    assert rows[sid]["status"] == "sent" and rows[sid]["tv_price"] == 1.085


# ---- v3.6: exit slippage ---------------------------------------------------------

@pytest.mark.parametrize(
    "action,ref,fill,pips",
    [
        ("CLOSELONG", 1.08700, 1.08690, 1.0),    # closing a long is a sell: lower is adverse
        ("CLOSELONG", 1.08700, 1.08705, -0.5),
        ("CLOSESHORT", 1.08300, 1.08308, 0.8),   # closing a short is a buy: higher is adverse
        ("CLOSESHORT", 1.08300, 1.08296, -0.4),
    ],
)
def test_exit_slippage_sign(action, ref, fill, pips):
    s = metrics.slippage(metrics.fill_side(action), ref, fill, 0.0001)
    assert s["pips"] == pytest.approx(pips)


def test_bare_close_is_not_measured():
    assert metrics.fill_side("CLOSE") == 0


def test_close_ack_shows_exit_fill_and_slippage(store):
    store.record_signal("c1", {"signal": "closelong", "symbol": "EURUSD", "close_price": 1.08700},
                        {"action": "CLOSELONG", "symbol": "EURUSD"})
    store.mark_sent("c1", True)
    reports.handle(store, json.dumps({
        "type": "ack", "signal_id": "c1", "ok": True, "closed_tickets": [5001, 5002],
        "fill_price": 1.08690, "requested_price": 1.08692, "volume": 0.3,
        "tick_size": 0.00001, "tick_value": 1.0,
    }))
    row = store.overview()["signals"][0]
    assert row["fill_price"] == 1.0869
    assert row["slip_tv_pips"] == pytest.approx(1.0)
    assert row["slip_tv_money"] == pytest.approx(3.0)       # 1 pip adverse on 0.3 lots
    assert row["slip_broker_pips"] == pytest.approx(0.2)
    # Exits don't enter the entry-slippage histogram or its average.
    assert store.overview()["slippage"] == []
