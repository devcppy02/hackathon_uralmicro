"""A machine-wide proxy must not intercept requests to the local model."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from tests.test_llm import llm_response


@contextmanager
def http_server(status, body):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append(self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_model_bypasses_proxy(monkeypatch, tmp_path, call_json, extraction):
    body = json.dumps(llm_response(extraction)).encode()
    with (
        http_server(403, b'{"error":"proxy denied local address"}') as (proxy, proxy_requests),
        http_server(200, body) as (model, model_requests),
    ):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(key, proxy)
            monkeypatch.setenv(key.lower(), proxy)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")

        # Reproduce the observed 403 before checking that the app avoids it.
        with httpx.Client(timeout=5) as client:
            assert client.post(model + "/v1/chat/completions", json={}).status_code == 403
        assert len(proxy_requests) == 1
        settings = Settings(
            _env_file=None, database_path=tmp_path / "proxy.sqlite3", llm_base_url=model
        )
        with TestClient(create_app(settings)) as client:
            response = client.post("/calls", json=[call_json])
            assert response.status_code == 200, response.text
            assert response.json()["tasks_created"] == 1
        assert len(proxy_requests) == 1
        assert model_requests == ["/v1/chat/completions"]
