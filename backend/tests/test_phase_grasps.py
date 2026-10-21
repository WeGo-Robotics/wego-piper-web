"""파지 순간 추출 — 에피소드 → POS분석 탭의 원료 (feature/episode-analyzer.md)."""

import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from piper_phase import APPROACH, GRASP, HOLD, IDLE, RELEASE
from piper_phase import grasps as PG
from piper_robot.kinematics import link_transforms, norm_to_rad

from app.main import app
from app.services.dataset_scanner import find_dataset_path

_NAMES_1 = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]
_NAMES_2 = [f"{s}_{n}" for s in ("left", "right") for n in _NAMES_1]
_SETTLE = 8  # 15fps 의 0.5초 — 그리퍼는 GRASP 끝에서 이만큼 더 보고 최소를 잡는다


def _write(root, names, episodes: dict[int, np.ndarray], labels: dict[int, list],
           actions: dict[int, np.ndarray] | None = None):
    """최소 v3 데이터셋: info.json + data parquet + phase_labels.json. 지령은 기본이 실측 그대로."""
    (root / "meta").mkdir(parents=True)
    (root / "data" / "chunk-000").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({
        "fps": 15, "features": {"observation.state": {"names": names}}}))
    actions = actions or episodes
    rows = [{"episode_index": ep, "frame_index": i, "observation.state": s.tolist(),
             "action": actions[ep][i].tolist()}
            for ep, st in episodes.items() for i, s in enumerate(st)]
    pd.DataFrame(rows).to_parquet(root / "data" / "chunk-000" / "file-000.parquet")
    (root / "meta" / "phase_labels.json").write_text(json.dumps({
        "version": 3, "episodes": {
            str(ep): {"segments": segs, "reviewed": ep == 0, "frames": len(episodes[ep])}
            for ep, segs in labels.items()}}))
    return root


def _pose(j: float, grip: float, width=7, arm=0) -> np.ndarray:
    s = np.zeros(width)
    s[7 * arm: 7 * arm + 6] = [j, 10 + j, -10 - j, j, j, j]
    s[7 * arm + 6] = grip
    return s


def test_no_labels_means_none(tmp_path):
    """라벨이 없으면 "분석 전" — 빈 목록과 구분돼야 화면이 [분석] 을 권할 수 있다."""
    root = _write(tmp_path / "ds", _NAMES_1, {0: np.stack([_pose(0, 100)] * 5)}, {})
    (root / "meta" / "phase_labels.json").unlink()
    assert PG.extract(root) is None


def test_grasp_start_frame_and_outcome(tmp_path):
    """GRASP **시작** 프레임의 자세, HOLD 로 넘어갔으면 hold · 아니면 empty."""
    st = np.stack([_pose(f, 100 - f) for f in range(40)])
    segs = [[0, 9, APPROACH], [10, 14, GRASP], [15, 24, HOLD],
            [25, 29, RELEASE], [30, 34, GRASP], [35, 39, APPROACH]]
    out = PG.extract(_write(tmp_path / "ds", _NAMES_1, {0: st}, {0: segs}))

    ev = out["events"]
    assert [(e["cycle"], e["frame"], e["outcome"]) for e in ev] == [(1, 10, "hold"), (2, 30, "empty")]
    assert ev[0]["arm"] is None and ev[0]["reviewed"] is True
    want = np.degrees(norm_to_rad(st[[10], :6]))[0]
    assert ev[0]["joints"] == pytest.approx(want, abs=0.01)
    # 그리퍼만 **닫기를 마친 뒤** — 구간 끝(14)에서 0.5초(8프레임) 더 본 최소 열림.
    # 시작은 늘 열려 있어 아무것도 안 말해주고, 구간 끝은 아직 닫히는 중일 수 있다.
    assert ev[0]["gripper"] == pytest.approx(100 - (14 + _SETTLE))
    assert out["episodes"] == 1 and out["reviewed"] == 1


def test_stick_is_the_fk_chain_of_each_event(tmp_path):
    """스틱은 이벤트마다 **자기** 자세의 사슬이다 (베이스 → … → 그리퍼 끝).

    `tf[:, idx, :3, 3]` 로 뽑았을 때는 numpy 가 축 순서를 바꿔 이벤트마다
    마디 수가 달라지고 다른 이벤트의 점이 섞였다 — 오류는 안 났다.
    """
    st = np.stack([_pose(0, 100)] * 10 + [_pose(30, 100)] * 10)
    segs = [[0, 1, IDLE], [2, 3, GRASP], [4, 11, HOLD], [12, 13, GRASP], [14, 19, HOLD]]
    ev = PG.extract(_write(tmp_path / "ds", _NAMES_1, {0: st}, {0: segs}))["events"]

    assert len(ev) == 2
    for e in ev:
        assert len(e["stick"]) == len(PG.STICK_LABELS) == 6
        assert e["stick"][0] == [0, 0, 0], "첫 마디는 base_link 원점"
        assert e["stick"][-1] == e["tip"], "끝 마디가 곧 말단(그리퍼 손가락 뿌리 가운데)"
    assert ev[0]["stick"] != ev[1]["stick"]
    pos = link_transforms(norm_to_rad(st[[12], :6]))[0, :, :3, 3] * 1000
    assert ev[1]["stick"][2] == pytest.approx(pos[3], abs=0.1), "세 번째 마디 = link3 원점"


def test_bimanual_uses_the_labelers_arm(tmp_path):
    """양팔이면 라벨러와 **같은 팔**(더 활동적인 그리퍼)의 관절을 본다."""
    right_busy = np.stack([np.r_[_pose(5, 100), _pose(20, 100 - 3 * f)] for f in range(20)])
    segs = [[0, 4, APPROACH], [5, 9, GRASP], [10, 19, HOLD]]
    ev = PG.extract(_write(tmp_path / "ds", _NAMES_2, {0: right_busy}, {0: segs}))["events"]

    assert ev[0]["arm"] == "right"
    want = np.degrees(norm_to_rad(right_busy[[5], 7:13]))[0]
    assert ev[0]["joints"] == pytest.approx(want, abs=0.01)
    assert ev[0]["gripper"] == pytest.approx(100 - 3 * (9 + _SETTLE)), "오른 그리퍼, 끝(9)+0.5초"


def _grasping_arm(n_approach: int, total: int) -> tuple[np.ndarray, np.ndarray]:
    """FSM 이 GRASP→HOLD 로 읽는 한 팔(7채널): 이동 → 닫힘(실측 34 에서 멈춤) → 놓기."""
    st, ac = [], []

    def push(g_state, g_cmd, n, move=True):
        for _ in range(n):
            j = 30 - abs((len(st) * 3) % 120 - 60) if move else st[-1][0]
            st.append([j, 10, -10, 0, 0, 0, g_state])
            ac.append([j, 10, -10, 0, 0, 0, g_cmd])

    push(100, 100, n_approach)
    for i in range(10):
        push(100 - 66 * (i + 1) / 10, 0, 1, move=False)
    push(34, 0, 40)
    for i in range(10):
        push(34 + 66 * (i + 1) / 10, 100, 1, move=False)
    push(100, 100, total - len(st))
    return np.array(st, float), np.array(ac, float)


def test_bimanual_grasps_of_both_arms(tmp_path):
    """⚠ 사이드카에는 **한 팔**의 페이즈만 있다 — 실측 양팔 데이터는 거의 매 에피소드
    두 팔이 다 잡는데, 사이드카만 읽으면 절반이 조용히 빠졌다.
    주 팔은 사이드카(사람이 고친 라벨)에서, 나머지 팔은 같은 FSM 을 즉석으로."""
    from piper_phase.fsm import main_gripper

    ls, la = _grasping_arm(30, 140)
    rs, ra = _grasping_arm(40, 140)
    st, ac = np.hstack([ls, rs]), np.hstack([la, ra])
    main = main_gripper(st) // 7
    start = (30, 40)[main]
    segs = [[0, start - 1, APPROACH], [start, start + 4, GRASP], [start + 5, 139, HOLD]]
    ev = PG.extract(_write(tmp_path / "ds", _NAMES_2, {0: st}, {0: segs}, {0: ac}))["events"]

    by_arm = {e["arm"]: e for e in ev}
    assert set(by_arm) == {"left", "right"}
    main_name, other_name = ("left", "right")[main], ("left", "right")[1 - main]
    assert by_arm[main_name]["source"] == "sidecar" and by_arm[main_name]["frame"] == start
    other = by_arm[other_name]
    assert other["source"] == "fsm" and other["outcome"] == "hold" and other["reviewed"] is False
    assert other["frame"] == (30, 40)[1 - main], "그 팔이 닫히기 시작한 프레임"
    assert other["gripper"] == pytest.approx(34, abs=8), "닫기를 마친 순간 = 물체 폭"


def test_yaw_turns_with_the_base(tmp_path):
    """yaw 는 손가락 여닫는 축의 수평 방향이다 — 베이스(J1)를 θ 돌리면 yaw 도 θ 돈다.
    축 규약(link6 국소 y)이 틀리면 J1 이 아닌 다른 관절과 엮여 이 관계가 깨진다."""
    def pose(j1):
        return np.array([j1, 40, -40, 10, 30, 20, 100], float)

    st = np.stack([pose(0)] * 6 + [pose(25)] * 6)
    segs = [[0, 1, IDLE], [2, 3, GRASP], [4, 7, HOLD], [8, 9, GRASP], [10, 11, HOLD]]
    ev = PG.extract(_write(tmp_path / "ds", _NAMES_1, {0: st}, {0: segs}))["events"]

    assert all(e["yaw"] is not None and 0 <= e["yaw"] < 180 for e in ev)
    turned = np.degrees(norm_to_rad(st[[8], :6]) - norm_to_rad(st[[2], :6]))[0, 0]
    assert (ev[1]["yaw"] - ev[0]["yaw"]) % 180 == pytest.approx(turned % 180, abs=0.2)


def test_closing_speed_catches_a_lunge(tmp_path):
    """⚠ 멈춰 서서 닫는 파지와 **닫으며 튀어나가는** 파지를 가른다 (§6 A2).

    알약 조제 로봇 사례: 접근하다 앞으로 툭 튀는 동작의 원인이 시연 버릇(잘 안 잡힐 것 같으면
    튀어나가며 집음)이었다. 그 버릇은 닫는 동안의 움직임으로 드러난다.
    """
    def pose(j2):
        return [0, j2, -40, 0, 30, 0, 100]

    still = [pose(40)] * 30                                     # 0~29: 가만히, 10~14 에서 닫음
    lunge = [pose(40)] * 11 + [pose(40 + 4 * k) for k in range(1, 5)] + [pose(56)] * 15  # 닫기 시작(10) 뒤 11~14 에서 J2 가 민다
    segs = [[0, 9, APPROACH], [10, 14, GRASP], [15, 29, HOLD]]
    ev = PG.extract(_write(tmp_path / "ds", _NAMES_1,
                           {0: np.array(still, float), 1: np.array(lunge, float)},
                           {0: segs, 1: segs}))["events"]
    by_ep = {e["episode"]: e for e in ev}

    assert by_ep[0]["closing_speed"] == pytest.approx(0, abs=0.5)
    assert by_ep[1]["closing_speed"] > 100, "닫으며 민 것이 속도로 안 잡혔다"
    assert by_ep[1]["approach_speed"] < by_ep[1]["closing_speed"], "튀는 것은 닫기 전이 아니라 닫는 중이다"
    for e in ev:
        assert e["trail"][e["trail_at"]] == e["tip"], "trail_at 은 파지 순간(닫기 시작)의 자리"
        assert len(e["trail"]) == 8 + 5, "닫기 0.5초(8프레임) 전부터 닫기 끝(14)까지"


def test_labels_past_the_data_are_dropped(tmp_path):
    """에피소드를 자르고 재분석 안 했으면 라벨이 데이터보다 길다 — 그 파지는 버린다.
    마지막 프레임으로 눌러 담으면 엉뚱한 자세가 파지로 찍힌다."""
    st = np.stack([_pose(0, 100)] * 10)
    segs = [[0, 4, APPROACH], [5, 6, GRASP], [7, 11, HOLD], [12, 13, GRASP], [14, 15, HOLD]]
    ev = PG.extract(_write(tmp_path / "ds", _NAMES_1, {0: st}, {0: segs}))["events"]
    assert [e["frame"] for e in ev] == [5]


# ── API ──

def test_unlabeled_dataset_is_200_not_404(tmp_path, monkeypatch):
    """"분석 전" 은 오류가 아니다 — 화면이 번역된 오류 문구로 분기할 수 없으므로
    상태 코드가 아니라 `labeled` 로 가른다. 라벨을 쓰면 캐시를 건너뛰고 새로 읽는다."""
    import app.routers.phase as router

    root = _write(tmp_path / "ds", _NAMES_1, {0: np.stack([_pose(0, 100)] * 10)},
                  {0: [[0, 4, APPROACH], [5, 6, GRASP], [7, 9, HOLD]]})
    labels = (root / "meta" / "phase_labels.json").read_text()
    (root / "meta" / "phase_labels.json").unlink()
    monkeypatch.setattr(router, "find_dataset_path", lambda _id: root)
    c = TestClient(app)

    r = c.get("/api/phase/x/unlabeled/grasps")
    assert r.status_code == 200 and r.json() == {"labeled": False, "events": []}
    (root / "meta" / "phase_labels.json").write_text(labels)
    r = c.get("/api/phase/x/unlabeled/grasps").json()
    assert r["labeled"] is True and len(r["events"]) == 1


_DS = "wego-hansu/min_cube_071410"


@pytest.fixture(scope="module")
def client():
    if find_dataset_path(_DS) is None:
        pytest.skip(f"{_DS} 없음")
    return TestClient(app)


def test_grasps_route_matches_cycles(client):
    """GRASP 구간 수 = 이벤트 수. 두 번째 호출은 캐시에서 같은 값."""
    labels = client.get(f"/api/phase/{_DS}/labels")
    if labels.status_code != 200:
        pytest.skip("라벨 없음")
    n = sum(1 for e in labels.json()["episodes"].values()
            for s in e["segments"] if s[2] == GRASP)
    r = client.get(f"/api/phase/{_DS}/grasps")
    assert r.status_code == 200
    assert len(r.json()["events"]) == n
    assert client.get(f"/api/phase/{_DS}/grasps").json() == r.json()


def test_grasps_route_missing_dataset_404(client):
    assert client.get("/api/phase/org/nope/grasps").status_code == 404
