"""번들 `recycling` 플러그인 — 오케스트레이터 1단계가 지키던 것을 플러그인판도 지킨다 (feature/plugins.md §12).

예전 `test_orchestrator.py` 의 사례를 그대로 옮겼다: 전체 루프가 task·reset 을 밀고, 저널에
회차마다 남고, 드라이런은 버스 파라미터를 안 건드리고, 판단 실패는 회차 스킵이고, target=none 은
조용히 넘어가고, E-stop·추론 사망은 루프를 끝낸다. LLM·버스·게이트웨이는 전부 가짜다.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from piper_plugin import Env
from piper_plugin.api import Response
from piper_plugin.detections import payload

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("recycling_main", REPO / "plugins" / "recycling" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Recycling


class FakeBus:
    def __init__(self):
        now_ns = time.time_ns()
        self.dets = {"top": payload("top", 1, now_ns, (480, 640), {0: "bottle"}, [[1, 1, 50, 50]], [0.9], [0])}
        self.state = {}
        self.cmds = []
        self.last = None

    def detection_names(self):
        return list(self.dets)

    def get_detections(self, name):
        return self.dets.get(name)

    def put_plugin_state(self, name, st):
        self.state = st

    def pop_plugin_command(self, name):
        return self.cmds.pop(0) if self.cmds else None

    def last_estop(self):
        return self.last


class FakeApi:
    def __init__(self, slots=None, judge_status=200, policy=None, inference=True):
        self.calls = []
        self.slots = slots if slots is not None else {"target": "bottle", "destination": "plastic_bin", "reason": "r"}
        self.judge_status = judge_status
        self.policy = policy or {"running": True, "type": "pi05", "takes_language": True}
        self.inference = inference
        self.base = "http://fake"

    available = True

    def request(self, method, path, json_body=None, timeout_s=None):
        self.calls.append((method, path, json_body))
        if path == "/api/vision/judge":
            if self.judge_status != 200:
                return Response(self.judge_status, json.dumps({"detail": "llm down"}).encode())
            return Response(200, json.dumps({"slots": self.slots}).encode())
        if path == "/api/models/inference/policy":
            return Response(200, json.dumps(self.policy).encode())
        if path == "/api/activity":
            return Response(200, json.dumps({"running": ["inference"] if self.inference else []}).encode())
        return Response(200, b"{}")

    def get(self, path, **kw):
        return self.request("GET", path)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json)


def _plugin(tmp_path, api: FakeApi, **cfg):
    Recycling = _load()
    base = {"max_episodes": 2, "episode_timeout_s": 0.01, "settle_s": 0, "reset_wait_s": 0, "freshness_s": 3}
    base.update(cfg)
    p = Recycling(Env(name="recycling", dir=tmp_path, runtime_dir=tmp_path / "rt", config=base, api_base="http://fake", token="t"))
    p.poll_s = 0
    bus = FakeBus()
    p._bus = bus
    p.api = api
    p.robot.api = api
    return p, bus


def _paths(api):
    return [pth for _m, pth, _b in api.calls]


def test_full_loop_pushes_task_and_reset(tmp_path):
    api = FakeApi()
    p, _ = _plugin(tmp_path, api)
    assert p.run() == 0
    tasks = [b for m, pth, b in api.calls if pth == "/api/params"]
    assert [t["params"]["task"] for t in tasks] == ["pick up the bottle and put it in the plastic_bin"] * 2
    assert _paths(api).count("/api/params/reset") == 2
    judge = next(b for m, pth, b in api.calls if pth == "/api/vision/judge")
    assert judge["user"].startswith("[top 640x480] bottle(0.90)") and "system" not in judge   # 규칙 비우면 게이트웨이 기본


def test_journal_records_each_episode(tmp_path):
    p, _ = _plugin(tmp_path, FakeApi())
    p.run()
    lines = [json.loads(l) for l in p.journal_path.read_text().splitlines()]
    assert [l["episode"] for l in lines] == [1, 2]
    assert lines[0]["decision"]["extra"]["target"] == "bottle" and lines[0]["detections"].startswith("[top")
    assert lines[0]["result"]["timeout"] is True        # 완료 판정 센서가 없다 — 타임아웃으로 끝난다


def test_dry_run_never_touches_the_bus_params(tmp_path):
    api = FakeApi()
    p, bus = _plugin(tmp_path, api, dry_run=True)
    bus.dets = {}                                          # 검출기도 없다
    p.run()
    assert "/api/params" not in _paths(api) and "/api/params/reset" not in _paths(api)
    assert "/api/models/inference/policy" not in _paths(api)   # 드라이런은 정책 검사도 건너뛴다
    assert p.journal_path.exists()


def test_judge_failure_skips_episode_not_the_run(tmp_path):
    api = FakeApi(judge_status=502)
    p, _ = _plugin(tmp_path, api)
    p.run()
    lines = [json.loads(l) for l in p.journal_path.read_text().splitlines()]
    assert len(lines) == 2 and all(l["decision"]["kind"] == "skip" and "judge_failed" in l["decision"]["reason"] for l in lines)
    assert "/api/params" not in _paths(api)


def test_none_target_skips_quietly(tmp_path):
    api = FakeApi(slots={"target": "none", "destination": "none", "reason": "비었다"})
    p, _ = _plugin(tmp_path, api)
    p.run()
    lines = [json.loads(l) for l in p.journal_path.read_text().splitlines()]
    assert all(l["decision"]["reason"] == "nothing_to_pick" for l in lines) and "/api/params" not in _paths(api)


def test_no_fresh_detections_skips(tmp_path):
    api = FakeApi()
    p, bus = _plugin(tmp_path, api)
    bus.dets = {}
    p.run()
    lines = [json.loads(l) for l in p.journal_path.read_text().splitlines()]
    assert all(l["decision"]["reason"] == "no_fresh_detections" for l in lines)
    assert "/api/vision/judge" not in _paths(api)


def test_estop_aborts_mid_wait(tmp_path):
    api = FakeApi()
    p, bus = _plugin(tmp_path, api, max_episodes=5, episode_timeout_s=5)
    bus.last = {"ts": time.time() + 60, "reason": "manual"}
    p.run()
    assert p._stop_reason.startswith("E-stop")


def test_inference_death_aborts(tmp_path):
    api = FakeApi(inference=False)
    p, _ = _plugin(tmp_path, api, max_episodes=5)
    p.run()
    assert "추론이 멈췄다" in p._stop_reason


def test_act_without_skills_is_refused_but_with_skills_switches_checkpoints(tmp_path):
    api = FakeApi(policy={"running": True, "type": "act", "takes_language": False})
    p, _ = _plugin(tmp_path, api)
    p.run()
    assert "task 를 읽지 않는다" in p._stop_reason

    api = FakeApi(policy={"running": True, "type": "act", "takes_language": False},
                  slots={"target": "bottle", "destination": "plastic_bin", "reason": "r", "skill": "plastic_pick"})
    p, _ = _plugin(tmp_path, api, skills={"plastic_pick": "/ck/plastic"})
    p.run()
    judge = next(b for m, pth, b in api.calls if pth == "/api/vision/judge")
    assert judge["skills"] == {"plastic_pick": "/ck/plastic"}          # LLM 이 고를 수 있는 값 = 스킬 이름
    starts = [b for m, pth, b in api.calls if pth == "/api/models/inference/start"]
    assert len(starts) == 1 and starts[0]["checkpoint_path"] == "/ck/plastic"
    assert "/api/params" not in _paths(api)                              # ACT 에는 task 를 안 민다


def test_skip_command_ends_the_wait_early(tmp_path):
    api = FakeApi()
    p, bus = _plugin(tmp_path, api, max_episodes=1, episode_timeout_s=30)
    bus.cmds = [{"name": "skip", "args": {}}]
    t0 = time.monotonic()
    p.run()
    assert time.monotonic() - t0 < 5
    lines = [json.loads(l) for l in p.journal_path.read_text().splitlines()]
    assert lines[0]["result"]["done"] is True


def test_bundled_manifest_declares_every_scope_the_code_uses():
    from app.core import plugin_scopes
    from app.services.plugins import read_manifest

    info = read_manifest(REPO / "plugins" / "recycling")
    assert info.manifest is not None, info.error
    scopes = frozenset(info.manifest.permissions)
    for method, path in [("POST", "/api/vision/judge"), ("POST", "/api/params"), ("POST", "/api/params/reset"),
                         ("GET", "/api/models/inference/policy"), ("GET", "/api/activity"),
                         ("POST", "/api/models/inference/start"), ("POST", "/api/models/inference/stop"),
                         ("POST", "/api/eval/log")]:
        assert plugin_scopes.allows(scopes, method, path), f"{method} {path} 범위가 매니페스트에 없다"
    assert info.manifest.mission is True
