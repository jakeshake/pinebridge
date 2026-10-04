"""SQLite store behind the dashboard: webhook signals, the EA's execution
reports (acks, deals, account heartbeats), runs, and the queries the
dashboard reads. Lives in the bridge's /config volume so it survives
updates.

Signals and deals are the raw record and are never deleted: "Start new
run" only moves the dashboard's default time window. Only the account
heartbeats (one every ~30 s, used for the equity chart) are pruned.

Every write is best-effort from the caller's point of view: the webhook
path wraps store calls so a storage problem can never block a trade.
"""
import json
import sqlite3
import threading
import time

from . import metrics
from .translate import UNTAGGED, get_pip_size, normalize_strategy, strategy_slot

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
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_backfill(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, strategy TEXT NOT NULL,
    before_ts REAL NOT NULL, created_at REAL
);
"""

# Columns added after the first release: (table, column, type). Added in
# place on startup so an existing pinebridge.db keeps all its history.
MIGRATIONS = [
    ("signals", "strategy", "TEXT"),
    ("deals", "magic", "INTEGER"),
    ("deals", "comment", "TEXT"),
]

ENTRY_ACTIONS = {"BUY", "SELL"}
DEFAULT_MAGIC = 777777       # the EA's MagicNumber input default
OUT_ENTRIES = ("out", "out_by")
CLOSED_TRADES_SHOWN = 500    # the page; exports have every trade


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value)
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


def _vwap(deals):
    vol = sum(d["volume"] or 0 for d in deals)
    if not vol:
        return None
    return sum((d["price"] or 0) * (d["volume"] or 0) for d in deals) / vol


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


class Filters:
    """What the dashboard is looking at: a run's time window (or all
    time), and optionally one symbol and one strategy."""

    def __init__(self, run=None, start=None, end=None, symbol=None, strategy=None):
        self.run, self.start, self.end = run, start, end
        self.symbol = symbol or None
        self.strategy = strategy or None

    def in_window(self, ts):
        if ts is None:
            return self.start is None and self.end is None
        return (self.start is None or ts >= self.start) and (self.end is None or ts < self.end)

    def matches(self, symbol, strategy):
        return (self.symbol is None or symbol == self.symbol) and \
               (self.strategy is None or strategy == self.strategy)


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self._db.execute("CREATE INDEX IF NOT EXISTS signals_strategy ON signals(strategy)")
        self._db.commit()

    def _write(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- webhook side -------------------------------------------------

    def record_signal(self, signal_id, parsed, ea_signal=None, status="received", detail=""):
        ea_signal = ea_signal or {}
        symbol = str(ea_signal.get("symbol") or parsed.get("symbol") or "").upper()
        self._write(
            "INSERT OR REPLACE INTO signals(signal_id, received_at, signal, action, symbol, volume,"
            " tv_price, tv_time, zone, status, detail, pip_size, strategy)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal_id, time.time(), str(parsed.get("signal", "")), ea_signal.get("action"),
                symbol, _num(ea_signal.get("size")), tv_reference_price(parsed),
                metrics.parse_tv_time(parsed.get("tv_time")),
                f"{parsed.get('zone_src', '')}#{parsed['zone_id']}" if "zone_id" in parsed else None,
                status, detail, get_pip_size(symbol) if symbol else None,
                normalize_strategy(parsed.get("strategy")),
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
            "INSERT OR REPLACE INTO deals(deal, order_ticket, position_id, symbol, side, entry, volume,"
            " price, profit, commission, swap, time, reason, magic, comment)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("deal"), r.get("order"), r.get("position_id"), r.get("symbol"),
                r.get("side"), r.get("entry"), _num(r.get("volume")), _num(r.get("price")),
                _num(r.get("profit")) or 0.0, _num(r.get("commission")) or 0.0,
                _num(r.get("swap")) or 0.0, _num(r.get("time")), r.get("reason"),
                _int(r.get("magic")), r.get("comment"),
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
        """Drop account heartbeats older than `days`. Signals and deals are
        the trade record and are kept forever."""
        if days <= 0:
            return
        with self._lock:
            self._db.execute("DELETE FROM account WHERE ts < ?", (time.time() - days * 86400,))
            self._db.commit()

    # ---- runs ------------------------------------------------------------

    def runs(self):
        rows = self._rows("SELECT id, name, started_at FROM runs ORDER BY started_at, id")
        for i, r in enumerate(rows):
            r["ended_at"] = rows[i + 1]["started_at"] if i + 1 < len(rows) else None
        return rows

    def start_run(self, name):
        name = " ".join(str(name or "").split())[:60]
        if not name:
            raise ValueError("A run needs a name")
        run_id = self._write("INSERT INTO runs(name, started_at) VALUES (?, ?)", (name, time.time()))
        return next(r for r in self.runs() if r["id"] == run_id)

    def filters(self, run=None, symbol=None, strategy=None):
        """Build Filters from request values. run: a run id, "all", or None
        for the latest run (all time when no run was ever started)."""
        runs = self.runs()
        chosen = None
        if run in (None, "") and runs:
            chosen = runs[-1]
        elif run not in (None, "", "all"):
            chosen = next((r for r in runs if str(r["id"]) == str(run)), None)
            if chosen is None:
                raise ValueError(f"No run {run!r}")
        symbol = str(symbol).strip().upper()[:20] if symbol else None
        if strategy:
            strategy = UNTAGGED if str(strategy).strip().lower() == UNTAGGED else normalize_strategy(strategy)
        if chosen:
            return Filters(chosen, chosen["started_at"], chosen["ended_at"], symbol, strategy)
        return Filters(None, None, None, symbol, strategy)

    # ---- strategy backfill -------------------------------------------------

    def add_backfill(self, symbol, strategy, before_ts):
        """Attribute untagged trades on `symbol` opened before `before_ts` to
        `strategy`. Stored as a rule, so the raw signals stay as received;
        tags in the data itself always win over a rule."""
        tag = normalize_strategy(strategy)
        if not tag or not symbol:
            raise ValueError("backfill needs a symbol and a strategy tag")
        self._write(
            "INSERT INTO strategy_backfill(symbol, strategy, before_ts, created_at) VALUES (?,?,?,?)",
            (str(symbol).upper(), tag, float(before_ts), time.time()),
        )
        return tag

    def backfill_rules(self):
        return self._rows("SELECT * FROM strategy_backfill ORDER BY id")

    # ---- building blocks ------------------------------------------------

    def _load(self):
        sigs = self._rows("SELECT * FROM signals ORDER BY received_at")
        deals = self._rows("SELECT * FROM deals WHERE position_id IS NOT NULL ORDER BY time, deal")
        rules = self.backfill_rules()
        ea = self.ea_status()
        known = sorted({s["strategy"] for s in sigs if s["strategy"]} | {r["strategy"] for r in rules})
        slots = {strategy_slot(t): t for t in known}
        base_magic = _int((ea or {}).get("magic")) or DEFAULT_MAGIC

        def backfill(symbol, ts):
            for r in reversed(rules):
                if r["symbol"] == symbol and ts is not None and ts < r["before_ts"]:
                    return r["strategy"]
            return None

        def signal_strategy(s):
            return s["strategy"] or backfill(s["symbol"], s["received_at"]) or UNTAGGED

        def position_strategy(entry_sig, ins, symbol, opened_at):
            if entry_sig and entry_sig["strategy"]:
                return entry_sig["strategy"]
            for d in ins:    # magic MagicNumber*1000 + slot (EA v3.7+)
                magic = d.get("magic")
                if magic and magic // 1000 == base_magic and magic % 1000 in slots:
                    return slots[magic % 1000]
            for d in ins:    # order comment = the tag (the broker keeps it on the entry deal)
                tag = normalize_strategy((d.get("comment") or "").split("|")[0])
                if tag in known:
                    return tag
            return backfill(symbol, opened_at) or UNTAGGED

        return sigs, deals, ea, signal_strategy, position_strategy

    def _signal_rows(self, sigs, signal_strategy):
        out = []
        for s in sigs:
            side = metrics.fill_side(s["action"])
            vs_tv = metrics.slippage(side, s["tv_price"], s["fill_price"], s["pip_size"],
                                     s["tick_size"], s["tick_value"], s["fill_volume"])
            vs_req = metrics.slippage(side, s["requested_price"], s["fill_price"], s["pip_size"],
                                      s["tick_size"], s["tick_value"], s["fill_volume"])
            out.append({
                "signal_id": s["signal_id"], "received_at": s["received_at"],
                "signal": s["signal"], "action": s["action"], "symbol": s["symbol"],
                "strategy": signal_strategy(s),
                "status": s["status"], "detail": s["detail"], "retcode": s["retcode"],
                "zone": s["zone"], "tv_price": s["tv_price"], "fill_price": s["fill_price"],
                "volume": s["fill_volume"] or s["volume"],
                "slip_tv_pips": vs_tv and vs_tv["pips"], "slip_tv_money": vs_tv and vs_tv["money"],
                "slip_broker_pips": vs_req and vs_req["pips"],
                "tv_to_bridge_ms": (s["received_at"] - s["tv_time"]) * 1000 if s["tv_time"] else None,
                "bridge_to_ea_ms": (s["acked_at"] - s["sent_at"]) * 1000
                if s["acked_at"] and s["sent_at"] else None,
            })
        return out

    def _trades(self, sigs, deals, position_strategy):
        """One row per position from the EA's deal reports (grouped by
        position ID), so trades the broker closed itself (SL/TP, stop out)
        show up too. Each is matched to its Pinebridge entry signal by order
        ticket and to its exit signal by the tickets that signal closed."""
        entry_sig = {}
        exit_sig = {}
        for s in sigs:
            if s["order_ticket"]:
                entry_sig.setdefault(s["order_ticket"], s)
            for t in (s["closed_tickets"] or "").strip(",").split(","):
                if t.strip().isdigit():
                    exit_sig.setdefault(int(t), s)

        by_pos = {}
        for d in deals:
            by_pos.setdefault(d["position_id"], []).append(d)

        out = []
        for pos_id, ds in by_pos.items():
            ins = [d for d in ds if d["entry"] in ("in", "inout")]
            outs = [d for d in ds if d["entry"] in OUT_ENTRIES]
            vol_in = sum(d["volume"] or 0 for d in ins)
            vol_out = sum(d["volume"] or 0 for d in outs)
            symbol = ds[0]["symbol"]
            entry = entry_sig.get(pos_id) or next(
                (entry_sig[d["order_ticket"]] for d in ins if d["order_ticket"] in entry_sig), None)
            exit_ = exit_sig.get(pos_id)
            if ins:
                side = 1 if ins[0]["side"] == "buy" else -1
            elif outs:
                side = -1 if outs[0]["side"] == "buy" else 1   # closing a long is a sell
            else:
                side = 0
            entry_price, exit_price = _vwap(ins), _vwap(outs)
            pip = get_pip_size(symbol or "")
            gross = sum(d["profit"] or 0 for d in ds)
            costs = sum((d["commission"] or 0) + (d["swap"] or 0) for d in ds)
            opened_at = ins[0]["time"] if ins else ds[0]["time"]
            closed = vol_in > 0 and vol_out >= vol_in - 1e-9
            closed_at = outs[-1]["time"] if outs else None
            expected = None
            if entry and exit_:
                expected = metrics.expected_pnl(
                    metrics.side_of(entry["action"]), entry["tv_price"], exit_["tv_price"],
                    entry["tick_size"], entry["tick_value"], entry["fill_volume"] or vol_in,
                )
            entry_slip = None
            if entry and entry["action"] in ENTRY_ACTIONS:
                slip = metrics.slippage(metrics.side_of(entry["action"]), entry["tv_price"],
                                        entry["fill_price"], entry["pip_size"])
                entry_slip = slip and slip["pips"]
            out.append({
                "position_id": pos_id,
                "symbol": symbol,
                "strategy": position_strategy(entry, ins, symbol, opened_at),
                "side": "buy" if side > 0 else "sell" if side < 0 else None,
                "lots": vol_in or vol_out,
                "closed": closed,
                "opened_at": opened_at,
                "closed_at": closed_at if closed else None,
                "duration_s": closed_at - opened_at if closed and closed_at and opened_at else None,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pips": (exit_price - entry_price) * side / pip
                if entry_price is not None and exit_price is not None and side else None,
                "gross_pnl": gross,
                "commission": sum(d["commission"] or 0 for d in ds),
                "swap": sum(d["swap"] or 0 for d in ds),
                "costs": costs,
                "net_pnl": gross + costs,
                "actual_pnl": gross + costs,
                "expected_pnl": expected,
                "exit_reason": outs[-1]["reason"] if outs else None,
                "entry_signal_id": entry and entry["signal_id"],
                "exit_signal_id": exit_ and exit_["signal_id"],
                "entry_slip_tv_pips": entry_slip,
            })
        return out

    def positions(self):
        """Every position seen in the deals (closed or not), newest last."""
        sigs, deals, _ea, _ss, position_strategy = self._load()
        return self._trades(sigs, deals, position_strategy)

    def _open_positions(self, ea, deals, sigs, position_strategy, f):
        """The EA's live positions (heartbeat), tagged with their strategy."""
        ins_by_pos = {}
        for d in deals:
            if d["entry"] in ("in", "inout"):
                ins_by_pos.setdefault(d["position_id"], []).append(d)
        by_order = {s["order_ticket"]: s for s in sigs if s["order_ticket"]}
        out = []
        for p in (ea or {}).get("positions", []) or []:
            ticket = _int(p.get("ticket"))
            ins = ins_by_pos.get(ticket, [])
            if not ins and p.get("magic") is not None:
                ins = [{"magic": _int(p.get("magic")), "comment": p.get("comment")}]
            entry = by_order.get(ticket) or next((by_order[d["order_ticket"]] for d in ins
                                                  if d.get("order_ticket") in by_order), None)
            strategy = position_strategy(entry, ins, str(p.get("symbol") or "").upper(), _num(p.get("opened_at")))
            row = {**p, "strategy": strategy}
            if f.matches(str(p.get("symbol") or "").upper(), strategy):
                out.append(row)
        return out

    # ---- dashboard queries -----------------------------------------------

    def overview(self, f=None, signal_limit=100, equity_points=400):
        f = f or self.filters()
        now = time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

        sigs, deals, ea, signal_strategy, position_strategy = self._load()
        all_signals = self._signal_rows(sigs, signal_strategy)
        all_trades = self._trades(sigs, deals, position_strategy)

        # Chip options come from the whole run, not the current chip selection.
        run_signals = [s for s in all_signals if f.in_window(s["received_at"])]
        run_trades = [t for t in all_trades if f.in_window(t["opened_at"])]
        symbols = sorted({s["symbol"] for s in run_signals if s["symbol"]} |
                         {t["symbol"] for t in run_trades if t["symbol"]})
        strategies = sorted({s["strategy"] for s in run_signals} | {t["strategy"] for t in run_trades})

        signals = [s for s in run_signals if f.matches(s["symbol"], s["strategy"])]
        trades = [t for t in run_trades if f.matches(t["symbol"], t["strategy"])]
        closed = [t for t in trades if t["closed"]]
        open_positions = self._open_positions(ea, deals, sigs, position_strategy, f)

        entries = [s for s in signals if s["action"] in ENTRY_ACTIONS]
        filled = [s for s in entries if s["status"] == "executed" and s["fill_price"]]
        slips = [{"pips": s["slip_tv_pips"], "money": s["slip_tv_money"], "symbol": s["symbol"],
                  "strategy": s["strategy"], "at": s["received_at"]}
                 for s in entries if s["slip_tv_pips"] is not None]
        compared = [t for t in closed if t["expected_pnl"] is not None]
        recent = list(reversed(signals))[:signal_limit]
        stats = metrics.trade_stats(closed)

        kpis = {
            "signals_today": sum(1 for s in signals if s["received_at"] >= midnight),
            "signals_total": len(signals),
            "market_fill_rate": len(filled) / len(entries) if entries else None,
            "win_rate": stats["win_rate"],
            "closed_trades": len(closed),
            "net_pnl": stats["net_pnl"],
            "expectancy": stats["expectancy"],
            "expectancy_pips": stats["expectancy_pips"],
            "avg_win": stats["avg_win"],
            "avg_loss": stats["avg_loss"],
            "profit_factor": stats["profit_factor"],
            "largest_win": stats["largest_win"],
            "largest_loss": stats["largest_loss"],
            "max_drawdown": stats["max_drawdown"],
            "avg_slip_tv_pips": _avg(r["pips"] for r in slips),
            "avg_slip_broker_pips": _avg(r["slip_broker_pips"] for r in recent if r["action"] in ENTRY_ACTIONS),
            "avg_tv_to_bridge_ms": _avg(r["tv_to_bridge_ms"] for r in recent),
            "avg_bridge_to_ea_ms": _avg(r["bridge_to_ea_ms"] for r in recent),
            "equity": ea and ea.get("equity"),
            "currency": ea and ea.get("currency"),
            "expected_pnl": sum(t["expected_pnl"] for t in compared) if compared else None,
            "actual_pnl_compared": sum(t["net_pnl"] for t in compared) if compared else None,
            "compared_trades": len(compared),
        }

        # Account equity is account-wide, so it only applies unfiltered;
        # with a symbol/strategy chip the chart shows that slice's realised PnL.
        if f.symbol is None and f.strategy is None:
            args = []
            sql = "SELECT ts, equity, balance FROM account"
            if f.start is not None:
                sql += " WHERE ts >= ?" + (" AND ts < ?" if f.end is not None else "")
                args = [f.start] + ([f.end] if f.end is not None else [])
            points = self._rows(sql + " ORDER BY ts", args)
            if len(points) > equity_points:
                step = len(points) / equity_points
                points = [points[int(i * step)] for i in range(equity_points)] + [points[-1]]
            chart = {"kind": "equity", "points": points}
        else:
            cum, points = 0.0, []
            for t in sorted(closed, key=lambda t: t["closed_at"] or 0):
                cum += t["net_pnl"]
                points.append({"ts": t["closed_at"], "equity": cum})
            chart = {"kind": "pnl", "points": points}

        return {
            "generated_at": now,
            "runs": self.runs(),
            "selected": {"run": f.run["id"] if f.run else "all", "symbol": f.symbol, "strategy": f.strategy},
            "run": f.run,
            "options": {"symbols": symbols, "strategies": strategies},
            "kpis": kpis,
            "trades": sorted(closed, key=lambda t: t["closed_at"] or 0, reverse=True)[:CLOSED_TRADES_SHOWN],
            "breakdown": {
                "symbols": self._breakdown(trades, slips, open_positions, "symbol"),
                "strategies": self._breakdown(trades, slips, open_positions, "strategy"),
            },
            "signals": recent,
            "slippage": slips,
            "chart": chart,
            "equity": chart["points"] if chart["kind"] == "equity" else [],
            "positions": open_positions,
            "ea": ea,
        }

    @staticmethod
    def _breakdown(trades, slips, open_positions, key):
        """One row per symbol or per strategy: closed-trade stats, entry
        slippage vs TradingView, and how many positions are open now."""
        def pkey(p):
            return str(p.get("symbol") or "").upper() if key == "symbol" else p.get(key)

        groups = sorted({t[key] for t in trades if t[key]} | {pkey(p) for p in open_positions if pkey(p)})
        rows = []
        for g in groups:
            closed = [t for t in trades if t[key] == g and t["closed"]]
            stats = metrics.trade_stats(closed)
            rows.append({
                key: g,
                "trades": stats["trades"],
                "win_rate": stats["win_rate"],
                "net_pnl": stats["net_pnl"],
                "expectancy": stats["expectancy"],
                "profit_factor": stats["profit_factor"],
                "avg_slip_tv_pips": _avg(s["pips"] for s in slips if s[key] == g),
                "open": sum(1 for p in open_positions if pkey(p) == g),
            })
        return rows

    def export(self, f):
        """(closed trades, raw signals) for the CSV download, with the same
        filters as the dashboard but no row limits."""
        sigs, deals, _ea, signal_strategy, position_strategy = self._load()
        trades = [t for t in self._trades(sigs, deals, position_strategy)
                  if t["closed"] and f.in_window(t["opened_at"]) and f.matches(t["symbol"], t["strategy"])]
        trades.sort(key=lambda t: t["closed_at"] or 0)
        raw = []
        for s in sigs:
            strategy = signal_strategy(s)
            if f.in_window(s["received_at"]) and f.matches(s["symbol"], strategy):
                raw.append({**s, "strategy_resolved": strategy})
        return trades, raw
