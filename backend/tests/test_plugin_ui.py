"""플러그인 자기 화면 — 정적 서빙·소켓 프록시·WS 프록시 (feature/plugins.md §8-2).

프록시 뒤의 "플러그인 서버"는 테스트가 유닉스 소켓에 직접 띄운다 (uvicorn 없이 — HTTP 는
표준 라이브러리, WS 는 `websockets`). 게이트웨이와 같은 기계라는 전제가 곧 소켓이다.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import socketserver
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import plugins as P
from app.services.plugins import registry

YAML = "name: {name}\nui: {ui}\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "plugins_dir", tmp_path / "plugins")
    monkeypatch.setattr(P, "BUNDLED_DIR", tmp_path / "bundled")
    (tmp_path / "plugins").mkdir()
    registry._owners.clear(); registry._runs.clear()
    return tmp_path / "plugins"


def _plugin(root: Path, name: str, ui: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "plugin.yaml").write_text(YAML.format(name=name, ui=ui))
    (d / "main.py").write_text("")
    return d


@pytest.fixture
def client():
    return TestClient(app)


# ── 정적 ──


def test_static_ui_is_served_with_spa_fallback_and_no_traversal(root, client):
    d = _plugin(root, "dash", "ui/")
    (d / "ui").mkdir()
    (d / "ui" / "index.html").write_text("<h1>hi</h1>")
    (d / "ui" / "app.js").write_text("console.log(1)")
    (d / "secret.txt").write_text("nope")

    assert client.get("/plugins/dash/ui/").text == "<h1>hi</h1>"
    assert client.get("/plugins/dash/ui/app.js").text == "console.log(1)"
    assert client.get("/plugins/dash/ui/some/route").text == "<h1>hi</h1>"        # SPA 폴백
    assert client.get("/plugins/dash/ui/../secret.txt").status_code in (404, 400)
    assert client.get("/plugins/dash/ui/%2e%2e/secret.txt").status_code in (404, 400)
    r = client.get("/plugins/dash/ui", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("/plugins/dash/ui/")


def test_no_ui_is_404_and_route_ui_redirects(root, client):
    _plugin(root, "plain", "false")
    assert client.get("/plugins/plain/ui/").status_code == 404
    _plugin(root, "routed", "/detector")
    r = client.get("/plugins/routed/ui/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/detector"


# ── 서버 프록시 ──


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        self.socket.bind(self.server_address)


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def _reply(self):
        body = json.dumps({"path": self.path, "method": self.command,
                           "prefix": self.headers.get("X-Forwarded-Prefix"),
                           "plugin": self.headers.get("X-Piper-Plugin"),
                           "body": self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream", "yes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


@pytest.fixture
def upstream(root):
    _plugin(root, "srv", "server")
    sock = registry.ui_socket("srv")
    srv = _UnixHTTPServer(str(sock), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield sock
    srv.shutdown()
    sock.unlink(missing_ok=True)


def test_server_ui_is_proxied_over_the_unix_socket(root, client, upstream):
    r = client.get("/plugins/srv/ui/some/page?x=1")
    assert r.status_code == 200 and r.headers["x-upstream"] == "yes"
    d = r.json()
    assert d["path"] == "/some/page?x=1" and d["method"] == "GET"
    assert d["prefix"] == "/plugins/srv/ui" and d["plugin"] == "srv"
    r = client.post("/plugins/srv/ui/api/thing", content=b"payload")
    assert r.json()["method"] == "POST" and r.json()["body"] == "payload"


def test_offline_server_ui_shows_a_notice_not_a_502(root, client):
    _plugin(root, "offline", "server")
    r = client.get("/plugins/offline/ui/")
    assert r.status_code == 503 and "꺼져 있습니다" in r.text and "/plugins/offline" in r.text


def test_websocket_is_proxied(root, client):
    _plugin(root, "wsp", "server")
    sock = registry.ui_socket("wsp")
    ready = threading.Event()
    stop = threading.Event()

    def run_server():
        from websockets.asyncio.server import unix_serve

        async def echo(ws):
            async for m in ws:
                await ws.send(f"echo:{m}:{ws.request.path}")

        async def main():
            async with unix_serve(echo, str(sock)):
                ready.set()
                while not stop.is_set():
                    await asyncio.sleep(0.05)

        asyncio.run(main())

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    assert ready.wait(5)
    try:
        with client.websocket_connect("/plugins/wsp/ui/live?room=a") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "echo:ping:/live?room=a"
    finally:
        stop.set()
        t.join(2)
        sock.unlink(missing_ok=True)


def test_ui_socket_env_is_given_only_to_server_plugins(root, monkeypatch):
    """`ui: server` 만 소켓을 받는다 — 정적·경로 UI 는 서버가 없다."""
    import asyncio as _a

    fakes = {}

    class FakePM:
        from app.services.process_manager import ProcessState as _S
        state = _S.IDLE
        pid = None
        env = None

        def set_state_callback(self, cb): pass
        def set_log_callback(self, cb): pass

        @property
        def is_running(self): return True

        async def start(self, cmd, env_extra=None):
            self.env = env_extra; self.state = self._S.RUNNING; self.pid = 1

        async def stop(self): self.state = self._S.IDLE
        def recent_log(self, n=20): return []

    monkeypatch.setattr("app.services.systemd_process.make_process", lambda unit: fakes.setdefault(unit, FakePM()))
    monkeypatch.setattr("app.services.estop_bridge.estop_bridge.sync_activities", lambda: None)
    _plugin(root, "srv2", "server")
    _plugin(root, "dash2", "ui/")
    _a.run(registry.start("srv2", {}, {}))
    _a.run(registry.start("dash2", {}, {}))
    assert fakes["piper-plugin-srv2"].env["PIPER_PLUGIN_UI_SOCKET"].endswith("/.runtime/srv2/ui.sock")
    assert "PIPER_PLUGIN_UI_SOCKET" not in fakes["piper-plugin-dash2"].env
    registry._owners.clear(); registry._runs.clear(); registry._grants.clear()


def test_bundled_ui_demo_and_dashboard_template_are_valid(tmp_path):
    import subprocess
    import sys

    from app.services.plugins import read_manifest

    info = read_manifest(Path(__file__).resolve().parents[2] / "plugins" / "ui-demo")
    assert info.manifest and info.manifest.ui == "server"
    r = subprocess.run([sys.executable, "-m", "piper_plugin", "new", str(tmp_path / "my-dash"), "--template", "dashboard"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "my-dash" / "ui" / "index.html").read_text()
    assert "/api/estop/trigger" in html, "정적 UI 틀에 E-stop 버튼이 없다 (§8-2)"
    assert "__NAME__" not in html
