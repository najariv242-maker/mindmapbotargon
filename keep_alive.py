"""
keep_alive.py - Conflict-Free Web Server & 5-Minute Self-Ping for Render
"""

import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("keep_alive")


class ReusableHTTPServer(HTTPServer):
    """جلوگیری از تداخل پورت (Port Conflict) هنگام ری‌استارت شدن در Render."""
    allow_reuse_address = True


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("OK - Mind Map Bot is Alive!".encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


_server_instance = None


def _run_web_server():
    global _server_instance
    port = int(os.environ.get("PORT", 10000))
    try:
        _server_instance = ReusableHTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info("Keep-Alive web server started on port %d", port)
        _server_instance.serve_forever()
    except Exception as exc:
        logger.warning("Keep-Alive server bind warning: %s", exc)


def _self_ping_loop():
    time.sleep(20)
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return

    while True:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                logger.debug("Self-ping status: %s", response.status)
        except Exception as exc:
            logger.debug("Self-ping error: %s", exc)
        time.sleep(300)


def keep_alive():
    global _server_instance
    if _server_instance is not None:
        return

    server_thread = threading.Thread(target=_run_web_server, daemon=True)
    server_thread.start()

    ping_thread = threading.Thread(target=_self_ping_loop, daemon=True)
    ping_thread.start()
