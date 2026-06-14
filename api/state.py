"""Vercel serverless — GET /api/state

Reads runtime/state.json and returns it as JSON.
Returns 204 if the file doesn't exist (bot not started yet).
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_STATE = Path(__file__).resolve().parents[1] / "runtime" / "state.json"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _STATE.exists():
            self.send_response(204)
            self.end_headers()
            return
        try:
            body = _STATE.read_bytes()
        except Exception:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass
