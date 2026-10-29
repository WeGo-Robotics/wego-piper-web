"""시나리오 설계기 2단계 — 녹화와 계획을 잇는다 (feature/scenario-designer.md §8·§9·§11).

지키는 것:

1. **시도 ↔ 에피소드가 1:1** — ← 재녹화는 같은 시도를 다시 찍고 줄을 안 남기며, 실패로 표시한
   시도는 저장되고(미결) 블록 끝에 다시 들어간다. 이어 찍으면 이미 성공한 시도를 건너뛴다.
2. **옮길 것만** — 연쇄면 "그대로", 되돌리기면 집은 것만 도로, 앞이 실패·미루기면 판 전체.
3. **미결은 학습을 막는다** — 정할 때까지 학습 시작이 거절되고, 제외는 지우지 않고
   `--dataset.episodes` 로 거른다. 에피소드를 지우면 로그 번호가 같이 당겨진다.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
from scenario_cursor import Cursor, append_line, write_plan  # noqa: E402

from app.services.scenario import progress, sidecar  # noqa: E402
from test_scenario_plan import _gen  # noqa: E402


def _eps(*blocks):
    """블록 번호 목록 → 최소 시도 목록 (id 는 순서대로 t0, t1, …)."""
    return [{"id": f"t{i}", "block": b, "task": f"task {i}"} for i, b in enumerate(blocks)]


# ── 1. 커서 ──

def test_saved_episode_binds_the_trial_and_moves_on():
    c = Cursor("p", _eps(1, 1, 1))
    assert c.begin() == {"id": "t0", "task": "task 0"}
    line = c.saved(0, now="T")
    assert line == {"plan": "p", "trial": "t0", "episode_index": 0, "outcome": "success", "at": "T"}
    assert c.begin()["id"] == "t1"
    assert c.state()["prev"] == "t0" and c.state()["done"] == 1


def test_rerecord_keeps_the_same_trial_and_leaves_no_line():
    c = Cursor("p", _eps(1, 1))
    c.begin()
    c.mark_rerecord()
    st = c.state()
    assert st["rerecord"] and st["next"] == "t0", "← 뒤 리셋 동안의 '다음' 은 같은 시도다"
    c.dropped()
    assert c.begin()["id"] == "t0"
    assert c.saved(0)["trial"] == "t0"


def test_failed_trial_is_saved_pending_and_comes_back_at_block_end():
    c = Cursor("p", _eps(1, 1, 1, 2, 2))
    c.begin()
    assert c.set_failed(True)
    line = c.saved(0)
    assert line["outcome"] == "failed" and line["decision"] == "pending"
    assert c.queue == ["t1", "t2", "t0", "t3", "t4"]
    assert c.state()["prev"] is None, "실패한 시도 뒤는 연쇄가 끊긴다"
    assert c.state()["done"] == 0


def test_failed_last_of_block_is_retried_right_away():
    c = Cursor("p", _eps(1, 2))
    c.begin()
    c.set_failed(True)
    c.saved(0)
    assert c.queue == ["t0", "t1"]


def test_unfail_before_save_saves_as_success():
    c = Cursor("p", _eps(1))
    c.begin()
    c.set_failed(True)
    c.set_failed(False)
    assert c.saved(0)["outcome"] == "success"
    assert c.finished


def test_fail_without_an_episode_is_refused():
    c = Cursor("p", _eps(1))
    assert c.set_failed(True) is False


def test_defer_moves_the_next_trial_to_its_block_end():
    c = Cursor("p", _eps(1, 1, 1, 2))
    assert c.defer() == "t0"
    assert c.queue == ["t1", "t2", "t0", "t3"]


def test_defer_of_the_last_in_its_block_goes_to_the_very_end():
    c = Cursor("p", _eps(1, 2, 2))
    assert c.defer() == "t0"
    assert c.queue == ["t1", "t2", "t0"], "제자리에 두면 미룬 게 아니다"
    c2 = Cursor("p", _eps(1))
    assert c2.defer() is None


def test_resume_skips_trials_already_saved():
    c = Cursor("p", _eps(1, 1, 1), done={"t0", "t2"})
    assert c.queue == ["t1"] and c.done == 2 and c.total == 3
    c.begin()
    c.saved(5)
    assert c.finished


def test_sidecar_files(tmp_path):
    append_line(tmp_path, {"episode_index": 0, "trial": "t0"})
    append_line(tmp_path, {"episode_index": 1, "trial": "t1"})
    lines = (tmp_path / "meta" / "piper_scenario_log.jsonl").read_text().splitlines()
    assert [json.loads(x)["trial"] for x in lines] == ["t0", "t1"]
    assert write_plan(tmp_path, {"plan_id": "a"}) is True
    assert write_plan(tmp_path, {"plan_id": "b"}) is False, "있는 계획은 덮지 않는다"
    assert sidecar.read_plan(tmp_path)["plan_id"] == "a"


# ── 2. 옮길 것 ──

def _payload(plan, done=()):
    return {"plan_id": sidecar.plan_id(plan), "plan": plan, "done": list(done)}


def _state(cursor):
    return cursor.state()


def test_chain_next_needs_nothing_moved():
    plan = _gen({"round_style": "chain", "variants": {}})
    eps = plan["episodes"]
    pair = next((a, b) for a, b in zip(eps, eps[1:]) if a["round"] == b["round"])
    c = Cursor("x", [pair[0], pair[1]])
    c.begin()
    v = progress.view({"plan_id": "x", "plan": {**plan, "episodes": [pair[0], pair[1]]}, "done": []}, _state(c))
    assert v["current"]["relayout"], "세션 첫 시도는 판 전체"
    assert v["next"]["changes"] == [], "연쇄면 앞 시도가 끝난 판이 곧 다음 배치다"


def test_restore_next_puts_the_picked_object_back():
    ep = {"id": "a", "block": 1, "layout": [{"obj": "red", "cell": "B2"}, {"obj": "yellow", "cell": "C3"}],
          "picks": [{"obj": "red", "cell": "B2", "dest": "bin"}]}
    nxt = {**ep, "id": "b", "picks": [{"obj": "yellow", "cell": "C3", "dest": "bin"}]}
    plan = {"episodes": [ep, nxt]}
    c = Cursor("x", plan["episodes"])
    c.begin()
    v = progress.view({"plan_id": "x", "plan": plan, "done": []}, c.state())
    assert v["next"]["changes"] == [{"obj": "red", "from": None, "to": "B2"}]


def test_after_a_failure_the_next_is_laid_out_fresh():
    ep = {"id": "a", "block": 1, "layout": [{"obj": "red", "cell": "B2"}], "picks": []}
    plan = {"episodes": [ep, {**ep, "id": "b"}]}
    c = Cursor("x", plan["episodes"])
    c.begin()
    c.set_failed(True)
    v = progress.view({"plan_id": "x", "plan": plan, "done": []}, c.state())
    assert v["current_failed"] and v["next"]["relayout"] and v["next"]["changes"] is None


def test_rerecord_shows_how_to_restore_the_same_layout():
    ep = {"id": "a", "block": 1, "layout": [{"obj": "red", "cell": "B2"}],
          "picks": [{"obj": "red", "cell": "B2", "dest": "bin"}]}
    plan = {"episodes": [ep, {**ep, "id": "b"}]}
    c = Cursor("x", plan["episodes"])
    c.begin()
    c.mark_rerecord()
    v = progress.view({"plan_id": "x", "plan": plan, "done": []}, c.state())
    assert v["rerecord"] and v["next"]["id"] == "a"
    assert v["next"]["changes"] == [{"obj": "red", "from": None, "to": "B2"}]


def test_moves_pair_removal_and_placement_of_the_same_object():
    before = progress.after({"layout": [{"obj": "red", "cell": "A1"}, {"obj": "y", "cell": "B1"}], "picks": []})
    out = progress.diff(before, [{"obj": "red", "cell": "C3"}])
    assert out == [{"obj": "red", "from": "A1", "to": "C3"}, {"obj": "y", "from": "B1", "to": None}]


def test_view_before_the_wrapper_reports_uses_the_start_payload():
    plan = _gen()
    first = plan["episodes"][0]["id"]
    v = progress.view(_payload(plan, done=[first]), None)
    assert v["next"]["id"] == plan["episodes"][1]["id"]
    assert v["done"] == 1 and v["remaining"] == len(plan["episodes"]) - 1
    assert v["block"]["total"] == max(ep["block"] for ep in plan["episodes"])


def test_state_from_another_plan_is_ignored():
    plan = _gen()
    v = progress.view(_payload(plan), {"plan_id": "old", "current": "zzz", "next": None})
    assert v["current"] is None and v["next"]["id"] == plan["episodes"][0]["id"]


# ── 3. 사이드카·결정·학습 ──

def _dataset(tmp_path, total=5, lines=()):
    root = tmp_path / "org" / "ds"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({"total_episodes": total}))
    for line in lines:
        append_line(root, line)
    return root


FAILED = {"plan": "p", "trial": "t1", "episode_index": 1, "outcome": "failed", "decision": "pending"}
OK = {"plan": "p", "trial": "t0", "episode_index": 0, "outcome": "success"}


def test_later_lines_win_and_only_failed_episodes_take_a_decision(tmp_path):
    root = _dataset(tmp_path, lines=[OK, FAILED])
    (root / "meta" / "piper_scenario_log.jsonl").open("a").write("{broken\n")
    assert sidecar.pending(root) == [1]
    assert sidecar.done_trials(root, "p") == {"t0"}
    assert sidecar.done_trials(root, "other") == set()
    assert sidecar.decide(root, [0, 1], "exclude", by="test") == [1]
    assert sidecar.pending(root) == [] and sidecar.excluded(root) == [1]
    assert sidecar.kept_episodes(root) == [0, 2, 3, 4]
    sidecar.decide(root, [1], "include", by="test")
    assert sidecar.kept_episodes(root) is None, "결정은 뒤집을 수 있다 — 지우지 않았으므로"
    assert sidecar.summary(root) == {"bound": 2, "failed": 1, "pending": 0, "excluded": 0}


def test_no_log_means_no_scenario(tmp_path):
    root = _dataset(tmp_path)
    assert sidecar.summary(root) is None and sidecar.kept_episodes(root) is None


def test_training_refuses_undecided_failures_and_filters_excluded(tmp_path, monkeypatch):
    from app.core.cli_mapping import build_train_args
    from app.routers import training

    root = _dataset(tmp_path, lines=[OK, FAILED])
    monkeypatch.setattr(training, "find_dataset_path", lambda repo: root)
    with pytest.raises(HTTPException) as e:
        training.apply_scenario_exclusions({"dataset_repo_id": "org/ds"}, enforce=True)
    assert e.value.status_code == 409
    # 미리보기는 거절하지 않는다
    assert "dataset_episodes" not in training.apply_scenario_exclusions({"dataset_repo_id": "org/ds"}, enforce=False)

    sidecar.decide(root, [1], "exclude", by="training-start")
    params = training.apply_scenario_exclusions({"dataset_repo_id": "org/ds"}, enforce=True)
    assert "--dataset.episodes=[0,2,3,4]" in build_train_args(params)


def test_train_cli_params_carries_the_exclusion(tmp_path, monkeypatch):
    """로컬 시작과 클라우드 임대가 같은 조립기를 쓴다 — 한쪽에서만 걸러지면 안 된다."""
    from app.routers import training

    root = _dataset(tmp_path, lines=[OK, {**FAILED, "decision": "exclude"}])
    monkeypatch.setattr(training, "find_dataset_path", lambda repo: root)
    params, *_ = training.train_cli_params(training.TrainStartRequest(dataset_repo_id="org/ds"))
    assert params["dataset_episodes"] == "[0,2,3,4]"


def test_dataset_scenario_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import datasets

    root = _dataset(tmp_path, lines=[OK, FAILED])
    write_plan(root, {"plan_id": "p", "template": "sort", "seed": 3, "episodes": [{"id": "t0"}, {"id": "t1"}]})
    monkeypatch.setattr(datasets, "find_dataset_path", lambda repo: root)
    c = TestClient(app)
    r = c.get("/api/datasets/org/ds/scenario").json()
    assert r["plan"] == {"plan_id": "p", "template": "sort", "seed": 3, "total": 2}
    assert r["episodes"]["1"] == {"trial": "t1", "outcome": "failed", "decision": "pending"}
    assert r["pending"] == [1]
    r = c.post("/api/datasets/org/ds/scenario/decision",
               json={"episodes": [1], "decision": "exclude", "by": "training-start"}).json()
    assert r == {"changed": [1], "pending": [], "excluded": [1]}
    assert c.post("/api/datasets/org/ds/scenario/decision",
                  json={"episodes": [1], "decision": "maybe"}).status_code == 422


def test_deleting_episodes_renumbers_the_log(tmp_path):
    from piper_phase.sidecar import remap_after_delete

    ds = tmp_path / "myset"
    (ds / "meta").mkdir(parents=True)
    old = _dataset(tmp_path / "x", total=4, lines=[
        {**OK, "episode_index": 0}, {**OK, "trial": "t1", "episode_index": 1},
        {**FAILED, "trial": "t2", "episode_index": 2}, {**OK, "trial": "t3", "episode_index": 3}])
    old.rename(tmp_path / "myset_old")
    write_plan(tmp_path / "myset_old", {"plan_id": "p"})
    moved = remap_after_delete(ds, [1])
    assert {"piper_scenario_log.jsonl", "piper_scenario.json"} <= set(moved)
    eps = sidecar.episodes(ds)
    assert {i: e["trial"] for i, e in eps.items()} == {0: "t0", 1: "t2", 2: "t3"}
    assert sidecar.pending(ds) == [1], "옛 #2 의 미결이 새 #1 로 따라간다"


# ── 4. 녹화 API ──

@pytest.fixture
def scenario_spec():
    from test_scenario_plan import FIXTURES

    from app.routers.recording import ScenarioSpec

    fx = FIXTURES["sort"]
    return ScenarioSpec(template="sort", seed=7, board=fx["board"], catalog=fx["catalog"],
                        values={**fx["values"], "episodes": 20})


def _payload_of(spec, repo):
    from app.routers import recording

    return asyncio.run(recording._scenario_payload(spec, repo))


def test_resume_payload_skips_done_trials(tmp_path, monkeypatch, scenario_spec):
    from app.routers import recording

    monkeypatch.setattr(recording.settings, "lerobot_dir", tmp_path)
    first = _payload_of(scenario_spec, "org/ds")
    assert first["done"] == [] and "summary" not in first["plan"]
    root = _dataset(tmp_path)
    write_plan(root, {"plan_id": first["plan_id"], **first["plan"]})
    t0 = first["plan"]["episodes"][0]["id"]
    append_line(root, {"plan": first["plan_id"], "trial": t0, "episode_index": 0, "outcome": "success"})
    again = _payload_of(scenario_spec, "org/ds")
    assert again["plan_id"] == first["plan_id"] and again["done"] == [t0]


def test_a_different_plan_on_the_same_dataset_is_refused(tmp_path, monkeypatch, scenario_spec):
    from app.routers import recording

    monkeypatch.setattr(recording.settings, "lerobot_dir", tmp_path)
    write_plan(_dataset(tmp_path), {"plan_id": "someone-else"})
    with pytest.raises(HTTPException) as e:
        _payload_of(scenario_spec, "org/ds")
    assert e.value.status_code == 409


def test_bad_design_is_a_400(tmp_path, monkeypatch, scenario_spec):
    from app.routers import recording

    monkeypatch.setattr(recording.settings, "lerobot_dir", tmp_path)
    bad = scenario_spec.model_copy(update={"values": {"nope": 1}})
    with pytest.raises(HTTPException) as e:
        _payload_of(bad, "org/ds")
    assert e.value.status_code == 400


def test_progress_api_and_commands(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.control_bridge import control_bridge
    from app.services.record_manager import record_manager

    c = TestClient(app)
    monkeypatch.setattr(control_bridge, "scenario", lambda: (None, None))
    assert c.get("/api/recording/scenario").json() == {"active": False}

    plan = _gen()
    monkeypatch.setattr(control_bridge, "scenario", lambda: (_payload(plan), None))
    r = c.get("/api/recording/scenario").json()
    assert r["active"] and r["recording"] is False and r["next"]["id"] == plan["episodes"][0]["id"]
    # 녹화가 안 돌면 표시·미루기는 거절된다
    assert c.post("/api/recording/scenario/fail", json={"failed": True}).status_code == 400

    sent = []
    monkeypatch.setattr(type(record_manager), "is_running", property(lambda self: True))
    monkeypatch.setattr(control_bridge, "send", lambda cmd: sent.append(cmd) or True)
    assert c.post("/api/recording/scenario/fail", json={"failed": True}).status_code == 200
    assert c.post("/api/recording/scenario/fail", json={"failed": False}).status_code == 200
    assert c.post("/api/recording/scenario/defer").status_code == 200
    assert sent == ["scenario_fail", "scenario_unfail", "scenario_defer"]
    # 계획을 따르는 녹화는 task 를 손으로 못 바꾼다
    assert c.post("/api/recording/task", json={"task": "x"}).status_code == 409
