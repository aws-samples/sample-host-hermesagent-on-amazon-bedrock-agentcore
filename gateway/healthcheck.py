"""Minimal HTTP health-check server for ECS Fargate.

Runs in a background thread inside main.py.  ECS health checks hit
GET /health on port 8080.
"""

from __future__ import annotations

import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("healthcheck")

_healthy = True


def set_healthy(value: bool) -> None:
    global _healthy
    _healthy = value


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" and _healthy:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"status":"unhealthy"}')

    def log_message(self, format, *args):
        pass  # Suppress access logs


def start_health_server(port: int = 8080) -> threading.Thread:
    """Start the health-check HTTP server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="healthcheck")
    thread.start()
    logger.info("Health-check server listening on :%d", port)
    return thread
