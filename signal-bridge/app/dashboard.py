"""The Pinebridge Dashboard: a separate Flask app on its own port
(DASHBOARD_PORT), never the webhook port, because it shows account data.
Keep that port on the LAN -- don't route it through the Cloudflare tunnel.

Everything is served from this package: no CDN or external requests.
"""
import csv
import hmac
import io
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config

STATIC = os.path.join(os.path.dirname(__file__), "static")

TRADE_COLUMNS = [
    "position_id", "strategy", "symbol", "side", "lots", "open_time_utc", "close_time_utc",
    "duration_min", "entry_price", "exit_price", "pips", "gross_pnl", "commission", "swap",
    "net_pnl", "exit_reason", "expected_pnl_tv", "entry_slip_tv_pips", "entry_signal_id", "exit_signal_id",
]
SIGNAL_TIME_COLUMNS = ("received_at", "tv_time", "sent_at", "acked_at")


def _iso(ts):
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _cell(value):
    """CSV-safe cell: round floats, and stop spreadsheet apps from running
    text that starts like a formula (alert text comes from outside)."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv(columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue()


def _trade_row(t):
    return {
        **t,
        "open_time_utc": _iso(t["opened_at"]),
        "close_time_utc": _iso(t["closed_at"]),
        "duration_min": round(t["duration_s"] / 60, 1) if t["duration_s"] is not None else None,
        "lots": t["lots"],
        "pips": round(t["pips"], 1) if t["pips"] is not None else None,
        "gross_pnl": round(t["gross_pnl"], 2),
        "commission": round(t["commission"], 2),
        "swap": round(t["swap"], 2),
        "net_pnl": round(t["net_pnl"], 2),
        "expected_pnl_tv": round(t["expected_pnl"], 2) if t["expected_pnl"] is not None else None,
        "entry_slip_tv_pips": round(t["entry_slip_tv_pips"], 2) if t["entry_slip_tv_pips"] is not None else None,
    }


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "run"


def create_app(store, password=None):
    password = config.DASHBOARD_PASSWORD if password is None else password
    app = Flask(__name__, static_folder=None)

    @app.before_request
    def _auth():
        if not password:
            return None
        auth = request.authorization
        supplied = (auth.password or "") if auth else ""
        if hmac.compare_digest(supplied.encode(), password.encode()):
            return None
        return Response("Pinebridge dashboard: password required", 401,
                        {"WWW-Authenticate": 'Basic realm="Pinebridge"'})

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'"
        )
        return resp

    def _filters():
        return store.filters(request.args.get("run"), request.args.get("symbol"), request.args.get("strategy"))

    @app.route("/")
    def index():
        return send_from_directory(STATIC, "dashboard.html")

    @app.route("/pinebridge.svg")
    def logo():
        return send_from_directory(STATIC, "pinebridge.svg")

    @app.route("/api/overview")
    def overview():
        try:
            f = _filters()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(store.overview(f))

    @app.route("/api/runs", methods=["GET", "POST"])
    def runs():
        if request.method == "GET":
            return jsonify(store.runs())
        # State-changing: only from this page. A JSON body can't be sent
        # cross-site without a CORS preflight (which we never answer), and a
        # browser's Origin must be this dashboard's own.
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return jsonify({"error": "cross-origin request refused"}), 403
        if not request.is_json:
            return jsonify({"error": "send JSON: {\"name\": \"...\"}"}), 415
        try:
            run = store.start_run((request.get_json(silent=True) or {}).get("name"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(run), 201

    @app.route("/api/export")
    def export():
        try:
            f = _filters()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        trades, signals = store.export(f)
        signal_columns = list(signals[0].keys()) if signals else ["signal_id", "received_at", "strategy_resolved"]
        for s in signals:
            for c in SIGNAL_TIME_COLUMNS:
                s[c] = _iso(s.get(c))

        parts = ["pinebridge", _slug(f.run["name"]) if f.run else "all-time"]
        parts += [_slug(v) for v in (f.symbol, f.strategy) if v]
        name = "-".join(parts) + "-" + time.strftime("%Y%m%d-%H%M")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{name}/trades.csv", _csv(TRADE_COLUMNS, [_trade_row(t) for t in trades]))
            z.writestr(f"{name}/signals.csv", _csv(signal_columns, signals))
        return Response(buf.getvalue(), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    return app
