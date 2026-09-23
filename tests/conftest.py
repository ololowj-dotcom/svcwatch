from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

import pytest

from svcwatch.config import Config, parse_config
from svcwatch.models import Event
from svcwatch.notifiers import Notifier
from svcwatch.runner import CmdResult, CommandNotFound

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


class FakeRunner:
    def __init__(self) -> None:
        self.rules: List[tuple] = []
        self.calls: List[List[str]] = []

    def on(self, prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0,
           fn: Optional[Callable[[List[str]], CmdResult]] = None) -> None:
        self.rules.append((prefix.split(), fn or (lambda cmd, r=CmdResult(returncode, stdout, stderr): r)))

    def missing(self, program: str) -> None:
        def boom(cmd):
            raise CommandNotFound(f"'{program}' was not found on this machine")
        self.rules.append(([program], boom))

    def run(self, cmd: List[str], timeout: float = 30.0) -> CmdResult:
        self.calls.append(list(cmd))
        for prefix, fn in reversed(self.rules):
            if cmd[: len(prefix)] == prefix:
                return fn(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    def called(self, prefix: str) -> List[List[str]]:
        p = prefix.split()
        return [c for c in self.calls if c[: len(p)] == p]


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeNotifier(Notifier):
    def __init__(self, name: str = "fake", min_severity: str = "info") -> None:
        self.name = name
        self.min_severity = min_severity
        self.sent: List[Event] = []
        self.fail = False

    def send(self, event: Event, host: str, prefix: str) -> None:
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append(event)

    @property
    def titles(self) -> List[str]:
        return [e.title for e in self.sent]


def journal(*messages: Any, start: int = 1, priority: int = 6) -> str:
    lines = []
    for i, m in enumerate(messages, start):
        text, prio = (m if isinstance(m, tuple) else (m, priority))
        lines.append(json.dumps({"MESSAGE": text, "__CURSOR": f"c{i}", "PRIORITY": str(prio)}))
    return "\n".join(lines) + "\n"


def show(active: str = "active", sub: str = "running", load: str = "loaded", result: str = "success",
         restarts: Optional[int] = 0) -> str:
    out = f"LoadState={load}\nActiveState={active}\nSubState={sub}\nResult={result}\n"
    if restarts is not None:
        out += f"NRestarts={restarts}\n"
    return out


def make_cfg(toml_text: str = "", tmp_path=None, env: Optional[Dict[str, str]] = None) -> Config:
    from pathlib import Path
    base = Path(tmp_path) if tmp_path else Path.cwd()
    return parse_config(tomllib.loads(toml_text), env or {}, base)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def clock() -> Clock:
    return Clock()


class _Server:
    def __init__(self, handler_factory) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeTelegram:
    TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"

    def __init__(self) -> None:
        self.updates: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.requests: List[str] = []
        self.webhook = ""
        self.username = "watch_test_bot"
        self.fail_send: Optional[Dict[str, Any]] = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                parts = self.path.strip("/").split("/")
                token, method = parts[0][3:], parts[1]
                outer.requests.append(method)
                if token != outer.TOKEN:
                    return self._send({"ok": False, "error_code": 401, "description": "Unauthorized"})
                if method == "getMe":
                    return self._send({"ok": True, "result": {"id": 1, "username": outer.username}})
                if method == "getWebhookInfo":
                    return self._send({"ok": True, "result": {"url": outer.webhook}})
                if method == "getUpdates":
                    off = body.get("offset")
                    ups = [u for u in outer.updates if off is None or u["update_id"] >= off]
                    return self._send({"ok": True, "result": ups})
                if method == "sendMessage":
                    if outer.fail_send:
                        return self._send(outer.fail_send)
                    outer.sent.append(body)
                    return self._send({"ok": True, "result": {"message_id": len(outer.sent)}})
                return self._send({"ok": False, "error_code": 404, "description": "Not Found"})

            def _send(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _Server(Handler)

    @property
    def base(self) -> str:
        return self.server.base

    def message(self, chat_id: int, text: str = "/start", update_id: Optional[int] = None, **extra) -> None:
        uid = update_id or (len(self.updates) + 1)
        chat = {"id": chat_id, "type": "private", "first_name": "Tester"}
        self.updates.append({"update_id": uid, "message": {"chat": chat, "text": text, **extra}})


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.server.close()


class WebhookSink:
    def __init__(self, status: int = 200) -> None:
        self.received: List[Dict[str, Any]] = []
        self.headers: List[Dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.received.append(json.loads(self.rfile.read(length)))
                outer.headers.append(dict(self.headers))
                self.send_response(status)
                self.end_headers()

        self.server = _Server(Handler)

    @property
    def url(self) -> str:
        return self.server.base + "/hook"


@pytest.fixture
def webhook_sink():
    sink = WebhookSink()
    yield sink
    sink.server.close()


class PageServer:
    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/ok":
                    body = b"all systems ok"
                    self.send_response(200)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    body = b""
                elif self.path == "/error":
                    self.send_response(503)
                    body = b"maintenance"
                else:
                    self.send_response(404)
                    body = b"nope"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = _Server(Handler)

    @property
    def base(self) -> str:
        return self.server.base


@pytest.fixture
def pages():
    srv = PageServer()
    yield srv
    srv.server.close()
