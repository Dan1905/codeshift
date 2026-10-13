"""Fake upstream services so the order service is tested over real HTTP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

STOCK = {"widget": 10, "gadget": 0}
PRICES = {"widget": 2.5, "gadget": 9.99}


class _Upstream(BaseHTTPRequestHandler):
    failures_left = 0

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts[0] == "stock":
            if _Upstream.failures_left > 0:
                _Upstream.failures_left -= 1
                return self._send(503, {"error": "busy"})
            if parts[1] not in STOCK:
                return self._send(404, {"error": "unknown sku"})
            return self._send(200, {"quantity": STOCK[parts[1]]})
        if parts[0] == "price":
            return self._send(200, {"unit_price": PRICES[parts[1]]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/reserve":
            return self._send(200, {"reservation_id": f"res-{payload['sku']}-{payload['quantity']}"})
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def upstream(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("INVENTORY_URL", url)
    monkeypatch.setenv("PRICING_URL", url)
    _Upstream.failures_left = 0
    yield _Upstream
    server.shutdown()
    server.server_close()


@pytest.fixture()
def api(upstream):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
