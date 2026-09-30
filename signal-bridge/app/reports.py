"""Receives the EA's execution reports on a ZeroMQ PULL socket (the EA
connects and PUSHes) and hands them to the store.

Report types (JSON, one per message):
  ack      -- result of one signal, echoing its signal_id
  deal     -- every deal on the account for the EA's magic number,
              including broker-side SL/TP closes nobody signalled
  account  -- heartbeat: balance/equity/margin, broker connection,
              algo-trading permissions, open positions
"""
import json
import logging
import threading
import time

import zmq

logger = logging.getLogger(__name__)

# Seen by /health: when the EA last reported anything.
last_report_at = None


def handle(store, message):
    global last_report_at
    try:
        report = json.loads(message)
    except ValueError:
        logger.warning(f"Ignoring malformed EA report: {message[:200]!r}")
        return
    last_report_at = time.time()
    kind = report.get("type")
    if kind == "ack":
        store.apply_ack(report)
        logger.info(
            f"EA ack {report.get('signal_id')}: {'OK' if report.get('ok') else 'FAILED'} "
            f"{report.get('detail', '')}"
        )
    elif kind == "deal":
        store.add_deal(report)
    elif kind == "account":
        store.add_account(report)
    else:
        logger.warning(f"Unknown EA report type: {kind!r}")


def _serve(store, port):
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.RCVTIMEO, 1000)
    try:
        sock.bind(f"tcp://*:{port}")
    except zmq.ZMQError as exc:
        logger.error(f"EA reports disabled: can't listen on port {port} ({exc}); the webhook keeps working")
        return
    logger.info(f"Listening for EA reports on tcp://*:{port}")
    while True:
        try:
            message = sock.recv_string()
        except zmq.Again:
            continue
        try:
            handle(store, message)
        except Exception:  # a bad report must never kill the listener
            logger.exception("Failed to store EA report")


def start(store, port):
    thread = threading.Thread(target=_serve, args=(store, port), name="ea-reports", daemon=True)
    thread.start()
    return thread
