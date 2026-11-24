"""장치 확장 API·화면 프록시 (feature/pika.md §2).

화면 프록시는 플러그인 `ui: server` 와 같은 코드다(test_plugin_ui.py 가 그쪽을 본다). 여기는 확장 쪽만:
경로가 `<ext_dir>/<name>/run/ui.sock` 이다, 이름으로 경로 밖을 못 가리킨다, 꺼져 있으면 502 가 아니라 안내다,
플러그인 토큰은 `*` 로도 확장을 못 더한다, 목록이 로그인 상태를 싣는다(꺼져 있으면 화면이 경고한다).
"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        self.socket.bind(self.server_address)


class _Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self):
        body = json.dumps({"path": self.path, "method": self.command,
                           "prefix": self.headers.get("X-Forwarded-Prefix"),
                           "ext": self.headers.get("X-Piper-Extension")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


@pytest.fixture
def ext_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ext_dir", tmp_path / "ext")
    return tmp_path / "ext"


@pytest.fixture
def client():
    return TestClient(app)


def test_the_extension_screen_is_proxied_from_its_data_dir_socket(ext_dir, client):
    sock = ext_dir / "pika" / "run" / "ui.sock"
    sock.parent.mkdir(parents=True)
    srv = _UnixHTTPServer(str(sock), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = client.get("/ext/pika/ui/api/state?x=1").json()
        assert d["path"] == "/api/state?x=1" and d["prefix"] == "/ext/pika/ui" and d["ext"] == "pika"
        assert client.post("/ext/pika/ui/api/assign", content=b"{}").json()["method"] == "POST"
        r = client.get("/ext/pika/ui", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].endswith("/ext/pika/ui/")
    finally:
        srv.shutdown()


def test_an_extension_that_is_off_shows_a_notice_not_a_502(ext_dir, client):
    r = client.get("/ext/pika/ui/")
    assert r.status_code == 503 and "장치 확장이 꺼져" in r.text


@pytest.mark.parametrize("path", ["/ext/..%2f..%2fetc/ui/", "/ext/Pika/ui/", "/ext/a/ui/"])
def test_a_name_cannot_point_outside_the_ext_dir(ext_dir, client, path):
    assert client.get(path).status_code == 404


def test_a_plugin_token_cannot_add_an_extension_even_with_all_scopes():
    from app.core import plugin_scopes as S

    for method in ("GET", "POST", "DELETE"):
        assert not S.allows({S.ALL}, method, "/api/system/extensions")
        assert not S.allows({S.ALL}, method, "/api/system/extensions/pika")


class _FakeBus:
    def __init__(self):
        self.calls = []

    def rpc_call(self, daemon, method, args=None, timeout=20):
        self.calls.append((daemon, method, args))
        if method == "ext_list":
            return {"extensions": [{"name": "pika", "title": "PIKA (Sense·Gripper)", "image": "pika-docker:dev",
                                    "manifest": {"rpc_hub": "pika", "ui": "server"}, "unit": "piper-ext-pika"}],
                    "registries": ["ghcr.io/wego-robotics/"]}
        if method == "ext_add":
            return {"state": "running", "image": args[0]}
        raise AssertionError(method)

    def daemon_info(self, hub):
        return {"started": 1.0} if hub == "pika" else None


@pytest.fixture
def unitd(monkeypatch):
    from app.services import units

    bus = _FakeBus()
    monkeypatch.setattr(units, "unitd_available", lambda: True)
    monkeypatch.setattr(units, "_bus", lambda: bus)
    return bus


def test_the_list_says_whether_each_extension_is_alive_and_whether_login_is_on(ext_dir, client, unitd):
    d = client.get("/api/system/extensions").json()
    e = d["extensions"][0]
    assert e["name"] == "pika" and e["alive"] is True and e["ui_ready"] is False
    assert "login_enabled" in d and d["registries"] == ["ghcr.io/wego-robotics/"]


def test_adding_hands_the_image_to_unitd_and_returns_at_once(client, unitd):
    r = client.post("/api/system/extensions", json={"image": " ghcr.io/wego-robotics/pika-docker:0.1.0 "})
    assert r.json()["state"] == "running"
    assert unitd.calls[-1] == ("unitd", "ext_add", ["ghcr.io/wego-robotics/pika-docker:0.1.0"])


def test_extension_logs_are_offered_on_the_log_screen(client, unitd):
    ids = [u["id"] for u in client.get("/api/system/log-units").json()["units"]]
    assert "ext-pika" in ids


def test_without_unitd_the_api_says_why(client, monkeypatch):
    from app.services import units

    monkeypatch.setattr(units, "unitd_available", lambda: False)
    r = client.get("/api/system/extensions")
    assert r.status_code == 503 and "unitd" in r.json()["detail"]
