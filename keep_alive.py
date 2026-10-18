"""keep_alive.py — minimal HTTP ping server to prevent idle shutdown on Replit/Render."""
import threading
from flask import Flask

_app = Flask(__name__)


@_app.route("/")
def home():
    return "alive", 200


@_app.route("/health")
def health():
    return {"status": "ok"}, 200


def keep_alive() -> None:
    t = threading.Thread(
        target=lambda: _app.run(host="0.0.0.0", port=8080, use_reloader=False),
        daemon=True,
    )
    t.start()
