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


def test_run_stops_when_the_process_that_started_it_is_gone(tmp_path, monkeypatch):
    """자식 프로세스로 띄운 플러그인은 게이트웨이가 죽어도 남는다 — 부모가 바뀌면 스스로 선다.

    2026-10-29: 시험 게이트웨이가 남긴 CPU 검출기가 6일 동안 16코어를 먹어 녹화 카메라가
    프레임을 놓쳤다(0.2초 공백 → 녹화가 죽음). teardown 은 정상 정지처럼 돈다.
    """
    import piper_plugin.plugin as mod

    ppid = {"v": 4242}
    monkeypatch.setattr(mod.os, "getppid", lambda: ppid["v"])
    seen = []

    class P(Plugin):
        def loop(self):
            seen.append("loop")
            if seen.count("loop") == 2:
                ppid["v"] = 1          # 부모가 죽어 init 에 입양됐다

        def teardown(self):
            seen.append("teardown")

    p = P(_env(tmp_path))
    bus = _wire(p)
    assert p.run() == 0
    assert seen == ["loop", "loop", "teardown"]
    assert bus.state["t"]["phase"] == "stopped" and bus.state["t"]["stop_reason"] == "부모 프로세스 종료"


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


def test_check_json_is_machine_readable(tmp_path):
    """에이전트·훅이 읽는 모양 — ✓/✗ 글자를 파싱하지 않는다 (feature/agent-cli.md §4-2)."""
    dst = tmp_path / "my-loop"
    assert _cli("new", str(dst), "--template", "loop").returncode == 0
    r = _cli("check", "--json", str(dst))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["name"] == "my-loop" and out["entry"] == "main.py"
    assert out["errors"] == [] and out["scopes_checked"] is False
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("name: bad\nconfig:\n  - { key: a\n")
    r = _cli("check", "--json", str(bad))
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["errors"][0]["where"] == "manifest" and "번째 줄" in out["errors"][0]["msg"]
    noentry = tmp_path / "noentry"
    noentry.mkdir()
    (noentry / "plugin.yaml").write_text("name: noentry\n")
    out = json.loads(_cli("check", "--json", str(noentry)).stdout)
    assert out["ok"] is False and out["errors"][0]["where"] == "manifest" and "main.py" in out["errors"][0]["msg"]


def test_plugin_check_hook_runs_only_inside_a_plugin_folder(tmp_path):
    """Claude Code PostToolUse 훅 — 플러그인 폴더 밖은 조용히 0, 안에서 깨진 매니페스트는 2 + 사유 (§4-2)."""
    import os

    hook = REPO / "tools" / "hooks" / "plugin-check.sh"
    # 훅은 PATH 의 python3 를 쓴다 — 테스트 인터프리터(piper_plugin 이 깔린 것)를 앞에 둔다
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def run(path):
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})
        return subprocess.run(["bash", str(hook)], input=payload, capture_output=True, text=True, env=env)

    plain = tmp_path / "notes.md"
    plain.write_text("x")
    assert run(plain).returncode == 0
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("name: bad\nconfig:\n  - { key: a\n")
    (bad / "main.py").write_text("print('hi')\n")
    r = run(bad / "main.py")
    assert r.returncode == 2 and "번째 줄" in r.stderr, r.stderr
    good = tmp_path / "good"
    assert _cli("new", str(good), "--template", "loop").returncode == 0
    assert run(good / "main.py").returncode == 0


def test_run_refuses_the_gateway_when_launched_by_the_web_chat(tmp_path):
    """웹 채팅 개발 모드(PIPER_AGENT_CHAT=1)가 띄운 run 은 --api/--token 을 거절한다 — 드라이런만 (feature/agent-cli.md §4-6)."""
    import os

    d = tmp_path / "echo"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: echo\n")
    (d / "main.py").write_text("print('ran')\n")
    env = {**os.environ, "PIPER_AGENT_CHAT": "1"}
    r = subprocess.run([sys.executable, "-m", "piper_plugin", "run", str(d), "--api", "http://127.0.0.1:8000"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "드라이런만" in r.stderr and "ran" not in r.stdout
    r = subprocess.run([sys.executable, "-m", "piper_plugin", "run", str(d), "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "ran" in r.stdout


def test_run_executes_the_entry_with_the_env(tmp_path):
    d = tmp_path / "echo"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: echo\nconfig:\n  - { key: k, default: 7 }\n")
    (d / "main.py").write_text("import os, json\nprint('CFG', os.environ['PIPER_PLUGIN_CONFIG'], os.environ['PIPER_PLUGIN_CAMERAS'])\n")
    r = _cli("run", str(d), "--camera", "watch=top", "--config", '{"x": 1}')
    assert r.returncode == 0, r.stderr
    assert '"k": 7' in r.stdout and '"x": 1' in r.stdout and '"watch": "top"' in r.stdout


@pytest.mark.parametrize("name", ["hello", "detect-and-start", "zone-guard", "barcode"])
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


# ── 플러그인 간 입출력 (§7-3) ───────────────────────────────────────────────────


class TopicBus(FakeBus):
    def __init__(self):
        super().__init__()
        self.topics = {}

    def put_topic(self, topic, data, publisher="", ttl_ms=None):
        seq = (self.topics[topic]["seq"] + 1) if topic in self.topics else 1
        self.topics[topic] = {"topic": topic, "publisher": publisher, "seq": seq, "ts": 1000.0, "data": data}
        return self.topics[topic]

    def get_topic(self, topic):
        return self.topics.get(topic)


def test_publish_and_latest_go_through_the_input_mapping(tmp_path):
    from piper_plugin import Topic

    env = _env(tmp_path)
    env.inputs = {"grades_in": "grades"}
    p = Plugin(env)
    bus = TopicBus()
    p._bus = bus
    p.publish("grades", {"counts": {"large": 1}})
    p.publish("grades", {"counts": {"large": 2}})
    tp = p.latest("grades_in")                       # 슬롯 키로 읽으면 매핑을 거친다
    assert isinstance(tp, Topic) and tp.seq == 2 and tp.publisher == "t" and tp.data["counts"]["large"] == 2
    assert p.latest("nothing") is None
    assert p.input("grades_in") == "grades" and p.input("unmapped") == "unmapped"


def test_topic_bus_roundtrip():
    from piper_bus.client import Bus

    try:
        bus = Bus()
        bus.r.ping()
    except Exception:
        pytest.skip("redis 없음")
    try:
        a = bus.put_topic("t_test", {"x": 1}, publisher="p1")
        b = bus.put_topic("t_test", {"x": 2}, publisher="p1")
        assert (a["seq"], b["seq"]) == (1, 2)
        got = bus.get_topic("t_test")
        assert got["data"] == {"x": 2} and got["publisher"] == "p1" and "t_test" in bus.topic_names()
    finally:
        from piper_bus import contract as C
        bus.r.delete(C.plugin_topic_key("t_test"))


def test_env_knows_its_instance():
    e = Env.from_environ({"PIPER_PLUGIN_NAME": "detector:cam2"})
    assert (e.base_name, e.instance) == ("detector", "cam2")
    e = Env.from_environ({"PIPER_PLUGIN_NAME": "detector"})
    assert (e.base_name, e.instance) == ("detector", "")
    e2 = Env.from_environ(Env(name="d:x", base_name="d", instance="x", dir=Path("."), runtime_dir=Path(".")).to_environ())
    assert e2.base_name == "d" and e2.instance == "x" and e2.name == "d:x"


# ── 팔 상태 (읽기 전용, feature/plugins.md §7-5) ─────────────────────────────

ARM_IFACE = "pytest-plugin-arm"


@pytest.fixture
def arm_writer():
    """실제 `/dev/shm` 세그먼트 — 포맷은 `piper_shm.arm` 이 정본이라 가짜로 바꾸지 않는다."""
    import contextlib

    from piper_shm import arm as A

    made = []

    def make():
        w = A.StateWriter(ARM_IFACE)
        made.append(w)
        return w

    yield make
    for w in made:
        with contextlib.suppress(Exception):
            w.close()
    A.unlink(A.segment_name(ARM_IFACE, A.KIND_STATE))


def test_env_carries_arm_slots(tmp_path):
    e = _env(tmp_path)
    e.arms = {"follower": "can0"}
    assert Env.from_environ(e.to_environ()).arms == {"follower": "can0"}


def test_arm_reads_normalized_state_and_follows_a_restarted_publisher(tmp_path, arm_writer):
    from piper_shm import JOINTS

    env = _env(tmp_path)
    env.arms = {"follower": ARM_IFACE}
    p = Plugin(env)
    arm = p.arm("follower")
    assert arm.iface == ARM_IFACE and p.arm(ARM_IFACE) is arm      # 슬롯 키든 인터페이스든 같은 리더
    assert arm.read() is None and arm.connected is False            # 세그먼트가 없으면 예외가 아니라 None

    w = arm_writer()
    pose = {j: float(i * 10) for i, j in enumerate(JOINTS)}
    w.publish(pose)
    st = arm.read_new(timeout_s=0.5)
    assert st is not None and st.joints == pose and st.gripper == pose["gripper"] and st.fresh(1.0)
    assert arm.read_new(timeout_s=0.05) is None                     # 새 것이 없다

    # 발행자 재시작 — 옛 mmap 의 마지막 자세가 아니라 새 세그먼트를 읽어야 한다
    w.close()
    w2 = arm_writer()
    w2.publish({**pose, "gripper": 5.0})
    st2 = arm.read()
    assert st2 is not None and st2.gripper == 5.0


def test_arm_end_pose_is_in_mm_and_degrees_through_robot_read(tmp_path):
    p = Plugin(_env(tmp_path))
    api = FakeApi({
        ("GET", "/api/robots/end-pose/can0"): {
            "pose": {"x": 250000, "y": -10000, "z": 600000, "rx": 180000, "ry": 0, "rz": 90000},
            "box": {"x": [100.0, 500.0], "y": [-300.0, 300.0], "z": [50.0, 500.0]}},
        ("GET", "/api/robots/end-pose/can1"): (403, {"detail": "범위 밖"}),
    })
    _wire(p, api)
    pose = p.arm("can0").end_pose()
    assert (pose["x"], pose["y"], pose["z"], pose["rz"]) == (250.0, -10.0, 600.0, 90.0)
    assert pose["inside"] is False                                   # z 600mm 는 상자(50~500) 밖
    assert p.arm("can1").end_pose() is None                          # 범위가 없으면 모른다


def test_run_hands_arm_slots_to_the_entry(tmp_path):
    d = tmp_path / "armecho"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: armecho\narms:\n  - { key: follower }\n")
    (d / "main.py").write_text("import os\nprint('ARMS', os.environ['PIPER_PLUGIN_ARMS'])\n")
    r = _cli("run", str(d), "--arm", "follower=can0")
    assert r.returncode == 0, r.stderr
    assert '"follower": "can0"' in r.stdout
    c = _cli("check", str(d))
    assert c.returncode == 0 and "arms: ['follower']" in c.stdout
