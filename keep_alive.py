"""
keep_alive.py - Web Server & 5-Minute Self-Ping for Render Free Tier
"""

import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("keep_alive")


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Mind Map Telegram Bot is Alive and Running!".encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def _run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("Keep-Alive web server started on port %d", port)
    server.serve_forever()


def _self_ping_loop():
    time.sleep(15)
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return

    while True:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                logger.debug("Self-ping status: %s", response.status)
        except Exception as exc:
            logger.warning("Self-ping error: %s", exc)
        time.sleep(300)


def keep_alive():
    server_thread = threading.Thread(target=_run_web_server, daemon=True)
    server_thread.start()

    ping_thread = threading.Thread(target=_self_ping_loop, daemon=True)
    ping_thread.start()
