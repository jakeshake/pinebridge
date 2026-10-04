"""Runs, strategy attribution, per-trade PnL, breakdowns and CSV export."""
import csv
import io
import json
import sqlite3
import zipfile

import pytest

from app import metrics, reports, server, translate
from app.dashboard import create_app
from app.parser import parse_alert
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def tr(body):
    return translate.translate(parse_alert(body))


# ---- strategy tags in the translator ------------------------------------------

@pytest.mark.parametrize(
    "raw,tag",
    [
        ("sflow-v2", "sflow-v2"),
        ("SFLOW v2", "sflow-v2"),
        ("ictgann", "ictgann"),
        ("a-very-long-strategy-name", "a-very-long-stra"),
        ("", None),
        ("untagged", None),
        (None, None),
    ],
)
def test_normalize_strategy(raw, tag):
    assert translate.normalize_strategy(raw) == tag


def test_strategy_slot_is_stable_and_in_range():
    assert translate.strategy_slot("sflow-v2") == translate.strategy_slot("sflow-v2")
    assert all(1 <= translate.strategy_slot(t) <= 999 for t in ("a", "igt", "sdx2", "sflow-v2"))


def test_entry_carries_tag_as_comment_and_magic_slot():
    sig = tr("signal=long,symbol=EURUSD,qty=1000,entry_price=1.085,strategy=SFLOW-v2,comment=old")
    assert sig["strategy"] == "sflow-v2"
    assert sig["comment"] == "sflow-v2"
    assert sig["magic_slot"] == translate.strategy_slot("sflow-v2")
    arm = tr("signal=armlong,symbol=EURUSD,qty=1000,limit_price=1.085,zone_id=7,strategy=igt")
    assert arm["comment"] == "igt" and arm["magic_slot"] == translate.strategy_slot("igt")


def test_exit_carries_tag_but_no_magic():
    sig = tr("signal=closelong,symbol=EURUSD,strategy=igt")
    assert sig["strategy"] == "igt" and "magic_slot" not in sig


def test_untagged_entry_is_unchanged():
    sig = tr("signal=short,symbol=EURUSD,qty=1000,comment=mine")
    assert "strategy" not in sig and "magic_slot" not in sig and sig["comment"] == "mine"


def test_webhook_stores_the_tag(monkeypatch):
    monkeypatch.setattr(server.zmq_client, "send", lambda s: (True, 0.1))
    r = server.app.test_client().post("/webhook?secret=test-secret",
                                      data="signal=long,symbol=GBPUSD,qty=1000,entry_price=1.3,strategy=igt")
    assert r.status_code == 200 and r.get_json()["signal"]["strategy"] == "igt"
    rows = [s for s in server.store.overview(server.store.filters("all"))["signals"] if s["symbol"] == "GBPUSD"]
    assert rows[0]["strategy"] == "igt"


# ---- helpers to build trades ------------------------------------------------------

def _trade(store, pos, symbol="EURUSD", side="buy", entry=1.10000, exit_=1.10100, lots=0.1, profit=10.0,
           commission=-0.7, swap=0.0, t_in=1000.0, t_out=1600.0, reason="ea", strategy=None,
           magic=None, comment=None, with_signal=True):
    if with_signal:
        sid = f"e{pos}"
        parsed = {"signal": "long" if side == "buy" else "short", "symbol": symbol, "entry_price": entry}
        if strategy:
            parsed["strategy"] = strategy
        store.record_signal(sid, parsed, {"action": "BUY" if side == "buy" else "SELL", "symbol": symbol})
        store._write("UPDATE signals SET received_at=? WHERE signal_id=?", (t_in, sid))
        store.apply_ack({"signal_id": sid, "ok": True, "order": pos, "fill_price": entry, "volume": lots})
    store.add_deal({"deal": pos * 10, "order": pos, "position_id": pos, "symbol": symbol, "side": side,
                    "entry": "in", "volume": lots, "price": entry, "profit": 0, "commission": commission / 2,
                    "swap": 0, "time": t_in, "magic": magic, "comment": comment})
    if exit_ is not None:
        store.add_deal({"deal": pos * 10 + 1, "order": pos + 1, "position_id": pos, "symbol": symbol,
                        "side": "sell" if side == "buy" else "buy", "entry": "out", "volume": lots,
                        "price": exit_, "profit": profit, "commission": commission / 2, "swap": swap,
                        "time": t_out, "reason": reason, "magic": magic})


def _closed(store, f=None):
    return {t["position_id"]: t for t in store.overview(f or store.filters("all"))["trades"]}


# ---- per-trade PnL ------------------------------------------------------------------

def test_closed_trade_row(store):
    _trade(store, 1, side="sell", entry=1.10100, exit_=1.10000, lots=0.2, profit=20.0,
           commission=-1.4, swap=-0.3, strategy="igt")
    t = _closed(store)[1]
    assert t["side"] == "sell" and t["lots"] == 0.2
    assert t["entry_price"] == 1.101 and t["exit_price"] == 1.1
    assert t["pips"] == pytest.approx(10.0)
    assert t["gross_pnl"] == 20.0 and t["costs"] == pytest.approx(-1.7)
    assert t["net_pnl"] == pytest.approx(18.3)
    assert t["duration_s"] == 600 and t["strategy"] == "igt"


def test_jpy_pips(store):
    _trade(store, 2, symbol="USDJPY", entry=150.000, exit_=149.800, profit=-13.4)
    assert _closed(store)[2]["pips"] == pytest.approx(-20.0)


def test_open_position_is_not_a_closed_trade(store):
    _trade(store, 3, exit_=None)
    assert _closed(store) == {}
    assert store.positions()[0]["closed"] is False


# ---- attribution -------------------------------------------------------------------

def test_broker_sl_close_keeps_the_entry_signals_strategy(store):
    # No exit signal at all: the broker closed it. Matched by position ticket.
    _trade(store, 4, profit=-15.0, reason="sl", strategy="sflow-v2")
    t = _closed(store)[4]
    assert t["strategy"] == "sflow-v2" and t["exit_reason"] == "sl" and t["exit_signal_id"] is None


def test_magic_number_attributes_without_a_stored_signal(store):
    # The entry ack was never stored, but the deal carries the strategy magic.
    _trade(store, 5, strategy="igt")     # makes "igt" a known tag
    magic = 777777 * 1000 + translate.strategy_slot("igt")
    _trade(store, 6, with_signal=False, magic=magic)
    assert _closed(store)[6]["strategy"] == "igt"


def test_comment_attributes_when_magic_is_plain(store):
    _trade(store, 7, strategy="sdx2")
    _trade(store, 8, with_signal=False, magic=777777, comment="sdx2|41821")
    assert _closed(store)[8]["strategy"] == "sdx2"


def test_untagged_and_backfill(store):
    _trade(store, 9, symbol="AUDUSD", t_in=1000, t_out=1100)
    _trade(store, 10, symbol="AUDUSD", t_in=5000, t_out=5100)
    _trade(store, 11, symbol="EURUSD", t_in=1000, t_out=1100, strategy="tagged")
    assert _closed(store)[9]["strategy"] == "untagged"
    store.add_backfill("audusd", "SFLOW v2", before_ts=2000)
    store.add_backfill("EURUSD", "other", before_ts=2000)
    trades = _closed(store)
    assert trades[9]["strategy"] == "sflow-v2"
    assert trades[10]["strategy"] == "untagged"     # opened after the rule's cutoff
    assert trades[11]["strategy"] == "tagged"       # a tag in the data beats a rule
    sig = [s for s in store.overview(store.filters("all"))["signals"] if s["signal_id"] == "e9"][0]
    assert sig["strategy"] == "sflow-v2"


def test_backfill_cli(store, capsys):
    from app import backfill
    _trade(store, 12, symbol="USDJPY", entry=150.0, exit_=150.1)
    assert backfill.main(["--db", store.path, "USDJPY=sdx2"]) == 0
    out = capsys.readouterr().out
    assert "USDJPY" in out and "sdx2" in out
    assert _closed(store)[12]["strategy"] == "sdx2"


# ---- stats ---------------------------------------------------------------------------

def test_trade_stats():
    trades = [
        {"net_pnl": 10, "pips": 10, "closed_at": 1},
        {"net_pnl": -5, "pips": -5, "closed_at": 2},
        {"net_pnl": -10, "pips": -10, "closed_at": 3},
        {"net_pnl": 30, "pips": 30, "closed_at": 4},
        {"net_pnl": 0, "pips": 0, "closed_at": 5},
    ]
    s = metrics.trade_stats(trades)
    assert s["trades"] == 5 and s["wins"] == 2 and s["win_rate"] == 0.4
    assert s["net_pnl"] == 25 and s["expectancy"] == 5 and s["expectancy_pips"] == 5
    assert s["avg_win"] == 20 and s["avg_loss"] == -7.5
    assert s["profit_factor"] == pytest.approx(40 / 15)
    assert s["largest_win"] == 30 and s["largest_loss"] == -10
    assert s["max_drawdown"] == 15          # +10 peak, then -5, -10 -> -5


def test_trade_stats_edge_cases():
    assert metrics.trade_stats([])["expectancy"] is None
    only_wins = metrics.trade_stats([{"net_pnl": 5, "closed_at": 1}])
    assert only_wins["profit_factor"] is None and only_wins["max_drawdown"] == 0


# ---- runs ------------------------------------------------------------------------------

def test_runs_filter_by_open_time_and_keep_everything(store):
    _trade(store, 20, t_in=1000, t_out=1100, profit=10)
    run = store.start_run("  SFLOW   v2 test ")
    assert run["name"] == "SFLOW v2 test"
    store._write("UPDATE runs SET started_at=? WHERE id=?", (2000, run["id"]))
    _trade(store, 21, t_in=3000, t_out=3100, profit=-4, commission=0)

    current = store.overview()          # default = the latest run
    assert current["selected"]["run"] == run["id"]
    assert [t["position_id"] for t in current["trades"]] == [21]
    assert current["kpis"]["net_pnl"] == -4
    everything = store.overview(store.filters("all"))
    assert {t["position_id"] for t in everything["trades"]} == {20, 21}
    assert everything["kpis"]["signals_total"] == 2
    assert len(store._rows("SELECT * FROM deals")) == 4      # nothing deleted


def test_earlier_run_has_an_end(store):
    a = store.start_run("first")
    b = store.start_run("second")
    store._write("UPDATE runs SET started_at=? WHERE id=?", (100, a["id"]))
    store._write("UPDATE runs SET started_at=? WHERE id=?", (200, b["id"]))
    _trade(store, 30, t_in=150, t_out=250)
    _trade(store, 31, t_in=250, t_out=260)
    first = store.overview(store.filters(a["id"]))
    assert first["run"]["ended_at"] == 200
    assert [t["position_id"] for t in first["trades"]] == [30]


def test_unknown_run_and_empty_name(store):
    with pytest.raises(ValueError):
        store.filters(999)
    with pytest.raises(ValueError):
        store.start_run("   ")


# ---- filters & breakdown -------------------------------------------------------------------

def test_symbol_and_strategy_filters_and_breakdown(store):
    _trade(store, 40, symbol="EURUSD", strategy="igt", profit=10, commission=0)
    _trade(store, 41, symbol="EURUSD", strategy="sflow", profit=-5, commission=0)
    _trade(store, 42, symbol="USDJPY", entry=150.0, exit_=150.1, strategy="sflow", profit=7, commission=0)
    reports.handle(store, json.dumps({"type": "account", "equity": 1000, "magic": 777777,
                                      "positions": [{"ticket": 42, "symbol": "USDJPY"}]}))
    o = store.overview(store.filters("all"))
    assert o["options"]["symbols"] == ["EURUSD", "USDJPY"]
    assert o["options"]["strategies"] == ["igt", "sflow"]
    by_sym = {r["symbol"]: r for r in o["breakdown"]["symbols"]}
    assert by_sym["EURUSD"]["trades"] == 2 and by_sym["EURUSD"]["net_pnl"] == 5
    assert by_sym["USDJPY"]["open"] == 1
    by_strat = {r["strategy"]: r for r in o["breakdown"]["strategies"]}
    assert by_strat["sflow"]["net_pnl"] == 2 and by_strat["sflow"]["win_rate"] == 0.5
    assert by_strat["sflow"]["profit_factor"] == pytest.approx(7 / 5)

    sflow = store.overview(store.filters("all", strategy="sflow"))
    assert {t["position_id"] for t in sflow["trades"]} == {41, 42}
    assert sflow["chart"]["kind"] == "pnl" and sflow["chart"]["points"][-1]["equity"] == 2
    assert sflow["options"]["strategies"] == ["igt", "sflow"]     # chips still offer the others
    eur_sflow = store.overview(store.filters("all", symbol="eurusd", strategy="sflow"))
    assert [t["position_id"] for t in eur_sflow["trades"]] == [41]
    assert eur_sflow["positions"] == []


# ---- dashboard API: runs and export ------------------------------------------------------

def test_runs_api(store):
    c = create_app(store, password="").test_client()
    assert c.post("/api/runs", data="name=x").status_code == 415
    assert c.post("/api/runs", json={"name": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
    r = c.post("/api/runs", json={"name": "SFLOW v2 test"}, headers={"Origin": "http://localhost"})
    assert r.status_code == 201 and r.get_json()["name"] == "SFLOW v2 test"
    assert c.get("/api/overview").get_json()["run"]["name"] == "SFLOW v2 test"
    assert c.get("/api/overview?run=all").get_json()["run"] is None
    assert c.get("/api/overview?run=12345").status_code == 400


def _unzip(resp):
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    files = {name.split("/")[-1]: list(csv.DictReader(io.StringIO(z.read(name).decode()))) for name in z.namelist()}
    return files


def test_export_respects_filters(store):
    _trade(store, 50, symbol="EURUSD", strategy="igt", profit=10, commission=-1)
    _trade(store, 51, symbol="USDJPY", entry=150.0, exit_=150.1, strategy="igt", profit=7)
    store.record_signal("evil", {"signal": "=HYPERLINK(\"x\")", "symbol": "EURUSD"}, None, "rejected", "-1+1")
    c = create_app(store, password="").test_client()

    r = c.get("/api/export?run=all&symbol=EURUSD")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    assert "pinebridge-all-time-eurusd-" in r.headers["Content-Disposition"]
    files = _unzip(r)
    assert set(files) == {"trades.csv", "signals.csv"}
    [t] = files["trades.csv"]
    assert t["position_id"] == "50" and t["strategy"] == "igt" and t["net_pnl"] == "9"
    assert t["pips"] == "10" and t["side"] == "buy" and t["open_time_utc"]
    sigs = {s["signal_id"]: s for s in files["signals.csv"]}
    assert set(sigs) == {"e50", "evil"}
    assert sigs["evil"]["signal"].startswith("'=")          # no spreadsheet formulas
    assert sigs["evil"]["detail"] == "'-1+1"
    assert sigs["e50"]["strategy_resolved"] == "igt"


def test_export_of_empty_run(store):
    store.start_run("fresh")
    files = _unzip(create_app(store, password="").test_client().get("/api/export"))
    assert files["trades.csv"] == [] and files["signals.csv"] == []


# ---- upgrading an existing database -------------------------------------------------------

def test_existing_database_is_upgraded_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE signals(signal_id TEXT PRIMARY KEY, received_at REAL, signal TEXT, action TEXT,
            symbol TEXT, volume REAL, tv_price REAL, tv_time REAL, zone TEXT, status TEXT, detail TEXT,
            retcode INTEGER, sent_at REAL, acked_at REAL, ea_ms REAL, order_ticket INTEGER,
            requested_price REAL, fill_price REAL, fill_volume REAL, tick_size REAL, tick_value REAL,
            pip_size REAL, closed_tickets TEXT);
        CREATE TABLE deals(deal INTEGER PRIMARY KEY, order_ticket INTEGER, position_id INTEGER, symbol TEXT,
            side TEXT, entry TEXT, volume REAL, price REAL, profit REAL, commission REAL, swap REAL,
            time REAL, reason TEXT);
        INSERT INTO signals(signal_id, received_at, signal, action, symbol, order_ticket)
            VALUES ('old', 1000, 'long', 'BUY', 'EURUSD', 77);
        INSERT INTO deals VALUES (1, 77, 77, 'EURUSD', 'buy', 'in', 0.1, 1.1, 0, 0, 0, 1000, 'ea');
        INSERT INTO deals VALUES (2, 78, 77, 'EURUSD', 'sell', 'out', 0.1, 1.101, 10, 0, 0, 1100, 'sl');
    """)
    db.commit()
    db.close()
    store = Store(path)
    [t] = store.overview(store.filters("all"))["trades"]
    assert t["strategy"] == "untagged" and t["net_pnl"] == 10 and t["entry_signal_id"] == "old"
