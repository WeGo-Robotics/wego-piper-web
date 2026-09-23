"""허브 다운로드 — **얼마나 왔는지 말하고, 다 왔는지 확인한다.**

## ⚠ 예전에는 둘 다 안 했다

`{"status":"downloading","progress":0}` 을 넣고 다시는 안 건드렸다. 화면은 "다운로드
중..." 을 영원히 띄웠고(지우는 곳이 없었다), 끝나면 파일을 세어 보지도 않고 `completed`
라고 했다.

⚠ **두 번째가 실제로 사고를 냈다**(2026-09-23). `wego-mink/sim_two_box_3_120` 의 `cam0`
mp4 가 안 받아졌는데(`.incomplete` 0바이트) 상태는 완료였다. 에피소드 수·프레임 수는
`meta/` 에서 읽으므로 목록은 멀쩡해 보였고, **몇 주 뒤 영상이 안 나오는 것**으로야
드러났다. 다 왔다고 말할 거면 세어 보고 말해야 한다.

## ⚠ 진행률은 **디스크에서** 읽는다 — 라이브러리 내부가 아니라

`snapshot_download(tqdm_class=...)` 로 가로채 봤다(huggingface_hub 1.22.0, 7파일 실측):

```
total=7  n=7   "Fetching 7 files"                  ← 파일 개수는 온다
total=0  n=0   "Downloading (incomplete total...)" ← 바이트는 안 온다
```

바이트를 못 얻는 것도 문제지만, 더 나쁜 것은 **저 문자열에 기대는 것**이다 —
라이브러리가 문구를 바꾸면 조용히 0% 가 된다. 그래서 tqdm 을 안 쓰고 캐시 디렉토리를
직접 본다:

| 값 | 출처 |
|---|---|
| `bytes_total` | `repo_info(files_metadata=True)` 의 `size` 합 — **받기 전에** 안다 |
| `bytes_done` | 그 repo 캐시 `blobs/*` 크기 합 (받는 중인 `.incomplete` 포함) |
| `files_done` | Hub 파일 중 스냅샷에 **실제로 존재하는** 것의 수 |

`files_done` 을 이렇게 세면 완료 판정(§`verify`)과 **같은 자**를 쓰게 된다 — 진행률은
차 있는데 완료는 아니라는 모순이 구조적으로 안 생긴다.

## ⚠ 퍼센트는 뒤로 안 간다

`.incomplete` 가 최종 블롭으로 바뀌는 순간 `blobs/*` 합이 **줄어든다**. 그대로 그리면
막대가 뒤로 간다. 단조 증가로 잠근다.

## ⚠ 캐시 히트는 "멈춘 것" 이 아니다

이미 있는 파일은 다시 안 받으므로 바이트가 안 는다. `files_done` 이 같이 오르므로 둘을
같이 내보낸다 — 화면이 "4/9" 를 보여 주면 사람이 멈췄다고 오해하지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 표본 간격. 화면 폴링(1초)보다 촘촘해야 속도가 튀지 않는다.
SAMPLE_S = 0.5

#: 속도·ETA 를 낼 때 보는 창. 짧으면 숫자가 요동치고, 길면 멈춘 것을 늦게 안다.
WINDOW_S = 5.0

_status: dict[str, dict] = {}
_lock = threading.Lock()


def _blank(repo_id: str, repo_type: str) -> dict:
    return {"status": "idle", "repo_id": repo_id, "repo_type": repo_type,
            "files_done": 0, "files_total": 0,
            "bytes_done": 0, "bytes_total": 0, "percent": 0.0,
            "speed_bps": 0.0, "eta_s": None, "started_at": None,
            "path": None, "missing": [], "error": None}


def _set(repo_id: str, **kw) -> None:
    with _lock:
        cur = _status.setdefault(repo_id, _blank(repo_id, kw.get("repo_type", "model")))
        cur.update(kw)


def status(repo_id: str) -> dict:
    with _lock:
        cur = _status.get(repo_id)
        return dict(cur) if cur else {"status": "idle", "repo_id": repo_id}


def busy(repo_id: str) -> bool:
    return status(repo_id).get("status") in ("downloading", "verifying")


def cache_root(repo_id: str, repo_type: str) -> Path:
    """그 repo 의 캐시 디렉토리 (`datasets--org--name`).

    ⚠ 이름 규칙을 손으로 만들지 않는다 — `repo_folder_name` 이 정답을 안다.
    """
    from huggingface_hub.constants import HF_HUB_CACHE
    from huggingface_hub.file_download import repo_folder_name

    return Path(HF_HUB_CACHE) / repo_folder_name(repo_id=repo_id, repo_type=repo_type)


def hub_files(repo_id: str, repo_type: str) -> dict[str, int]:
    """Hub 가 말하는 `{파일: 크기}`. 못 물어보면 빈 dict — 그래도 받기는 한다."""
    from app.services.hub_client import _api

    try:
        info = _api.repo_info(repo_id=repo_id, repo_type=repo_type, files_metadata=True)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("Hub 파일 목록을 못 읽었습니다(%s): %s", repo_id, exc)
        return {}
    return {s.rfilename: (s.size or 0) for s in (info.siblings or [])}


def blob_bytes(root: Path) -> int:
    """받은 바이트 — **`.incomplete` 를 포함한다.** 받는 중인 것도 진행이다."""
    blobs = root / "blobs"
    if not blobs.is_dir():
        return 0
    total = 0
    for f in blobs.iterdir():
        try:
            total += f.stat().st_size
        except OSError:                 # 지금 이름이 바뀌는 중일 수 있다
            continue
    return total


def snapshot_dir(root: Path) -> Path | None:
    """받는 중에도 생긴다. 여럿이면 가장 최근 것."""
    snaps = [p for p in (root / "snapshots").glob("*") if p.is_dir()] \
        if (root / "snapshots").is_dir() else []
    return max(snaps, key=lambda p: p.stat().st_mtime) if snaps else None


def present(root: Path, wanted: dict[str, int]) -> list[str]:
    """Hub 파일 중 **실제로 있는** 것. 끊긴 링크는 없는 것으로 친다.

    ⚠ HF 캐시의 스냅샷은 `blobs/` 로의 심볼릭이다 — `exists()` 는 링크를 따라가므로
    링크만 남고 실체가 없는(받다 만) 경우를 그대로 걸러 준다.
    """
    snap = snapshot_dir(root)
    if snap is None:
        return []
    return [name for name in wanted if (snap / name).exists()]


def verify(repo_id: str, repo_type: str, wanted: dict[str, int] | None = None) -> list[str]:
    """빠진 파일 목록. 비어 있으면 다 왔다.

    ⚠ **이것이 완료 판정이다.** `snapshot_download` 가 예외 없이 돌아온 것과 파일이 다
    있는 것은 다르다 — 모듈 머리말의 그 사고다.
    """
    wanted = hub_files(repo_id, repo_type) if wanted is None else wanted
    if not wanted:
        return []          # 물어보지 못했으면 없다고 단정하지 않는다
    root = cache_root(repo_id, repo_type)
    have = set(present(root, wanted))
    return sorted(name for name in wanted if name not in have)


def _sampler(repo_id: str, repo_type: str, wanted: dict[str, int], stop: threading.Event) -> None:
    """받는 동안 디스크를 훑어 상태를 갱신한다. **실패해도 다운로드를 안 막는다.**"""
    root = cache_root(repo_id, repo_type)
    total = sum(wanted.values())
    peak = 0.0
    hist: list[tuple[float, int]] = []
    while not stop.wait(SAMPLE_S):
        try:
            now = time.monotonic()
            done = blob_bytes(root)
            have = len(present(root, wanted))
            hist.append((now, done))
            hist[:] = [(t, b) for t, b in hist if now - t <= WINDOW_S] or hist[-1:]
            speed = 0.0
            if len(hist) >= 2 and hist[-1][0] > hist[0][0]:
                speed = max(0.0, (hist[-1][1] - hist[0][1]) / (hist[-1][0] - hist[0][0]))
            # ⚠ 단조 증가로 잠근다 — `.incomplete` 가 최종 블롭이 되는 순간 합이 줄어
            #   막대가 뒤로 간다.
            pct = peak if not total else max(peak, min(100.0, done * 100.0 / total))
            peak = pct
            left = max(0, total - done)
            _set(repo_id, bytes_done=done, bytes_total=total, percent=round(pct, 1),
                 files_done=have, files_total=len(wanted), speed_bps=round(speed, 1),
                 eta_s=int(left / speed) if speed > 1 and left else None)
        except Exception as exc:                                    # noqa: BLE001
            logger.debug("진행률 표본 실패(계속): %s", exc)


def _run(repo_id: str, repo_type: str) -> None:
    from huggingface_hub import snapshot_download

    wanted = hub_files(repo_id, repo_type)
    _set(repo_id, status="downloading", repo_type=repo_type, error=None, missing=[],
         started_at=time.time(), files_total=len(wanted),
         bytes_total=sum(wanted.values()), percent=0.0, bytes_done=0, files_done=0)

    stop = threading.Event()
    t = threading.Thread(target=_sampler, args=(repo_id, repo_type, wanted, stop),
                         name=f"hubdl-{repo_id}", daemon=True)
    t.start()
    try:
        path = snapshot_download(repo_id=repo_id, repo_type=repo_type)
    except Exception as exc:                                        # noqa: BLE001
        logger.error("다운로드 실패(%s): %s", repo_id, exc)
        _set(repo_id, status="error", error=str(exc)[:300])
        return
    finally:
        stop.set()
        t.join(timeout=2)

    # ⚠ **여기서 세어 본다.** 예외가 없었다는 것과 파일이 다 있다는 것은 다르다.
    _set(repo_id, status="verifying", path=path)
    gone = verify(repo_id, repo_type, wanted)
    have = len(wanted) - len(gone)
    if gone:
        logger.warning("받다 만 저장소(%s): %d개 빠짐 — %s", repo_id, len(gone), gone[:3])
        _set(repo_id, status="incomplete", missing=gone, files_done=have,
             files_total=len(wanted))
        return
    # ⚠ 다 왔으면 숫자도 맞춰 둔다. 캐시 히트만 있으면 전송한 바이트가 0 이라
    #   `0/18897020 bytes · 100%` 처럼 **버그로 읽히는 화면**이 된다. 막대가 내내
    #   재던 것은 "디스크에 이 repo 가 몇 바이트 있나" 이므로 그 값을 그대로 쓴다.
    _set(repo_id, status="completed", missing=[], percent=100.0,
         bytes_done=blob_bytes(cache_root(repo_id, repo_type)),
         files_done=have, files_total=len(wanted), speed_bps=0.0, eta_s=0)


def start(repo_id: str, repo_type: str = "model") -> dict:
    """백그라운드로 받기 시작. **이미 받는 중이면 새로 안 띄운다.**

    ⚠ 두 번 누르면 스레드가 둘 떠서 같은 파일을 두 번 받고 진행률이 뒤엉킨다.
    """
    if busy(repo_id):
        return status(repo_id)
    # ⚠ 지난 회차의 `missing`·`error` 를 지우고 시작한다 — 남겨 두면 새 다운로드가
    #   시작부터 "빠진 파일 N개" 를 달고 있다.
    with _lock:
        _status[repo_id] = _blank(repo_id, repo_type)
    _set(repo_id, status="downloading", started_at=time.time())
    threading.Thread(target=_run, args=(repo_id, repo_type),
                     name=f"hubdl-run-{repo_id}", daemon=True).start()
    return status(repo_id)
