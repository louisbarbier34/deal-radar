"""
Lightweight HTTP server to receive Pipedream webhook payloads.
Run this alongside main.py if you want Pipedream → Rabbit integration.

Pipedream workflows that can POST here:
  - Google Meet transcript available (after meeting ends)
  - Zoom recording ready
  - New Gmail with deal keywords (as alternative to local polling)

Usage:
  python pipedream/webhook_server.py

Endpoints:
  POST /webhook/meeting-recap   → A2 processing
  POST /webhook/email-signal    → A3 processing
  GET  /health                  → OK
"""
from __future__ import annotations

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

import config

logger = logging.getLogger(__name__)

WEBHOOK_PORT = config.WEBHOOK_PORT
WEBHOOK_SECRET = config.WEBHOOK_SECRET  # Set WEBHOOK_SECRET in .env


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info("Webhook: " + format % args)

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "service": "Rabbit Webhook"})
        else:
            self._respond(404, {"error": "Not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        if self.path == "/webhook/meeting-recap":
            asyncio.run(self._handle_meeting_recap(payload))
            self._respond(200, {"status": "queued"})
        elif self.path == "/webhook/email-signal":
            asyncio.run(self._handle_email_signal(payload))
            self._respond(200, {"status": "queued"})
        else:
            self._respond(404, {"error": "Unknown endpoint"})

    async def _handle_meeting_recap(self, payload: dict):
        from handlers.a2_meeting_recap import process_pipedream_webhook
        result = await process_pipedream_webhook(payload)
        logger.info("Meeting recap processed: %s", result)

    async def _handle_email_signal(self, payload: dict):
        # Pipedream can send pre-parsed email signals here
        logger.info("Email signal from Pipedream: %s", payload.get("subject", "—"))

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


def run_webhook_server():
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    logger.info("Webhook server listening on port %d", WEBHOOK_PORT)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_webhook_server()
