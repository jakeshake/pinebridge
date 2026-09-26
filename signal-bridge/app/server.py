#!/usr/bin/env python3
"""TradingView Webhook -> MT5 ZeroMQ Bridge.

Architecture:
    TradingView Alert -> POST /webhook -> Flask -> ZeroMQ PUSH -> MT5 EA (PULL)
"""
import hmac
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from flask import Flask, jsonify, request

from . import config, translate, zmq_client
from .parser import parse_alert


def setup_logging():
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(config.LOG_LEVEL)
    logger.addHandler(console_handler)

    # Host-mounted log dirs (e.g. Unraid appdata, owned by nobody:users) are
    # often not writable by this container's non-root user; don't die over it.
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            os.path.join(config.LOG_DIR, "webhook.log"),
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning(f"File logging disabled ({config.LOG_DIR} not writable: {exc}); logging to console only")

    return logger


logger = setup_logging()

problems = config.validate()
for problem in problems:
    logger.error(f"CONFIG ERROR: {problem}")
if problems:
    logger.error("Refusing to start with invalid configuration. See errors above.")
    sys.exit(1)

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

zmq_client.connect()


def _raw_body():
    # request.data is empty for form-encoded bodies (curl -d's default), so
    # read the raw bytes whatever the Content-Type is.
    return request.get_data()


def _redact(text):
    if config.WEBHOOK_SECRET:
        text = text.replace(config.WEBHOOK_SECRET, "<secret>")
    return text


def _without_secret(parsed_data):
    return {k: v for k, v in parsed_data.items() if k != "secret"}


def _secret_ok(parsed_data):
    if not config.REQUIRE_SECRET:
        return True
    supplied = str(parsed_data.get("secret", ""))
    return hmac.compare_digest(supplied, config.WEBHOOK_SECRET)


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify(
        {
            "status": "healthy",
            "zmq_address": zmq_client.ADDRESS,
            "zmq_connected": zmq_client.is_connected(),
        }
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    raw_data = _raw_body()
    logger.info("=" * 60)
    logger.info(f"Webhook received. Raw: {_redact(raw_data.decode('utf-8', 'replace'))}")

    try:
        parsed_data = parse_alert(raw_data)
    except Exception as exc:
        logger.error(f"Parse error: {exc}")
        return jsonify({"status": "error", "message": f"Parse error: {exc}"}), 400

    if not _secret_ok(parsed_data):
        logger.warning("Rejected webhook: missing or invalid secret")
        return jsonify({"status": "error", "message": "Invalid or missing secret"}), 401

    try:
        ea_signal = translate.translate(parsed_data)
    except ValueError as exc:
        logger.error(f"Translate error: {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 400

    logger.info(f"Signal: {translate.describe(ea_signal)}")

    if ea_signal["action"] not in translate.VALID_ACTIONS:
        logger.error(f"Invalid action: {ea_signal['action']}")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Invalid action: {ea_signal['action']}. "
                    f"Valid: {sorted(translate.VALID_ACTIONS)}",
                }
            ),
            400,
        )

    success, latency = zmq_client.send(ea_signal)
    logger.info(f"{'OK' if success else 'BUFFERED'} - ZMQ latency: {latency:.3f}ms")
    logger.info("=" * 60)

    return jsonify(
        {
            "status": "success" if success else "buffered",
            "signal": ea_signal,
            "zmq_latency_ms": round(latency, 3),
        }
    )


@app.route("/test", methods=["GET", "POST"])
def test_endpoint():
    if request.method == "GET":
        return jsonify(
            {
                "status": "ready",
                "zmq_address": zmq_client.ADDRESS,
                "zmq_connected": zmq_client.is_connected(),
                "supported_signals": {
                    "entry": ["long", "short", "buy", "sell"],
                    "exit": [
                        "closelong",
                        "closeshort",
                        "closealllong",
                        "closeallshort",
                        "close",
                        "closeall",
                        "exitlong",
                        "exitshort",
                        "exit",
                    ],
                    "modify": ["modify"],
                },
                "example": (
                    "signal=long,symbol=EURUSD,qty=100000,entry_price=1.0850,"
                    "sl_price=1.0800,tp_price=1.0950,secret=YOUR_SECRET"
                ),
            }
        )

    raw_data = _raw_body()
    send_it = request.args.get("send", "0") == "1"

    try:
        parsed = parse_alert(raw_data)
        if not _secret_ok(parsed):
            return jsonify({"status": "error", "message": "Invalid or missing secret"}), 401
        translated = translate.translate(parsed)

        zmq_result = None
        if send_it:
            success, latency = zmq_client.send(translated)
            zmq_result = {"sent": success, "latency_ms": latency}

        return jsonify(
            {"status": "success", "parsed": _without_secret(parsed), "translated": translated, "zmq": zmq_result}
        )
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


def main():
    logger.info("=" * 60)
    logger.info("TradingView -> MT5 Signal Bridge")
    logger.info(f"Flask:  http://{config.FLASK_HOST}:{config.FLASK_PORT}")
    logger.info(f"ZeroMQ: {zmq_client.ADDRESS} (PUSH -> MT5 PULL)")
    logger.info(f"Webhook secret required: {config.REQUIRE_SECRET}")
    logger.info("=" * 60)
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
