"""SQLite store behind the dashboard: webhook signals, the EA's execution
reports (acks, deals, account heartbeats) and the queries the dashboard
reads. Lives in the bridge's /config volume so it survives updates.

Every write is best-effort from the caller's point of view: the webhook
path wraps store calls so a storage problem can never block a trade.
"""
import json
import sqlite3
import threading
import time

from . import metrics
from .translate import get_pip_size

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
    signal_id TEXT PRIMARY KEY,
    received_at REAL,
    signal TEXT, action TEXT, symbol TEXT, volume REAL,
    tv_price REAL, tv_time REAL, zone TEXT,
    status TEXT, detail TEXT, retcode INTEGER,
    sent_at REAL, acked_at REAL, ea_ms REAL,
    order_ticket INTEGER, requested_price REAL, fill_price REAL, fill_volume REAL,
    tick_size REAL, tick_value REAL, pip_size REAL,
    closed_tickets TEXT
);
CREATE INDEX IF NOT EXISTS signals_received ON signals(received_at);
CREATE INDEX IF NOT EXISTS signals_order ON signals(order_ticket);
CREATE TABLE IF NOT EXISTS deals(
    deal INTEGER PRIMARY KEY,
    order_ticket INTEGER, position_id INTEGER, symbol TEXT,
    side TEXT, entry TEXT, volume REAL, price REAL,
    profit REAL, commission REAL, swap REAL, time REAL, reason TEXT
);
CREATE INDEX IF NOT EXISTS deals_position ON deals(position_id);
CREATE TABLE IF NOT EXISTS account(
    ts REAL PRIMARY KEY,
    balance REAL, equity REAL, margin REAL, free_margin REAL
);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
"""

ENTRY_ACTIONS = {"BUY", "SELL"}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tv_reference_price(parsed):
    """TradingView's price for a signal: an explicit tv_price, else the
    price the strategy put in the alert itself."""
    for key in ("tv_price", "entry_price", "limit_price", "close_price"):
        value = _num(parsed.get(key))
        if value is not None:
            return value
    return None


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _write(self, sql, args=()):
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- webhook side -------------------------------------------------

    def record_signal(self, signal_id, parsed, ea_signal=None, status="received", detail=""):
        ea_signal = ea_signal or {}
        symbol = str(ea_signal.get("symbol") or parsed.get("symbol") or "").upper()
        self._write(
            "INSERT OR REPLACE INTO signals(signal_id, received_at, signal, action, symbol, volume,"
            " tv_price, tv_time, zone, status, detail, pip_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal_id, time.time(), str(parsed.get("signal", "")), ea_signal.get("action"),
                symbol, _num(ea_signal.get("size")), tv_reference_price(parsed),
                metrics.parse_tv_time(parsed.get("tv_time")),
                f"{parsed.get('zone_src', '')}#{parsed['zone_id']}" if "zone_id" in parsed else None,
                status, detail, get_pip_size(symbol) if symbol else None,
            ),
        )

    def mark_sent(self, signal_id, delivered):
        self._write(
            "UPDATE signals SET status=?, sent_at=? WHERE signal_id=?",
            ("sent" if delivered else "not delivered", time.time(), signal_id),
        )

    # ---- EA reports ----------------------------------------------------

    def apply_ack(self, r):
        closed = r.get("closed_tickets") or []
        self._write(
            "UPDATE signals SET status=?, detail=?, retcode=?, acked_at=?, ea_ms=?, order_ticket=?,"
            " requested_price=?, fill_price=?, fill_volume=?, tick_size=?, tick_value=?, closed_tickets=?"
            " WHERE signal_id=?",
            (
                "executed" if r.get("ok") else "failed", r.get("detail", ""), r.get("retcode"),
                time.time(), _num(r.get("ea_ms")), r.get("order") or None,
                _num(r.get("requested_price")) or None, _num(r.get("fill_price")) or None,
                _num(r.get("volume")) or None, _num(r.get("tick_size")), _num(r.get("tick_value")),
                "," + ",".join(str(t) for t in closed) + "," if closed else None,
                r.get("signal_id"),
            ),
        )

    def add_deal(self, r):
        self._write(
            "INSERT OR REPLACE INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("deal"), r.get("order"), r.get("position_id"), r.get("symbol"),
                r.get("side"), r.get("entry"), _num(r.get("volume")), _num(r.get("price")),
                _num(r.get("profit")) or 0.0, _num(r.get("commission")) or 0.0,
                _num(r.get("swap")) or 0.0, _num(r.get("time")), r.get("reason"),
            ),
        )

    def add_account(self, r):
        now = time.time()
        self._write(
            "INSERT OR REPLACE INTO account VALUES (?,?,?,?,?)",
            (now, _num(r.get("balance")), _num(r.get("equity")), _num(r.get("margin")),
             _num(r.get("free_margin"))),
        )
        self._write(
            "INSERT OR REPLACE INTO state VALUES ('ea', ?)",
            (json.dumps({**r, "seen_at": now}),),
        )

    def ea_status(self):
        rows = self._rows("SELECT value FROM state WHERE key='ea'")
        return json.loads(rows[0]["value"]) if rows else None

    def prune(self, days):
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        with self._lock:
            self._db.execute("DELETE FROM signals WHERE received_at < ?", (cutoff,))
            self._db.execute("DELETE FROM deals WHERE time < ?", (cutoff,))
            self._db.execute("DELETE FROM account WHERE ts < ?", (cutoff,))
            self._db.commit()

    # ---- dashboard queries -----------------------------------------------

    def positions(self):
        """Closed positions from the deals, joined to their entry and exit
        signals, with actual vs expected (TradingView-price) PnL."""
        deals = self._rows("SELECT * FROM deals WHERE position_id IS NOT NULL ORDER BY time")
        by_pos = {}
        for d in deals:
            by_pos.setdefault(d["position_id"], []).append(d)

        out = []
        for pos_id, ds in by_pos.items():
            vol_in = sum(d["volume"] or 0 for d in ds if d["entry"] == "in")
            vol_out = sum(d["volume"] or 0 for d in ds if d["entry"] in ("out", "out_by"))
            actual = sum((d["profit"] or 0) + (d["commission"] or 0) + (d["swap"] or 0) for d in ds)
            entry = self._rows("SELECT * FROM signals WHERE order_ticket=? LIMIT 1", (pos_id,))
            exit_ = self._rows(
                "SELECT * FROM signals WHERE closed_tickets LIKE ? ORDER BY received_at LIMIT 1",
                (f"%,{pos_id},%",),
            )
            entry = entry[0] if entry else None
            exit_ = exit_[0] if exit_ else None
            expected = None
            if entry and exit_:
                expected = metrics.expected_pnl(
                    metrics.side_of(entry["action"]), entry["tv_price"], exit_["tv_price"],
                    entry["tick_size"], entry["tick_value"], entry["fill_volume"] or vol_in,
                )
            out.append({
                "position_id": pos_id,
                "symbol": ds[0]["symbol"],
                "closed": vol_in > 0 and vol_out >= vol_in - 1e-9,
                "opened_at": ds[0]["time"],
                "closed_at": ds[-1]["time"],
                "actual_pnl": actual,
                "expected_pnl": expected,
                "exit_reason": ds[-1]["reason"] if vol_out else None,
            })
        return out

    def overview(self, signal_limit=100, equity_points=400):
        now = time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

        sigs = self._rows("SELECT * FROM signals ORDER BY received_at DESC")
        recent = []
        slips = []
        for s in sigs:
            side = metrics.side_of(s["action"])
            vs_tv = metrics.slippage(side, s["tv_price"], s["fill_price"], s["pip_size"],
                                     s["tick_size"], s["tick_value"], s["fill_volume"])
            vs_req = metrics.slippage(side, s["requested_price"], s["fill_price"], s["pip_size"],
                                      s["tick_size"], s["tick_value"], s["fill_volume"])
            row = {
                "signal_id": s["signal_id"], "received_at": s["received_at"],
                "signal": s["signal"], "action": s["action"], "symbol": s["symbol"],
                "status": s["status"], "detail": s["detail"], "retcode": s["retcode"],
                "zone": s["zone"], "tv_price": s["tv_price"], "fill_price": s["fill_price"],
                "volume": s["fill_volume"] or s["volume"],
                "slip_tv_pips": vs_tv and vs_tv["pips"], "slip_tv_money": vs_tv and vs_tv["money"],
                "slip_broker_pips": vs_req and vs_req["pips"],
                "tv_to_bridge_ms": (s["received_at"] - s["tv_time"]) * 1000 if s["tv_time"] else None,
                "bridge_to_ea_ms": (s["acked_at"] - s["sent_at"]) * 1000
                if s["acked_at"] and s["sent_at"] else None,
            }
            if len(recent) < signal_limit:
                recent.append(row)
            if vs_tv and vs_tv["pips"] is not None and s["action"] in ENTRY_ACTIONS:
                slips.append({"pips": vs_tv["pips"], "money": vs_tv["money"], "symbol": s["symbol"],
                              "at": s["received_at"]})

        entries = [s for s in sigs if s["action"] in ENTRY_ACTIONS]
        filled = [s for s in entries if s["status"] == "executed" and s["fill_price"]]
        positions = self.positions()
        closed = [p for p in positions if p["closed"]]
        wins = [p for p in closed if p["actual_pnl"] > 0]
        compared = [p for p in closed if p["expected_pnl"] is not None]

        def avg(values):
            values = [v for v in values if v is not None]
            return sum(values) / len(values) if values else None

        equity = self._rows("SELECT ts, equity, balance FROM account ORDER BY ts")
        if len(equity) > equity_points:
            step = len(equity) / equity_points
            equity = [equity[int(i * step)] for i in range(equity_points)] + [equity[-1]]
        ea = self.ea_status()

        net = self._rows("SELECT COALESCE(SUM(profit + commission + swap), 0) AS n FROM deals")[0]["n"]
        return {
            "generated_at": now,
            "kpis": {
                "signals_today": sum(1 for s in sigs if s["received_at"] >= midnight),
                "signals_total": len(sigs),
                "market_fill_rate": len(filled) / len(entries) if entries else None,
                "win_rate": len(wins) / len(closed) if closed else None,
                "closed_trades": len(closed),
                "net_pnl": net,
                "avg_slip_tv_pips": avg(r["pips"] for r in slips),
                "avg_slip_broker_pips": avg(r["slip_broker_pips"] for r in recent
                                            if r["action"] in ENTRY_ACTIONS),
                "avg_tv_to_bridge_ms": avg(r["tv_to_bridge_ms"] for r in recent),
                "avg_bridge_to_ea_ms": avg(r["bridge_to_ea_ms"] for r in recent),
                "equity": ea and ea.get("equity"),
                "currency": ea and ea.get("currency"),
                "expected_pnl": sum(p["expected_pnl"] for p in compared) if compared else None,
                "actual_pnl_compared": sum(p["actual_pnl"] for p in compared) if compared else None,
                "compared_trades": len(compared),
            },
            "signals": recent,
            "slippage": slips,
            "equity": equity,
            "positions": (ea or {}).get("positions", []),
            "ea": ea,
        }
