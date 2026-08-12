"""HF 캐시 레이아웃 해석 (refactor/11-hf-cache-layout.md).

원래 문제: `_latest_snapshot` 이 두 스캐너에 **주석 빼면 완전히 동일**하게 있었고,
`info.json` 위치 규칙은 세 곳에 적혀 있었는데 **한 곳만 폴백이 없었다** —
평평한 `info.json` 데이터셋이 목록엔 안 보이는데 상세 조회로는 열렸다.
"""

from app.core.hf_layout import (
    dirname_from_repo_id,
    latest_snapshot,
    repo_id_from_dirname,
    repo_root_for_delete,
    resolve_info_json,
)


def test_repo_id_roundtrip():
    for kind in ("models", "datasets"):
        dirname = dirname_from_repo_id("wego-hansu/piper_smolvla", kind)
        assert dirname == f"{kind}--wego-hansu--piper_smolvla"
        assert repo_id_from_dirname(dirname, kind) == "wego-hansu/piper_smolvla"


def test_repo_id_rejects_wrong_kind():
    """접두사를 안 보면 모델 폴더가 데이터셋으로 잡힌다."""
    assert repo_id_from_dirname("models--org--name", "datasets") is None
    assert repo_id_from_dirname("datasets--org--name", "models") is None
    assert repo_id_from_dirname("random_folder", "models") is None


def test_name_with_hyphens_survives():
    """`split("--", 2)` 라 이름 안의 `-` 는 살아야 한다."""
    assert repo_id_from_dirname("models--wego-hansu--min_cube-071410", "models") == (
        "wego-hansu/min_cube-071410"
    )


def test_latest_snapshot_picks_newest(tmp_path):
    import os
    import time
    snaps = tmp_path / "snapshots"
    (snaps / "old").mkdir(parents=True)
    (snaps / "new").mkdir()
    old_t = time.time() - 1000
    os.utime(snaps / "old", (old_t, old_t))
    assert latest_snapshot(tmp_path).name == "new"


def test_latest_snapshot_missing(tmp_path):
    assert latest_snapshot(tmp_path) is None
    (tmp_path / "snapshots").mkdir()
    assert latest_snapshot(tmp_path) is None  # 비어 있음


def test_resolve_info_json_prefers_meta(tmp_path):
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "info.json").write_text("{}")
    (tmp_path / "info.json").write_text("{}")
    assert resolve_info_json(tmp_path) == tmp_path / "meta" / "info.json"


def test_resolve_info_json_falls_back(tmp_path):
    """폴백이 한 곳에만 있어야 목록과 상세가 같은 데이터셋을 본다."""
    (tmp_path / "info.json").write_text("{}")
    assert resolve_info_json(tmp_path) == tmp_path / "info.json"


def test_resolve_info_json_missing(tmp_path):
    assert resolve_info_json(tmp_path) is None


def test_repo_root_for_delete(tmp_path):
    """**잘못된 폴더를 지울 수 있는 경로**라 규칙이 흩어지면 특히 위험하다."""
    repo = tmp_path / "datasets--org--name"
    snap = repo / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    assert repo_root_for_delete(snap, "datasets") == repo
    # Hub 형식이 아니면 자기 자신
    plain = tmp_path / "org" / "name"
    plain.mkdir(parents=True)
    assert repo_root_for_delete(plain, "datasets") == plain


def test_scanners_use_the_shared_module():
    """쌍둥이 헬퍼가 되살아나지 않게 한다."""
    from pathlib import Path

    for name in ("dataset_scanner", "model_scanner"):
        src = (Path(__file__).resolve().parents[1] / "app" / "services" / f"{name}.py").read_text()
        assert "def _latest_snapshot" not in src, f"{name} 에 사본이 다시 생겼다"
        assert "def _repo_id_from_dirname" not in src, f"{name} 에 사본이 다시 생겼다"
        assert "hf_layout" in src


# ── repo_id 형식 ─────────────────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.core.hf_layout import repo_id_error  # noqa: E402


@pytest.mark.parametrize("value", [
    "wego/piper_demo", "wego-hansu/2-CAM", "org/pick_v1.0", "a/b",
])
def test_valid_repo_ids(value):
    assert repo_id_error(value) is None


@pytest.mark.parametrize("value,hint", [
    ("2-CAM", "네임스페이스"),        # **실기에서 실제로 터진 입력**
    ("", "필요"),
    ("a/b/c", "슬래시는 하나"),
    ("wego/한글", "쓸 수 없는 문자"),  # `\w` 는 유니코드를 먹으므로 ASCII 로 못 박았다
    ("-bad/x", "쓸 수 없는 문자"),
    ("wego/", "쓸 수 없는 문자"),
])
def test_invalid_repo_ids(value, hint):
    err = repo_id_error(value)
    assert err and hint in err, f"{value!r} → {err!r}"


def test_recording_start_rejects_bad_repo_id_before_touching_hardware():
    """**회귀** — LeRobot 은 `repo_id.split("/")` 를 2개로 언패킹한다.

    슬래시가 없으면 녹화 프로세스가 뜬 **뒤** ValueError 로 죽는데, 그 시점엔 이미
    팔·카메라를 다 잡은 상태라 스택트레이스만 남고 이유를 알기 어렵다.
    라우터가 시작 전에 막아야 한다.
    """
    src = (Path(__file__).resolve().parents[2]
           / "backend" / "app" / "routers" / "recording.py").read_text()
    start = src.split('@router.post("/start")', 1)[1].split("@router.", 1)[0]
    assert "repo_id_error" in start, "녹화 시작 경로가 repo_id 형식을 검사하지 않는다"
    assert start.index("repo_id_error") < start.index("record_manager.start"), (
        "프로세스를 띄운 뒤에 검사하면 늦다"
    )
