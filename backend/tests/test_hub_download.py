"""허브 다운로드 — **얼마나 왔는지 말하고, 다 왔는지 확인한다.**

이 파일이 지키는 것 둘:

1. 진행 상황이 **실제로 움직인다** (예전에는 `progress: 0` 이 끝까지 그대로였다).
2. `completed` 는 **세어 보고** 하는 말이다 — 예외가 없었다는 것과 파일이 다 있다는
   것은 다르다. 그 차이가 실기 사고였다(2026-09-23, `cam0` mp4 가 안 받아졌는데 완료).
"""

import threading
import time
from pathlib import Path

import pytest

from app.services import hub_download as D

REPO = "org/name"
FILES = {"meta/info.json": 100, "videos/a.mp4": 900}


@pytest.fixture(autouse=True)
def clean():
    D._status.clear()
    yield
    D._status.clear()


def _cache(tmp_path: Path, have: dict[str, int]) -> Path:
    """HF 캐시 모양 — 실체는 `blobs/`, 스냅샷은 링크."""
    root = tmp_path / "datasets--org--name"
    (root / "blobs").mkdir(parents=True)
    snap = root / "snapshots" / "abc"
    snap.mkdir(parents=True)
    for i, (name, size) in enumerate(have.items()):
        blob = root / "blobs" / f"sha{i}"
        blob.write_bytes(b"x" * size)
        link = snap / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(blob)
    return root


def _point_at(monkeypatch, root: Path):
    monkeypatch.setattr(D, "cache_root", lambda r, t: root)
    monkeypatch.setattr(D, "hub_files", lambda r, t: dict(FILES))


# ─────────────────────────────────────────────────────────────────────────────
# 다 왔는지는 **세어 본다**
# ─────────────────────────────────────────────────────────────────────────────

def test_a_repo_missing_a_file_is_not_complete(tmp_path, monkeypatch):
    """⚠ **이게 그 사고다.** `snapshot_download` 가 예외 없이 돌아와도 파일이 빠져
    있을 수 있다. 목록·프레임 수는 `meta/` 에서 읽으므로 멀쩡해 보이고, 사람은 몇 주 뒤
    영상이 안 나올 때 안다."""
    _point_at(monkeypatch, _cache(tmp_path, {"meta/info.json": 100}))
    assert D.verify(REPO, "dataset") == ["videos/a.mp4"]


def test_all_files_present_is_complete(tmp_path, monkeypatch):
    _point_at(monkeypatch, _cache(tmp_path, dict(FILES)))
    assert D.verify(REPO, "dataset") == []


def test_a_dangling_link_counts_as_missing(tmp_path, monkeypatch):
    """받다 만 자리 — 링크는 있는데 실체가 없다. 있다고 말하면 안 된다."""
    root = _cache(tmp_path, dict(FILES))
    _point_at(monkeypatch, root)
    (root / "blobs" / "sha1").unlink()
    assert D.verify(REPO, "dataset") == ["videos/a.mp4"]


def test_when_the_hub_cannot_be_asked_we_do_not_claim_missing(tmp_path, monkeypatch):
    """⚠ 모르는 것을 "빠졌다" 로 말하면, 멀쩡한 저장소를 다시 받게 만든다."""
    monkeypatch.setattr(D, "cache_root", lambda r, t: _cache(tmp_path, {}))
    monkeypatch.setattr(D, "hub_files", lambda r, t: {})
    assert D.verify(REPO, "dataset") == []


# ─────────────────────────────────────────────────────────────────────────────
# 진행 상황
# ─────────────────────────────────────────────────────────────────────────────

def test_bytes_count_what_is_still_arriving(tmp_path):
    """⚠ `.incomplete` 도 진행이다. 빼면 큰 파일 하나를 받는 동안 막대가 안 움직인다."""
    root = tmp_path / "r"
    (root / "blobs").mkdir(parents=True)
    (root / "blobs" / "sha0").write_bytes(b"x" * 100)
    (root / "blobs" / "sha1.abc.incomplete").write_bytes(b"x" * 400)
    assert D.blob_bytes(root) == 500


def test_the_bar_never_goes_backwards(tmp_path, monkeypatch):
    """⚠ `.incomplete` 가 최종 블롭이 되는 순간 `blobs/*` 합이 **줄어든다.** 그대로
    그리면 막대가 뒤로 간다."""
    _point_at(monkeypatch, _cache(tmp_path, dict(FILES)))
    seq = iter([500, 900, 300, 400])          # ← 가운데서 줄어든다
    monkeypatch.setattr(D, "blob_bytes", lambda root: next(seq, 400))
    monkeypatch.setattr(D, "SAMPLE_S", 0.01)

    seen: list[float] = []
    orig = D._set

    def spy(repo_id, **kw):
        if "percent" in kw:
            seen.append(kw["percent"])
        orig(repo_id, **kw)

    monkeypatch.setattr(D, "_set", spy)
    stop = threading.Event()
    t = threading.Thread(target=D._sampler, args=(REPO, "dataset", dict(FILES), stop))
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)
    assert len(seen) >= 3, seen
    assert seen == sorted(seen), f"퍼센트가 뒤로 갔다: {seen}"


def test_a_cache_hit_still_moves_the_file_count(tmp_path, monkeypatch):
    """⚠ 이미 있는 파일은 바이트가 안 는다 — 그것만 보면 멈춘 것처럼 보인다.
    파일 수가 같이 올라가야 사람이 오해하지 않는다."""
    root = _cache(tmp_path, dict(FILES))
    _point_at(monkeypatch, root)
    assert len(D.present(root, dict(FILES))) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 시작·중복
# ─────────────────────────────────────────────────────────────────────────────

def test_pressing_twice_does_not_start_two_downloads(monkeypatch):
    """⚠ 스레드가 둘 뜨면 같은 파일을 두 번 받고 진행률이 뒤엉킨다."""
    started: list[str] = []
    monkeypatch.setattr(D.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started.append(kw.get("name", ""))})())
    D.start(REPO, "dataset")
    D.start(REPO, "dataset")
    assert len(started) == 1, started


def test_the_status_shape_is_what_the_screen_reads():
    """⚠ 화면이 끝을 판정하는 값과 서버가 내보내는 값이 갈리면, 기다리는 척만 하는
    폴링이 된다 — 실제로 `TrainingPage` 가 그랬다(`running`/`started` 를 봤다)."""
    from conftest import code_only

    # ⚠ 주석을 걷어내고 본다 — **왜 안 쓰는지 적어둔 설명**이 검사에 걸리면 안 된다.
    hook = code_only((Path(__file__).resolve().parents[2]
                      / "frontend/src/hooks/useHubDownload.ts").read_text())
    for st in ("idle", "downloading", "verifying", "completed", "incomplete", "error"):
        assert f"'{st}'" in hook, f"화면이 {st} 를 모른다"
    assert "'running'" not in hook and "'started'" not in hook, \
        "서버가 안 내보내는 상태를 화면이 본다"
