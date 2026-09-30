"""The Pinebridge Dashboard: a separate Flask app on its own port
(DASHBOARD_PORT), never the webhook port, because it shows account data.
Keep that port on the LAN -- don't route it through the Cloudflare tunnel.

Everything is served from this package: no CDN or external requests.
"""
import hmac
import os

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config

STATIC = os.path.join(os.path.dirname(__file__), "static")


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

    @app.route("/")
    def index():
        return send_from_directory(STATIC, "dashboard.html")

    @app.route("/pinebridge.svg")
    def logo():
        return send_from_directory(STATIC, "pinebridge.svg")

    @app.route("/api/overview")
    def overview():
        return jsonify(store.overview())

    return app
