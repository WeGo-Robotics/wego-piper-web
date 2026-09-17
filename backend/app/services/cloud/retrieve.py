"""회수 — 학습이 끝난 가중치를 **집으로 가져온다** (feature/vast-training.md §5·§12-4).

경로는 둘이고, **언제 도느냐가 다르다.** 그게 이 파일이 따로 있는 이유다.

```
학습 끝
  │
  ├ Hub 에 가중치가 없다 ─→ 기계가 살아 있는 동안 `scp` (rescue.py)  ← 파기 전에!
  │
  └ Hub 에 가중치가 있다 ─→ 기계를 파기한 뒤 Hub 에서 받는다         ← 파기 후에!
```

## ⚠ Hub 다운로드를 파기 **뒤에** 하는 이유

`scp` 는 기계가 있어야 하지만 **Hub 는 기계를 안 탄다.** 파기 전에 받으면 200MB 를
내려받는 동안 빌린 GPU 요금을 그대로 낸다 — 4090 $0.4/h 에서 2분이면 $0.013 이고,
무엇보다 **파기가 그만큼 늦어진다.** 이 기능 전체에서 가장 중요한 불변식이 "반드시
파기한다" 인데, 그걸 다운로드 속도에 묶을 이유가 없다.

## ⚠ 받아야 끝난 것이다

푸시가 성공하면 예전에는 여기서 끝이었다 — 그리고 사람이 저장소 페이지에서 [다운로드]를
눌러야 로컬에 왔다. 학습을 걸어 두고 자리를 뜬 사람 입장에서는 **다 됐는데 아직 못 쓰는**
상태이고, 그 클릭을 잊으면 다음 날 추론을 돌리려다 그제서야 안다.

## ⚠ 받는 자리는 `models_dir` 다 — HF 기본 캐시가 아니라

`snapshot_download` 는 기본값으로 `HF_HUB_CACHE` 에 넣는다. 우리 스캐너가 보는 곳은
`settings.models_dir` 이고, 이 기계에서는 우연히 같지만 `PIPER_MODELS_DIR` 로 옮기면
갈라진다 — 파일은 받았는데 **화면에 안 뜨는** 상태가 된다. 그래서 `cache_dir` 을
명시한다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Hub 에서 받는 데 쓸 상한. 실측 가중치는 198MB 라 보통 몇십 초다.
#:
#: ⚠ 기계는 이미 파기된 뒤라 **요금은 안 나간다.** 이 상한이 지키는 것은 돈이 아니라
#: **다음 임대**다 — 다운로드가 매달리면 `busy()` 가 계속 참이라 다음 학습을 못 건다.
PULL_TIMEOUT_S = 900.0

#: "가중치가 왔다" 의 판정 파일. `config.json` 만 보면 빈 껍데기를 성공이라 읽는다.
WANTED_FILE = "model.safetensors"

#: 지금 회수가 어디까지 왔나. 화면이 이걸 받아 간다.
#:
#: - `idle`     아직 할 일이 없다
#: - `checking` Hub 에 올라갔는지 보는 중
#: - `rescuing` 기계에서 직접 끌어오는 중 (Hub 에 없다)
#: - `waiting`  Hub 에 있다 — **기계를 파기한 뒤** 받는다
#: - `pulling`  Hub 에서 받는 중
#: - `done`     로컬에 있다 (`path` 가 그 자리)
#: - `missing`  받을 것이 없다 — 체크포인트를 남기기 전에 죽었다
#: - `failed`   받으려다 실패했다 (`detail` 에 사유)
_state: dict = {"state": "idle", "repo_id": "", "path": "", "source": "", "detail": ""}


def status() -> dict:
    return dict(_state)


def _set(state: str, **kw) -> None:
    _state["state"] = state
    _state.update(kw)


def reset(repo_id: str = "") -> None:
    _state.update({"state": "idle", "repo_id": repo_id, "path": "", "source": "",
                   "detail": ""})


def cache_name(repo_id: str) -> str:
    """`org/name` → `models--org--name`. 스캐너가 읽는 이름 모양이다."""
    org, _, name = repo_id.partition("/")
    return f"models--{org}--{name}"


def local_snapshot(repo_id: str, models_dir: Path) -> Path | None:
    """이미 집에 와 있나. **가중치 파일까지 본다.**

    ⚠ 디렉토리만 보고 판정하면 안 된다 — 받다 만 자리, `config.json` 만 있는 자리,
    `.incomplete` 만 남은 자리가 전부 "있음" 으로 읽힌다.
    """
    root = Path(models_dir) / cache_name(repo_id) / "snapshots"
    if not root.is_dir():
        return None
    for snap in sorted(root.iterdir()):
        if (snap / WANTED_FILE).exists():
            return snap
    return None


def pushed(repo_id: str, since: float = 0.0) -> bool:
    """**이번 회차의** 가중치가 Hub 에 올라갔나.

    ⚠ 파일 목록을 본다 — repo 가 존재하는 것과 가중치가 올라간 것은 다르다. lerobot 이
    `initial commit` 으로 repo 만 만들고 죽을 수 있다(실측: 푸시는 커밋 넷으로 나뉜다).

    ⚠ **그리고 언제 올라갔는지도 본다.** 같은 `repo_id` 로 두 번째를 돌렸는데 이번
    회차가 중간에 죽으면, 거기 있는 것은 **지난번 가중치**다. 파일만 보면 "올라갔다" 가
    되어 `scp` 보험을 건너뛰고 — 이번 결과를 영영 잃은 채 **지난번 것을 받아 놓고
    성공이라 말한다.** 그게 이 경로에서 제일 나쁜 거짓말이다.
    """
    from datetime import datetime, timezone

    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id)
        files = {s.rfilename for s in (info.siblings or [])}
        touched = getattr(info, "last_modified", None)
    except Exception as exc:                                        # noqa: BLE001
        # ⚠ 모르면 **받는 쪽**으로 기운다. 전송비는 몇 센트지만 잃은 학습은 몇 시간이다.
        logger.warning("Hub 확인 실패(회수 쪽으로 진행): %s", exc)
        return False
    if WANTED_FILE not in files:
        return False
    if not since:
        return True
    if not isinstance(touched, datetime):
        # 시각을 모르면 이번 것인지 알 수 없다 — 역시 받는 쪽으로 기운다.
        logger.warning("Hub 갱신 시각을 모릅니다(회수 쪽으로 진행): %s", repo_id)
        return False
    # ⚠ tz 가 없으면 `timestamp()` 가 **로컬 시각으로 읽는다** — KST 에서는 방금 올린
    #   것이 9시간 전 것으로 둔갑한다. HF 는 UTC 를 주지만, 없을 때 UTC 로 보는 쪽이
    #   맞다(그 편이 틀려도 `scp` 로 기운다).
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=timezone.utc)
    fresh = touched.timestamp() >= since
    if not fresh:
        logger.warning("Hub 의 가중치가 이번 학습보다 오래됐습니다 — 지난 회차 것입니다: "
                       "%s (%s)", repo_id, touched.astimezone(timezone.utc).isoformat())
    return fresh


def pull(repo_id: str, models_dir: Path) -> Path:
    """Hub 에서 받는다. 받은 자리를 돌려준다. (스레드에서 부른다)

    ⚠ `cache_dir` 을 넘기는 이유는 모듈 머리말에 있다 — 기본 캐시에 받으면 스캐너가
    못 본다.
    """
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=repo_id, repo_type="model",
                                  cache_dir=str(models_dir)))


async def before_destroy(target, repo_id: str, models_dir: Path, *,
                         since: float = 0.0, rescue_fn=None) -> None:
    """**기계가 살아 있는 동안** 해야 할 몫. 여기서 판정도 한다.

    ⚠ 예외를 올리지 않는다. 회수 실패가 파기를 막으면 본전도 못 찾는다 — 기계가 계속
    돌면서 요금이 나간다.
    """
    reset(repo_id)
    if not repo_id:
        _set("idle", detail="저장소를 안 정했습니다")
        return

    _set("checking", repo_id=repo_id)
    if await asyncio.to_thread(pushed, repo_id, since):
        # ⚠ 여기서 받지 않는다. 기계를 끄고 나서 받는다(모듈 머리말).
        _set("waiting", source="hub",
             detail="Hub 에 있습니다 — 기계를 파기한 뒤 받습니다")
        logger.info("Hub 에 가중치가 있습니다 — 파기 뒤에 받습니다: %s", repo_id)
        return

    logger.warning("Hub 에 가중치가 없습니다 — 파기 전에 직접 끌어옵니다: %s", repo_id)
    _set("rescuing", source="scp")
    if rescue_fn is None:
        from app.services.cloud.rescue import rescue

        rescue_fn = rescue

    try:
        path = await asyncio.to_thread(rescue_fn, target, repo_id, models_dir)
    except Exception as exc:                                        # noqa: BLE001
        logger.error("가중치 회수 실패(파기는 계속): %s", exc)
        _set("failed", detail=str(exc)[:200])
        return
    if path is None:
        _set("missing", detail="원격에 체크포인트가 없습니다")
        return
    _set("done", path=str(path))


async def after_destroy(repo_id: str, models_dir: Path, *,
                        timeout: float = PULL_TIMEOUT_S) -> None:
    """**기계를 끈 뒤** 해야 할 몫 — Hub 에서 받아 온다.

    ⚠ `before_destroy` 가 `waiting` 을 남겼을 때만 돈다. `scp` 로 이미 받았으면 또
    받을 이유가 없고(전송비 두 번), 받을 것이 없으면 받을 것이 없다.
    """
    if _state["state"] != "waiting" or not repo_id:
        return

    here = await asyncio.to_thread(local_snapshot, repo_id, models_dir)
    if here is not None:
        # 같은 저장소로 다시 돌린 경우다. `snapshot_download` 도 캐시를 쓰지만,
        # 여기서 끊으면 네트워크를 아예 안 탄다.
        _set("done", path=str(here), source="local", detail="이미 로컬에 있습니다")
        logger.info("가중치가 이미 로컬에 있습니다: %s", here)
        return

    _set("pulling", detail="Hub 에서 받는 중입니다")
    logger.info("가중치를 받습니다: %s → %s", repo_id, models_dir)
    try:
        path = await asyncio.wait_for(
            asyncio.to_thread(pull, repo_id, models_dir), timeout=timeout)
    except asyncio.TimeoutError:
        # ⚠ 실패해도 잃은 것은 없다 — Hub 에 그대로 있다. 사람이 저장소 페이지에서
        #   받으면 된다. 그래서 여기서 예외를 올리지 않고 상태로 남긴다.
        _set("failed", detail=f"{timeout / 60:.0f}분 안에 못 받았습니다 — "
                              f"저장소 페이지에서 직접 받을 수 있습니다")
        logger.error("가중치 다운로드 시간 초과: %s", repo_id)
        return
    except Exception as exc:                                        # noqa: BLE001
        _set("failed", detail=f"{str(exc)[:200]} — 저장소 페이지에서 직접 받을 수 있습니다")
        logger.error("가중치 다운로드 실패: %s", exc)
        return

    _set("done", path=str(path), detail="")
    logger.info("가중치를 받았습니다: %s", path)
