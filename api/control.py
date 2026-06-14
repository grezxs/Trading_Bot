"""Vercel serverless — POST /api/control

Writes runtime/control.json so the running bot picks up Enable / Stop / Kill.
Body: {"trading_enabled": bool, "kill_switch": bool}
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_CONTROL = Path(__file__).resolve().parents[1] / "runtime" / "control.json"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            cmd = json.loads(body)
            payload = {
                "trading_enabled": bool(cmd.get("trading_enabled", True)),
                "kill_switch": bool(cmd.get("kill_switch", False)),
                "updated": time.time(),
            }
            _CONTROL.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CONTROL.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, _CONTROL)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as exc:
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *_):
        pass
