"""piper-mcp — ext v1 위의 MCP 어댑터 (feature/agent-cli.md §4-3).

핵심은 heartbeat 소유다: 자기가 시작한 미션에만, 끝나면 멈추고, 서버가 죽으면 끊긴다.
앞의 테스트들은 SDK 없이 돈다(`PiperMcp`·`HeartbeatPump`). 뒤의 것은 `mcp` SDK 가 있을 때만 —
stdio 로 실제 서버 프로세스를 띄워 가짜 게이트웨이에 대고 돌린 뒤 **죽여서** 박동이 멈추는지 본다.
"""

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from piper_plugin import mcp as M
from piper_plugin.api import Response

EXT = "/api/ext/v1"


class FakeApi:
    """`Api` 의 request 모양만 흉내 낸다 — 스크립트된 응답 + 호출 기록."""

    def __init__(self, routes: dict | None = None):
        self.base, self.token = "http://fake", "t"
        self.calls: list[tuple] = []
        self.routes = routes or {}
        self.live = True

    def request(self, method, path, json_body=None, timeout_s=None):
        self.calls.append((method, path, json_body))
        v = self.routes.get(f"{method} {path}")
        if v is not None:
            return v() if callable(v) else v
        if path == f"{EXT}/heartbeat":
            return Response(200, b'{"status":"ok"}')
        if method == "GET" and path.startswith(f"{EXT}/missions/"):
            return Response(200, json.dumps({"id": path.rsplit("/", 1)[1], "live": self.live}).encode())
        if method == "POST" and path == f"{EXT}/missions":
            return Response(200, b'{"id":"run_1","type":"recycling"}')
        if method == "GET" and path.startswith(f"{EXT}/diag/context"):
            return Response(200, "# PIPER 에이전트용 컨텍스트\n\n생성 …\n".encode())
        if method == "GET" and path == f"{EXT}/datasets":
            return Response(200, b'{"datasets":[{"id":"user/apple","total_episodes":12}]}')
        if method == "GET" and path.startswith(f"{EXT}/datasets/"):
            ds, _, ep = path[len(f"{EXT}/datasets/"):].partition("/episodes/")
            return Response(200, json.dumps({"dataset": ds, "episode": int(ep), "length": 210}).encode())
        if method == "GET" and path.startswith(f"{EXT}/eval/stats"):
            return Response(200, b'{"total":0,"successes":0,"rate":0}')
        if method == "GET" and path == f"{EXT}/training":
            return Response(200, b'{"status":{"state":"idle"},"jobs":[]}')
        return Response(200, b"{}")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json_body=json, **kw)


def _wait(cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_mission_start_defaults_to_dry_run_and_takes_the_heartbeat():
    api = FakeApi()
    core = M.PiperMcp(api, heartbeat_s=0.02, poll_s=0.05)
    out = core.mission_start("recycling", {"max_episodes": 3})
    body = [c for c in api.calls if c[:2] == ("POST", f"{EXT}/missions")][0][2]
    assert body["dry_run"] is True and body["type"] == "recycling" and body["max_episodes"] == 3
    assert out["id"] == "run_1" and "heartbeat" in out and out["dry_run"] is True
    assert _wait(lambda: core.pump.beats >= 3)
    assert core.status()["mcp"]["heartbeat_for"] == "run_1"
    core.close()
    assert _wait(lambda: not core.pump.alive) and core.pump.ended_reason == "closed"


def test_pump_stops_when_the_mission_ends_or_is_cancelled():
    api = FakeApi()
    core = M.PiperMcp(api, heartbeat_s=0.02, poll_s=0.05)
    core.mission_start("recycling", dry_run=False)
    assert [c for c in api.calls if c[1] == f"{EXT}/missions"][0][2]["dry_run"] is False
    assert _wait(lambda: core.pump.beats >= 2)
    api.live = False                                   # 게이트웨이가 "끝났다" 고 답한다
    assert _wait(lambda: not core.pump.alive) and core.pump.ended_reason == "finished"
    beats = core.pump.beats
    time.sleep(0.1)
    assert core.pump.beats == beats                    # 더 안 뛴다
    api.live = True
    core.mission_start("recycling")
    assert _wait(lambda: core.pump.beats >= 1)
    core.mission_cancel("run_1")
    assert _wait(lambda: not core.pump.alive) and core.pump.ended_reason == "cancelled"
    assert core.status()["mcp"]["heartbeat_for"] is None and core.status()["mcp"]["started_here"] == ["run_1", "run_1"]


def test_no_pump_for_missions_this_server_did_not_start():
    api = FakeApi()
    core = M.PiperMcp(api, heartbeat_s=0.02)
    core.status()
    core.missions()
    core.mission_get("run_foreign")
    time.sleep(0.1)
    assert core.pump is None and not any(c[1].endswith("/heartbeat") for c in api.calls)


def test_gateway_errors_carry_status_detail_and_a_next_step():
    api = FakeApi({f"POST {EXT}/missions": Response(409, json.dumps({"detail": "미션이 이미 돌고 있습니다: recycling"}).encode())})
    core = M.PiperMcp(api)
    with pytest.raises(M.GatewayError) as ei:
        core.mission_start("recycling")
    assert ei.value.status == 409 and "이미 돌고" in str(ei.value) and "사람" in str(ei.value)
    assert core.pump is None                            # 실패한 시작에는 박동이 없다
    api2 = FakeApi({f"GET {EXT}/status": Response(503, json.dumps({"detail": "외부 API 비활성"}).encode())})
    with pytest.raises(M.GatewayError) as ei:
        M.PiperMcp(api2).status()
    assert "PIPER_API_TOKEN" in str(ei.value)


def test_read_only_methods_hit_the_ext_read_routes():
    api = FakeApi()
    core = M.PiperMcp(api)
    assert core.context(since="1 hour ago", unit="robotd", lines=50).startswith("# PIPER")
    assert api.calls[-1][1] == f"{EXT}/diag/context?unit=robotd&lines=50&format=markdown&since=1+hour+ago"
    assert core.datasets()["datasets"][0]["id"] == "user/apple"
    assert core.episode("user/apple", 3) == {"dataset": "user/apple", "episode": 3, "length": 210}
    assert core.eval_stats(5)["total"] == 0 and api.calls[-1][1] == f"{EXT}/eval/stats?last_n=5"
    assert core.training()["status"]["state"] == "idle"
    assert not any(c[1].endswith("/heartbeat") for c in api.calls)     # 읽기는 박동을 켜지 않는다


def _zip(entries: dict) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_episode_bundle_tool_extracts_into_a_local_folder(tmp_path):
    """묶음은 에이전트가 도는 기계에 풀린다 — 키프레임을 자기 파일 도구로 바로 읽게 (§4-5)."""
    z = _zip({"README.md": "# 에피소드 묶음 — u/apple / 에피소드 3\n",
              "timeline.json": json.dumps({"length": 40, "fps": 10, "tasks": ["집어"], "phases": {"segments": []},
                                           "keyframes": {"frames": [0, 39], "fallback": False, "missing": {}}}),
              "keyframes/top/frame-000000.jpg": b"\xff\xd8\xff\xd9", "keyframes/top/frame-000039.jpg": b"\xff\xd8\xff\xd9",
              "state_action.csv": "frame_index\n0\n"})
    api = FakeApi({f"GET {EXT}/datasets/user/apple/episodes/3/bundle?at=phases": Response(200, z)})
    out = M.PiperMcp(api).episode_bundle("user/apple", 3, out_dir=str(tmp_path / "b"))
    assert out["dir"] == str(tmp_path / "b")
    assert out["files"] == ["README.md", "keyframes/top/frame-000000.jpg", "keyframes/top/frame-000039.jpg",
                            "state_action.csv", "timeline.json"]
    assert out["keyframes"]["top"][0].endswith("frame-000000.jpg") and (tmp_path / "b" / "keyframes" / "top" / "frame-000039.jpg").exists()
    assert out["length"] == 40 and out["selected_frames"] == [0, 39] and out["readme"].startswith("# 에피소드 묶음")
    # cams·at 는 질의로 간다
    api2 = FakeApi({f"GET {EXT}/datasets/u/episodes/1/bundle?at=every%3A5&cams=top%2Chand": Response(200, z)})
    M.PiperMcp(api2).episode_bundle("u", 1, cams=["top", "hand"], at="every:5", out_dir=str(tmp_path / "c"))
    # 경로 이탈 항목은 묶음 전체를 거절하고 아무것도 쓰지 않는다
    evil = _zip({"README.md": "x", "../evil.txt": b"x"})
    api3 = FakeApi({f"GET {EXT}/datasets/u/episodes/2/bundle?at=phases": Response(200, evil)})
    with pytest.raises(M.GatewayError) as ei:
        M.PiperMcp(api3).episode_bundle("u", 2, out_dir=str(tmp_path / "d"))
    assert "경로 이탈" in str(ei.value) and not (tmp_path / "evil.txt").exists() and not (tmp_path / "d" / "README.md").exists()
    # zip 이 아니면 그렇다고 말한다
    api4 = FakeApi({f"GET {EXT}/datasets/u/episodes/3/bundle?at=phases": Response(200, b'{"detail":"x"}')})
    with pytest.raises(M.GatewayError) as ei:
        M.PiperMcp(api4).episode_bundle("u", 3, out_dir=str(tmp_path / "e"))
    assert "zip 이 아니다" in str(ei.value)


def test_estop_is_a_plain_post_and_needs_nothing_else():
    api = FakeApi()
    M.PiperMcp(api).estop()
    assert api.calls[-1][:2] == ("POST", f"{EXT}/estop")


# ── SDK 가 있을 때 ────────────────────────────────────────────────────────────

pytest.importorskip("mcp")


def test_server_exposes_the_ext_surface_as_tools_and_no_heartbeat_tool():
    server = M.build_server(M.PiperMcp(FakeApi()))
    names = sorted(t.name for t in asyncio.run(server.list_tools()))
    assert names == sorted(M.TOOL_NAMES)
    assert not any("heartbeat" in n for n in names)     # 박동은 도구가 아니다 — 서버가 펌프한다


def test_gateway_errors_become_tool_errors_not_crashes():
    api = FakeApi({f"POST {EXT}/missions": Response(409, json.dumps({"detail": "미션이 이미 돌고 있습니다"}).encode())})
    server = M.build_server(M.PiperMcp(api))
    with pytest.raises(Exception) as ei:
        asyncio.run(server.call_tool("piper_mission_start", {"type": "recycling"}))
    assert "409" in str(ei.value) and "이미 돌고" in str(ei.value)


def _contents(items):
    """SDK 가 돌려주는 리소스 내용 → [(mime, text)] — v2 는 ReadResourceContents(content, mime_type)."""
    out = []
    for c in items:
        mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None)
        text = getattr(c, "content", None)
        if text is None:
            text = getattr(c, "text", None)
        out.append((mime, text if isinstance(text, str) else (text or b"").decode()))
    return out


def test_read_only_routes_are_resources_and_the_context_is_also_a_tool():
    server = M.build_server(M.PiperMcp(FakeApi()))
    uris = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert set(M.RESOURCE_URIS) <= uris
    templates = {getattr(t, "uri_template", None) or getattr(t, "uriTemplate", None)
                 for t in asyncio.run(server.list_resource_templates())}
    assert M.EPISODE_TEMPLATE in templates or M.EPISODE_TEMPLATE.replace("{+", "{") in templates
    mime, text = _contents(asyncio.run(server.read_resource("piper://context")))[0]
    assert mime == "text/markdown" and text.startswith("# PIPER")
    _, text = _contents(asyncio.run(server.read_resource("piper://status")))[0]
    assert "heartbeat_for" in text                       # 상태에는 이 서버의 박동 상태가 실린다
    _, text = _contents(asyncio.run(server.read_resource("piper://datasets/user/apple/episodes/3")))[0]
    assert json.loads(text) == {"dataset": "user/apple", "episode": 3, "length": 210}   # `/` 가 든 id 가 그대로 간다
    res = asyncio.run(server.call_tool("piper_context", {"since": "10 minutes ago"}))
    text = res.content[0].text if hasattr(res, "content") else res[0].text
    assert text.startswith("# PIPER")


TOKEN = "mcp-test-token"


def _gateway():
    """ext v1 의 네 경로만 — Bearer 검사, heartbeat 시각 기록."""
    state = {"beats": [], "missions": {}, "live": True}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"detail": "토큰이 틀렸습니다"})
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            if self.path == f"{EXT}/status":
                self._send(200, {"activities": {"running": []}, "estop": {"armed": False}, "mission": None})
            elif self.path == f"{EXT}/missions/run_x":
                self._send(200, {"id": "run_x", "live": state["live"]})
            else:
                self._send(404, {"detail": "그런 미션이 없습니다"})

        def do_POST(self):
            if not self._auth():
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if self.path == f"{EXT}/heartbeat":
                state["beats"].append(time.monotonic())
                self._send(200, {"status": "ok"})
            elif self.path == f"{EXT}/missions":
                state["missions"]["run_x"] = json.loads(raw or b"{}")
                self._send(200, {"id": "run_x", "type": "recycling"})
            elif self.path == f"{EXT}/estop":
                self._send(200, {"status": "stopped", "stopped": []})
            else:
                self._send(404, {"detail": "없음"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def test_stdio_server_end_to_end_and_heartbeat_dies_with_the_process():
    """실제 서버 프로세스 + MCP 클라이언트. 컨텍스트를 나오면(부모가 죽은 상황) 박동이 멈춰야 한다."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    srv, state = _gateway()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "piper_plugin.mcp", "--api", base, "--heartbeat-s", "0.05", "--poll-s", "0.2"],
        env={**os.environ, "PIPER_API_TOKEN": TOKEN})

    def is_error(res):
        return bool(getattr(res, "is_error", getattr(res, "isError", False)))

    async def drive():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert set(M.TOOL_NAMES) <= names
                st = await s.call_tool("piper_status", {})
                assert not is_error(st) and "activities" in st.content[0].text and "heartbeat_for" in st.content[0].text
                uris = {str(r.uri) for r in (await s.list_resources()).resources}
                assert "piper://status" in uris and "piper://context" in uris
                rr = await s.read_resource("piper://status")
                assert "activities" in rr.contents[0].text
                bad = await s.call_tool("piper_mission_get", {"mission_id": "nope"})
                assert is_error(bad) and "404" in bad.content[0].text
                started = await s.call_tool("piper_mission_start", {"type": "recycling", "config": {"max_episodes": 1}})
                assert not is_error(started), started.content[0].text
                assert state["missions"]["run_x"]["dry_run"] is True
                t0 = time.monotonic()
                while len(state["beats"]) < 4 and time.monotonic() - t0 < 3:
                    await asyncio.sleep(0.05)
                assert len(state["beats"]) >= 4, "박동이 안 온다"
        # 여기서 서버 프로세스가 죽는다 — 에이전트(부모)가 죽은 상황과 같다
        n = len(state["beats"])
        await asyncio.sleep(0.5)
        assert len(state["beats"]) == n, "서버가 죽었는데 heartbeat 가 계속 온다"

    try:
        asyncio.run(drive())
    finally:
        srv.shutdown()
