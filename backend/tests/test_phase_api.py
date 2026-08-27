"""페이즈 라벨 API (feature/01-phase-annotation.md §7)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.dataset_scanner import find_dataset_path

_DS = "wego-hansu/min_cube_071410"


@pytest.fixture(scope="module")
def client():
    if find_dataset_path(_DS) is None:
        pytest.skip(f"{_DS} 없음")
    return TestClient(app)


@pytest.fixture
def sidecar_backup():
    """테스트가 실제 사이드카를 건드리므로 원상복구한다."""
    from piper_phase import labeler as PL

    ds = find_dataset_path(_DS)
    if ds is None:
        pytest.skip("데이터셋 없음")
    labels, signals = PL.sidecar_paths(ds)
    saved = (labels.read_bytes() if labels.exists() else None,
             signals.read_bytes() if signals.exists() else None)
    yield ds
    for path, data in zip((labels, signals), saved):
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)


# ── 라우트 순서 (catch-all 사고 방지) ──

def test_fixed_dataset_routes_are_not_shadowed(client):
    """`/{dataset_id:path}` catch-all 아래에 두면 고정 경로가 404 가 된다.

    실제로 `/upload-status` 와 `/hf-cli` 가 그 상태였다 — 업로드 진행 상태가
    조회되지 않았고 프론트는 조용히 실패했다.
    """
    for path in ("/api/datasets/upload-status", "/api/datasets/hf-cli",
                 "/api/datasets/disk-usage"):
        assert client.get(path).status_code == 200, f"{path} 가 catch-all 에 먹혔다"


def test_catch_all_still_resolves_real_datasets(client):
    assert client.get(f"/api/datasets/{_DS}").json()["id"] == _DS


# ── 분석 ──

def test_defaults_expose_params_and_phases(client):
    r = client.get("/api/phase/defaults").json()
    assert "hold_gap" in r["params"] and "fps" in r["params"]
    # 목록 전체를 대조한다 — 마지막 이름을 박아두면 페이즈를 하나 붙일 때마다
    # 실패한다. 검사의 뜻은 "정본을 그대로 내보내는가" 다.
    from piper_phase import PHASE_NAMES

    assert r["phases"] == list(PHASE_NAMES)
    assert r["phases"][0] == "IDLE", "IDLE 은 0 이어야 한다 (사이드카가 정수로 저장)"


def test_unknown_param_is_rejected_not_ignored(client):
    """조용히 무시하면 "슬라이더를 움직였는데 왜 안 바뀌지"가 된다."""
    r = client.post(f"/api/phase/{_DS}/analyze", json={"params": {"없는키": 1}})
    assert r.status_code == 400
    assert "없는키" in r.json()["detail"]


def test_missing_dataset_404(client):
    assert client.post("/api/phase/org/nope/analyze", json={}).status_code == 404


def test_analyze_reports_cycles_and_outliers(client, sidecar_backup):
    r = client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0, 1, 2]})
    assert r.status_code == 200
    d = r.json()
    assert d["episodes"] == 3
    assert d["median_cycles"] == 3
    assert d["cycle_distribution"] == {"3": 3}


def test_partial_analysis_does_not_wipe_the_sidecar(client, sidecar_backup):
    """⚠ 문서가 의도한 "선택 에피소드만 재분석"(§3.5)이
    나머지를 날리는 사고가 되면 안 된다."""
    client.post(f"/api/phase/{_DS}/analyze", json={})           # 전체
    before = client.get(f"/api/phase/{_DS}/status").json()["episodes"]
    assert before == 50

    client.post(f"/api/phase/{_DS}/analyze",
                json={"episodes": [0, 1], "params": {"hold_gap": -8.0}})
    after = client.get(f"/api/phase/{_DS}/status").json()["episodes"]
    assert after == before, f"부분 분석이 사이드카를 {before}→{after} 로 날렸다"

    # 신호 캐시도 보존돼야 한다
    assert client.get(f"/api/phase/{_DS}/signals/49").status_code == 200


def test_review_state_survives_reanalysis(client, sidecar_backup):
    """사람이 검토한 표시가 재분석에 날아가면 안 된다."""
    client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0]})
    segs = client.get(f"/api/phase/{_DS}/labels").json()["episodes"]["0"]["segments"]
    client.put(f"/api/phase/{_DS}/labels/0",
               json={"segments": segs, "reviewed": True, "note": "확인함"})

    client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0]})
    ep0 = client.get(f"/api/phase/{_DS}/labels").json()["episodes"]["0"]
    assert ep0["reviewed"] is True and ep0["note"] == "확인함"


# ── 수동 편집 검증 ──

def test_segments_must_cover_every_frame(client, sidecar_backup):
    """깨진 구간을 저장하면 굽기 단계에서 프레임이 라벨 없이 남는다."""
    client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0]})
    for bad, why in [
        ([[0, 10, 1], [20, 30, 2]], "빈틈"),
        ([[0, 10, 1], [5, 30, 2]], "겹침"),
        ([[0, 10, 1]], "전체 미달"),
        ([[0, 10, 99]], "알 수 없는 페이즈"),
        ([], "빈 구간"),
    ]:
        r = client.put(f"/api/phase/{_DS}/labels/0", json={"segments": bad})
        assert r.status_code == 400, f"{why} 가 통과했다: {bad}"


def test_valid_edit_updates_cycles(client, sidecar_backup):
    from piper_phase import HOLD

    client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0]})
    frames = client.get(f"/api/phase/{_DS}/labels").json()["episodes"]["0"]["frames"]
    segs = [[0, frames // 2 - 1, 1], [frames // 2, frames - 1, HOLD]]
    r = client.put(f"/api/phase/{_DS}/labels/0", json={"segments": segs})
    assert r.status_code == 200
    assert r.json()["cycles"] == 1
    assert r.json()["edited_by"] == "auto+manual"


# ── 신호 ──

def test_signals_match_frame_count(client, sidecar_backup):
    client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0]})
    sig = client.get(f"/api/phase/{_DS}/signals/0").json()
    assert sig["frames"] == 830  # 문서 실측값
    assert len(sig["speed"]) == len(sig["phase"]) == 830


def test_signals_404_before_analysis(client, sidecar_backup):
    from piper_phase import labeler as PL

    labels, signals = PL.sidecar_paths(sidecar_backup)
    labels.unlink(missing_ok=True)
    signals.unlink(missing_ok=True)
    assert client.get(f"/api/phase/{_DS}/labels").status_code == 404
    assert client.get(f"/api/phase/{_DS}/signals/0").status_code == 404
    assert client.get(f"/api/phase/{_DS}/status").json() == {"analyzed": False}


def test_phase_uses_a_dedicated_process_manager():
    """업로드와 공유하면 "다른 작업이 진행 중" 409 로 서로를 막는다."""
    from app.services import dataset_jobs

    assert dataset_jobs.phase_pm is not dataset_jobs.upload_pm
    assert dataset_jobs.phase_pm is not dataset_jobs.edit_pm


def test_original_dataset_is_never_modified(client, sidecar_backup):
    """**원본을 in-place 로 고치지 않는다** — 사이드카에만 쓴다."""
    ds = sidecar_backup
    info = ds / "meta" / "info.json"
    before = info.read_bytes()
    data_before = {f: f.stat().st_mtime for f in (ds / "data").rglob("*.parquet")}

    client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0, 1]})

    assert info.read_bytes() == before, "info.json 이 수정됐다"
    assert {f: f.stat().st_mtime for f in (ds / "data").rglob("*.parquet")} == data_before, \
        "원본 parquet 가 수정됐다"
    _ = json


def test_analyze_preview_returns_segments_without_saving(client, sidecar_backup):
    """§3.5 파라미터 미리보기: 구간은 응답에, 사이드카는 그대로.

    요약만으로는 트랙을 못 그린다 — save=False + include_segments 가
    "저장 없이 비교"의 전부다.
    """
    from piper_phase import labeler as PL

    labels_path, _ = PL.sidecar_paths(sidecar_backup)
    before = labels_path.read_bytes() if labels_path.exists() else None

    r = client.post(f"/api/phase/{_DS}/analyze",
                    json={"episodes": [0], "save": False, "include_segments": True})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["episodes"], int)  # 요약 개수는 그대로 남는다
    segs = body["episode_labels"]["0"]["segments"]
    assert segs and segs[0][0] == 0 and len(segs[0]) == 3

    after = labels_path.read_bytes() if labels_path.exists() else None
    assert before == after, "미리보기가 사이드카를 건드렸다"


def test_analyze_without_flag_has_no_episodes_payload(client, sidecar_backup):
    r = client.post(f"/api/phase/{_DS}/analyze", json={"episodes": [0], "save": False})
    assert r.status_code == 200
    assert "episode_labels" not in r.json()
    assert isinstance(r.json()["episodes"], int)
