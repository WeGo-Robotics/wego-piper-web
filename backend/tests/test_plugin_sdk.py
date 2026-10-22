"""플러그인 SDK (`piper_plugin`) — 환경·런타임·미션 뼈대·CLI·템플릿 (feature/plugins.md §7).

버스·게이트웨이·카메라 없이 돈다 — 그 셋은 가짜로 바꾼다. SDK 의 약속은 "훅만 채우면
뼈대가 나머지를 한다"이므로, 여기서 보는 것은 **뼈대가 정말 그 일을 하는가**다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from piper_plugin import Decision, Env, MissionPlugin, Plugin, PluginStop
from piper_plugin.api import Response
from piper_plugin.detections import Detections, detections_text, payload

REPO = Path(__file__).resolve().parents[2]


class FakeBus:
    def __init__(self) -> None:
        self.state: dict = {}
        self.cmds: list = []
        self.detections: dict = {}
        self.previews: dict = {}
        self.last = None

    def put_plugin_state(self, name, st):
        self.state[name] = st

    def pop_plugin_command(self, name):
        return self.cmds.pop(0) if self.cmds else None

    def get_detections(self, name):
        return self.detections.get(name)

    def put_detections(self, name, p):
        self.detections[name] = p

    def put_preview(self, name, jpeg):
        self.previews[name] = jpeg

    def last_estop(self):
        return self.last


class FakeApi:
    """경로별 응답을 미리 적어 두고, 받은 호출을 기록한다."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, object]] = []
        self.base = "http://fake"

    available = True

    def request(self, method, path, json_body=None, timeout_s=None):
        self.calls.append((method, path, json_body))
        r = self.routes.get((method, path), (200, {}))
        status, body = r if isinstance(r, tuple) else (200, r)
        return Response(status, json.dumps(body).encode())

    def get(self, path, **kw):
        return self.request("GET", path)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json)

    def put(self, path, json=None, **kw):
        return self.request("PUT", path, json)

    def delete(self, path, **kw):
        return self.request("DELETE", path)


def _env(tmp_path, **cfg) -> Env:
    return Env(name="t", dir=tmp_path, runtime_dir=tmp_path / "rt", config=cfg, cameras={"watch": "top"},
               api_base="http://fake", token="tok")


def _wire(plugin: Plugin, api: FakeApi | None = None) -> FakeBus:
    bus = FakeBus()
    plugin._bus = bus
    if api is not None:
        plugin.api = api
        plugin.robot.api = api
    return bus


# ── Env ──────────────────────────────────────────────────────────────────────


def test_env_roundtrips_through_environ(tmp_path):
    e = _env(tmp_path, a=1)
    e.heartbeat = True
    back = Env.from_environ(e.to_environ())
    assert back.name == "t" and back.config == {"a": 1} and back.cameras == {"watch": "top"}
    assert back.api_base == "http://fake" and back.token == "tok" and back.heartbeat is True


def test_env_tolerates_garbage_json():
    e = Env.from_environ({"PIPER_PLUGIN_NAME": "x", "PIPER_PLUGIN_CONFIG": "{not json", "PIPER_PLUGIN_CAMERAS": "[]"})
    assert e.config == {} and e.cameras == {}


# ── 검출 계약 ────────────────────────────────────────────────────────────────


def test_payload_carries_publisher_and_model_but_text_is_unchanged():
    p = payload("top", 7, 2_000_000_000, (480, 640), {0: "cup"}, [[10, 10, 50, 50]], [0.9], [0], publisher="det", model="m")
    assert p["publisher"] == "det" and p["model"] == "m"
    assert p["text"] == detections_text(p) and p["text"].startswith("[top 640x480] cup(0.90)")
    d = Detections(p)
    assert d.count("cup") == 1 and d.labels() == ["cup"] and bool(d) and d.ts == 2.0
    assert not Detections({"cam": "top", "size": [1, 1], "objects": []})


# ── Plugin 런타임 ─────────────────────────────────────────────────────────────


def test_run_calls_setup_loop_teardown_and_publishes_state(tmp_path):
    seen = []

    class P(Plugin):
        def setup(self):
            seen.append("setup")

        def loop(self):
            seen.append("loop")
            self.state(foo=1)
            if seen.count("loop") == 3:
                self.stop("enough")

        def teardown(self):
            seen.append("teardown")

    p = P(_env(tmp_path))
    bus = _wire(p)
    assert p.run() == 0
    assert seen == ["setup", "loop", "loop", "loop", "teardown"]
    assert bus.state["t"]["phase"] == "stopped" and bus.state["t"]["stop_reason"] == "enough" and bus.state["t"]["foo"] == 1


def test_loop_exceptions_are_logged_and_retried(tmp_path):
    n = {"i": 0}

    class P(Plugin):
        retry_s = 0.0

        def loop(self):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("boom")
            self.stop()

    p = P(_env(tmp_path))
    bus = _wire(p)
    assert p.run() == 0 and n["i"] == 2
    assert bus.state["t"]["error"] == "boom"


def test_commands_reach_on_command(tmp_path):
    got = []

    class P(Plugin):
        def on_command(self, name, args):
            got.append((name, args))

        def loop(self):
            self.stop()

    p = P(_env(tmp_path))
    bus = _wire(p)
    bus.cmds = [{"name": "skip", "args": {"n": 1}}, {"name": "pause"}]
    p.run()
    assert got == [("skip", {"n": 1}), ("pause", {})]


def test_camera_resolves_slot_or_stream_name(tmp_path):
    p = Plugin(_env(tmp_path))
    assert p.camera("watch").stream == "top" and p.camera("other").stream == "other"
    assert p.camera_mapping() == {"watch": "top"}


def test_detections_freshness(tmp_path, monkeypatch):
    p = Plugin(_env(tmp_path))
    bus = _wire(p)
    import time as _t
    bus.detections["top"] = payload("top", 1, int((_t.time() - 10) * 1e9), (2, 2), {}, [], [], [])
    assert p.detections("top") is not None
    assert p.detections("top", fresh_s=1.0) is None


def test_robot_calls_the_documented_routes(tmp_path):
    p = Plugin(_env(tmp_path))
    api = FakeApi({("GET", "/api/models/inference/policy"): {"running": True, "type": "pi05", "takes_language": True}})
    _wire(p, api)
    p.robot.start_inference("/ck", {"top": "cam1"}, policy_type="pi05", task="go")
    p.robot.set_task("pick")
    p.robot.pause(); p.robot.resume(); p.robot.reset(); p.robot.stop_inference()
    p.robot.eval_log(True, memo="m")
    assert p.robot.policy()["takes_language"] is True
    paths = [(m, pth) for m, pth, _ in api.calls]
    assert ("POST", "/api/models/inference/start") in paths and ("POST", "/api/params") in paths
    assert ("POST", "/api/params/pause") in paths and ("POST", "/api/eval/log") in paths
    start_body = next(b for m, pth, b in api.calls if pth == "/api/models/inference/start")
    assert start_body["params"]["task"] == "go" and start_body["camera_mapping"] == {"top": "cam1"}
    task_body = next(b for m, pth, b in api.calls if pth == "/api/params")
    assert task_body == {"params": {"task": "pick"}}


# ── MissionPlugin ───────────────────────────────────────────────────────────


class Sorter(MissionPlugin):
    poll_s = 0.0

    def __init__(self, env, decisions):
        super().__init__(env)
        self._decisions = list(decisions)
        self.resets = 0

    def observe(self):
        return {"n": self.episode}

    def decide(self, ctx):
        return self._decisions.pop(0)

    def done(self, ctx):
        return ctx["n"] == 1              # 1회차만 완료, 나머지는 타임아웃

    def record(self, ctx, result):
        return {"n": ctx["n"]}

    def reset(self):
        self.resets += 1


def test_mission_runs_episodes_and_journals(tmp_path):
    env = _env(tmp_path, max_episodes=3, episode_timeout_s=0.01, settle_s=0)
    m = Sorter(env, [Decision.task("a"), Decision.skip("없음"), Decision.task("b")])
    api = FakeApi({("GET", "/api/models/inference/policy"): {"running": True, "type": "pi05", "takes_language": True},
                   ("GET", "/api/activity"): {"running": ["inference"]}})
    _wire(m, api)
    assert m.run() == 0
    lines = [json.loads(l) for l in m.journal_path.read_text().splitlines()]
    assert [l["episode"] for l in lines] == [1, 2, 3]
    assert lines[0]["result"]["done"] is True and lines[0]["decision"]["text"] == "a"
    assert lines[1]["result"] == {"skipped": True}
    assert lines[2]["result"]["timeout"] is True
    tasks = [b for mth, pth, b in api.calls if pth == "/api/params"]
    assert [t["params"]["task"] for t in tasks] == ["a", "b"]
    assert m.resets == 2 and m._stop_reason == "3회차 완료"


def test_mission_refuses_when_policy_ignores_task(tmp_path):
    m = Sorter(_env(tmp_path), [])
    api = FakeApi({("GET", "/api/models/inference/policy"): {"running": True, "type": "act", "takes_language": False}})
    _wire(m, api)
    m.run()
    assert "task 를 읽지 않는다" in m._stop_reason


def test_mission_refuses_when_inference_is_not_running(tmp_path):
    m = Sorter(_env(tmp_path), [])
    _wire(m, FakeApi({("GET", "/api/models/inference/policy"): {"running": False}}))
    m.run()
    assert "추론이 돌고 있지 않다" in m._stop_reason


def test_mission_dry_run_needs_no_robot(tmp_path):
    env = _env(tmp_path, dry_run=True, max_episodes=1, episode_timeout_s=0.01, settle_s=0)
    m = Sorter(env, [Decision.task("x")])
    api = FakeApi()
    _wire(m, api)
    m.run()
    assert not [c for c in api.calls if c[1] == "/api/params"]       # 로봇에 아무것도 안 보냈다
    assert m.journal_path.exists()


def test_mission_stops_on_estop(tmp_path):
    env = _env(tmp_path, max_episodes=5, episode_timeout_s=1, settle_s=0)
    m = Sorter(env, [Decision.task("a")] * 5)
    api = FakeApi({("GET", "/api/models/inference/policy"): {"running": True, "type": "pi05", "takes_language": True},
                   ("GET", "/api/activity"): {"running": ["inference"]}})
    bus = _wire(m, api)
    bus.last = {"ts": m.started_at + 1, "reason": "manual"}
    m.run()
    assert m._stop_reason.startswith("E-stop")


def test_decision_skill_switches_checkpoint(tmp_path):
    env = _env(tmp_path, max_episodes=2, episode_timeout_s=0.01, settle_s=0, skills={"pick_red": "/ck/red"})
    m = Sorter(env, [Decision.skill("pick_red"), Decision.skill("pick_red")])
    api = FakeApi({("GET", "/api/models/inference/policy"): {"running": True, "type": "act", "takes_language": False},
                   ("GET", "/api/activity"): {"running": ["inference"]}})
    _wire(m, api)
    m.run()
    starts = [c for c in api.calls if c[1] == "/api/models/inference/start"]
    assert len(starts) == 1 and starts[0][2]["checkpoint_path"] == "/ck/red"   # 같은 스킬은 다시 안 갈아탄다


# ── CLI · 템플릿 · 예제 ──────────────────────────────────────────────────────


def _cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "piper_plugin", *args], capture_output=True, text=True, cwd=cwd)


@pytest.mark.parametrize("template", ["loop", "mission", "detector"])
def test_new_then_check_passes_for_every_template(tmp_path, template):
    dst = tmp_path / f"my-{template}"
    r = _cli("new", str(dst), "--template", template)
    assert r.returncode == 0, r.stderr
    assert "__NAME__" not in (dst / "plugin.yaml").read_text()
    r = _cli("check", str(dst))
    assert r.returncode == 0, r.stderr
    assert f"my-{template}" in r.stdout


def test_check_reports_a_broken_manifest(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: bad\nconfig:\n  - { key: a\n")
    r = _cli("check", str(d))
    assert r.returncode == 1 and "번째 줄" in r.stderr


def test_run_executes_the_entry_with_the_env(tmp_path):
    d = tmp_path / "echo"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: echo\nconfig:\n  - { key: k, default: 7 }\n")
    (d / "main.py").write_text("import os, json\nprint('CFG', os.environ['PIPER_PLUGIN_CONFIG'], os.environ['PIPER_PLUGIN_CAMERAS'])\n")
    r = _cli("run", str(d), "--camera", "watch=top", "--config", '{"x": 1}')
    assert r.returncode == 0, r.stderr
    assert '"k": 7' in r.stdout and '"x": 1' in r.stdout and '"watch": "top"' in r.stdout


@pytest.mark.parametrize("name", ["hello", "detect-and-start", "zone-guard"])
def test_bundled_examples_are_valid(name):
    from app.services.plugins import read_manifest

    info = read_manifest(REPO / "plugins" / name)
    assert info.manifest is not None, info.error


def test_bundled_examples_do_not_import_agpl_detectors():
    """⚠ **낱말이 아니라 import 를 본다** — 번들은 왜 그걸 안 쓰는지 docstring 에 적는다."""
    import re

    for p in (REPO / "plugins").rglob("*.py"):
        src = p.read_text()
        assert not re.search(r"^\s*(import ultralytics|from ultralytics)", src, re.M), \
            f"{p} 가 AGPL 검출기를 import 한다"
