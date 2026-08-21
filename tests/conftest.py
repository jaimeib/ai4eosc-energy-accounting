"""Local HTTP mocks for Mimir and CIM, used by the end-to-end tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import pytest


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mimir_server():
    """A Mimir stand-in whose response is driven by ``handler.series``.

    Tests set ``server.handler.series`` (and optionally ``.window_fn``) before
    issuing requests; the handler builds the query_range response from it.
    """
    state = {"series": [], "window_fn": None, "by_query": {}}
    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs

            assert self.headers.get("Authorization") is not None
            qs = parse_qs(urlparse(self.path).query)
            start = float(qs["start"][0])
            end = float(qs["end"][0])
            query = qs["query"][0]

            if query in state["by_query"]:
                build = state["by_query"][query]
            else:
                build = state["window_fn"] or (lambda start, end: state["series"])
            result = build(start, end)

            body = {
                "status": "success",
                "data": {"resultType": "matrix", "result": result},
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class Server:
        endpoint = f"http://127.0.0.1:{port}"

        def set_series(self, series):
            state["series"] = series

        def set_window_fn(self, fn):
            state["window_fn"] = fn

        def set_query_series(self, query, series_or_fn):
            """Register a response for an exact PromQL ``query`` string,
            overriding the default series/window_fn for that query only.
            ``series_or_fn`` is either a fixed series list or a
            ``(start, end) -> series`` callable, same as set_series/
            set_window_fn."""
            if callable(series_or_fn):
                state["by_query"][query] = series_or_fn
            else:
                state["by_query"][query] = lambda start, end: series_or_fn

    yield Server()
    httpd.shutdown()


@pytest.fixture
def cim_server():
    """A GreenDIGIT CIM stand-in that records the last submitted payload."""
    state = {"payload": None, "fail": False}
    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"access_token": "fake-token"}).encode())

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            assert self.headers.get("Authorization") == "Bearer fake-token"
            if state["fail"]:
                self.send_response(500)
                self.end_headers()
                return
            state["payload"] = payload
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())

        def log_message(self, format, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class Server:
        base_url = f"http://127.0.0.1:{port}"

        @property
        def payload(self):
            return state["payload"]

        def set_fail(self, fail: bool):
            state["fail"] = fail

    yield Server()
    httpd.shutdown()
