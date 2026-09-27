import os
import sys
import tempfile

# app.server validates config and connects its ZeroMQ socket at import time.
os.environ.setdefault("ZMQ_HOST", "127.0.0.1")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
