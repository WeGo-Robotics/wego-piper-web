"""끊긴 녹화가 남긴 반쪽 데이터셋 — 진단과 색인 복구.

LeRobot 은 에피소드 메타를 **10개씩 모아서** parquet 에 쓰고(`metadata_buffer_size`),
`info.json` 의 개수는 **에피소드마다** 쓴다. 정상 종료면 `_close_writer()` 가
버퍼를 비우지만 SIGKILL 이면 그 호출이 없다.

그래서 화면이 이렇게 된다: **개수는 뜨는데 각 에피소드에 못 들어간다.**
개수는 `info.json`, 목록은 parquet 에서 오기 때문이다. 실제로 그 상태를 보고
복구 가능한 데이터셋을 지우고 다시 찍었다.
"""

import json
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from app.services import dataset_repair as R  # noqa: E402

FPS = 15
LENGTHS = [10, 12, 8, 15, 9]        # 에피소드 5개


def _make(tmp: Path, *, indexed: int) -> Path:
    """에피소드 5개짜리 데이터셋. `indexed` 개만 색인에 들어 있다 (나머지는 버퍼째 증발)."""
    ds = tmp / "ds"
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (ds / "data" / "chunk-000").mkdir(parents=True)

    (ds / "meta" / "info.json").write_text(json.dumps({
        "total_episodes": len(LENGTHS),          # ⚠ 전부 세어져 있다
        "total_frames": sum(LENGTHS),
        "fps": FPS,
        "features": {"observation.images.top": {"dtype": "video"},
                     "action": {"dtype": "float32"}},
    }))
    pd.DataFrame({"task": ["pick it up"]}).to_parquet(ds / "meta" / "tasks.parquet")

    # data — **모든** 에피소드의 프레임이 남아 있다
    rows = []
    for ep, n in enumerate(LENGTHS):
        rows += [{"episode_index": ep, "task_index": 0, "x": float(i)} for i in range(n)]
    pd.DataFrame(rows).to_parquet(ds / "data" / "chunk-000" / "file-000.parquet", index=False)

    # meta/episodes — 앞의 `indexed` 개만
    cursor, recs = 0.0, []
    start = 0
    for ep, n in enumerate(LENGTHS):
        if ep < indexed:
            recs.append({
                "episode_index": ep, "tasks": ["pick it up"], "length": n,
                "data/chunk_index": 0, "data/file_index": 0,
                "dataset_from_index": start, "dataset_to_index": start + n,
                "videos/observation.images.top/chunk_index": 0,
                "videos/observation.images.top/file_index": 0,
                "videos/observation.images.top/from_timestamp": cursor,
                "videos/observation.images.top/to_timestamp": cursor + n / FPS,
            })
        cursor += n / FPS
        start += n
    pd.DataFrame(recs).to_parquet(
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet", index=False)
    return ds


def test_a_healthy_dataset_reports_ok(tmp_path):
    st = R.check(_make(tmp_path, indexed=len(LENGTHS)))
    assert st["ok"] and not st["recoverable"]


def test_the_gap_between_the_count_and_the_list_is_reported(tmp_path):
    """⚠ **조용히 반쪽만 보여주는 것이 제일 나쁘다.**

    사용자는 데이터가 없어진 줄 알고 지운다 — 실제로 그렇게 지웠다.
    """
    st = R.check(_make(tmp_path, indexed=2))
    assert st["ok"] is False
    assert st["declared_episodes"] == 5      # 화면이 보여주던 숫자
    assert st["indexed_episodes"] == 2       # 목록에 실제로 보이던 것
    assert st["stored_episodes"] == 5        # 프레임은 다 있다
    assert st["recoverable"] == [2, 3, 4]


def test_frames_that_are_gone_are_not_promised_back(tmp_path):
    """프레임까지 없으면 되살릴 수 없다. 숫자만 큰 경우를 구분해야 한다 —
    복구된다고 말해놓고 못 하면 그게 더 나쁘다."""
    ds = _make(tmp_path, indexed=2)
    info = json.loads((ds / "meta" / "info.json").read_text())
    info["total_episodes"] = 8               # data 에는 5개뿐
    (ds / "meta" / "info.json").write_text(json.dumps(info))
    assert R.check(ds)["unrecoverable"] == 3


def test_rebuild_restores_the_missing_episodes(tmp_path):
    ds = _make(tmp_path, indexed=2)
    out = R.rebuild_index(ds, dry_run=False)
    assert out["ok"] and out["restored"] == [2, 3, 4]
    assert R.check(ds)["ok"] is True


def test_rebuilt_rows_carry_the_right_frame_ranges(tmp_path):
    """길이와 행 범위가 틀리면 뷰어가 엉뚱한 프레임을 보여준다."""
    ds = _make(tmp_path, indexed=2)
    R.rebuild_index(ds, dry_run=False)
    df = pd.read_parquet(ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    df = df.sort_values("episode_index").reset_index(drop=True)
    assert df["length"].tolist() == LENGTHS
    start = 0
    for ep, n in enumerate(LENGTHS):
        assert df.loc[ep, "dataset_from_index"] == start
        assert df.loc[ep, "dataset_to_index"] == start + n
        start += n


def test_video_timestamps_continue_from_the_last_good_episode(tmp_path):
    """⚠ 영상은 한 파일에 **연속으로** 붙는다. 이어붙이지 않으면 복구된
    에피소드가 영상의 엉뚱한 지점을 가리킨다 — 화면은 뜨는데 딴 장면이 나온다."""
    ds = _make(tmp_path, indexed=2)
    R.rebuild_index(ds, dry_run=False)
    df = pd.read_parquet(ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    df = df.sort_values("episode_index").reset_index(drop=True)
    k = "videos/observation.images.top"
    cursor = 0.0
    for ep, n in enumerate(LENGTHS):
        assert df.loc[ep, f"{k}/from_timestamp"] == pytest.approx(cursor)
        cursor += n / FPS
        assert df.loc[ep, f"{k}/to_timestamp"] == pytest.approx(cursor)


def test_existing_rows_are_never_rewritten(tmp_path):
    """⚠ 멀쩡한 부분을 다시 쓰다가 틀리면 되살리는 도구가 **망가뜨리는 도구**가 된다."""
    ds = _make(tmp_path, indexed=2)
    p = ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    before = pd.read_parquet(p).sort_values("episode_index").head(2).to_dict("records")
    R.rebuild_index(ds, dry_run=False)
    after = pd.read_parquet(p).sort_values("episode_index").head(2).to_dict("records")
    for b, a in zip(before, after):
        for key in b:
            assert a[key] == b[key], f"기존 행이 바뀌었다: {key}"


def test_a_backup_is_left_behind(tmp_path):
    """복구가 틀렸을 때 되돌릴 것이 있어야 한다."""
    ds = _make(tmp_path, indexed=2)
    out = R.rebuild_index(ds, dry_run=False)
    assert Path(out["backup"]).is_file()


def test_dry_run_changes_nothing(tmp_path):
    """무엇이 되살아나는지 먼저 보여주고 나서 쓴다."""
    ds = _make(tmp_path, indexed=2)
    p = ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    before = p.read_bytes()
    out = R.rebuild_index(ds, dry_run=True)
    assert out["restored"] == [2, 3, 4] and p.read_bytes() == before


# ── 화면까지 닿는가 ─────────────────────────────────────────────────────────

_PAGE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "DatasetsPage.tsx"
_ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "datasets.py"


def test_the_screen_asks_for_the_verdict_with_the_detail():
    """⚠ 진단이 있어도 **화면이 안 물으면** 아무것도 안 바뀐다.

    목록만 보여주면 "개수는 50인데 목록에 2개" 가 조용히 지나간다.
    """
    src = _PAGE.read_text()
    assert "/consistency" in src, "화면이 정합성을 안 묻는다"
    assert "handleRepair" in src, "복구 경로가 화면에 없다"


def test_the_warning_sits_above_the_list():
    """목록이 짧은 **이유**를 모른 채 스크롤하면 데이터가 없어진 줄 안다."""
    src = _PAGE.read_text()
    assert src.index("health && !health.ok") < src.index("{/* 에피소드 목록 */}")


def test_repair_defaults_to_preview():
    """남의 데이터를 고치는 일이다 — 무엇이 되살아나는지 먼저 보여준다."""
    src = _ROUTER.read_text()
    body = src.split("async def dataset_repair_index", 1)[1].split("\n@router", 1)[0]
    assert "apply: bool = False" in src.split("dataset_repair_index", 1)[1][:200], \
        "기본이 즉시 쓰기다"
    assert "dry_run=not apply" in body


def test_writing_is_exclusive_with_dataset_edit():
    """쓰는 동안 편집이 같은 파일을 만지면 둘 다 깨진다."""
    src = _ROUTER.read_text()
    body = src.split("async def dataset_repair_index", 1)[1].split("\n@router", 1)[0]
    assert "require_idle(Activity.DATASET_EDIT)" in body
    assert "if apply:" in body, "미리보기에도 가드를 걸면 진단조차 막힌다"


def test_the_repair_routes_come_before_the_catch_all():
    """⚠ 같은 메서드의 `:path` catch-all 이 먼저 등록되면 이 경로들이 통째로
    상세 응답으로 먹힌다 (`/upload-status` 가 실제로 겪은 사고)."""
    src = _ROUTER.read_text()
    assert src.index('/{dataset_id:path}/consistency') < src.index('@router.get("/{dataset_id:path}")')
