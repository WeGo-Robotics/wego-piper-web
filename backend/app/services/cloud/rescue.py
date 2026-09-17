"""가중치 보험 — **파기 전에 직접 끌어온다** (feature/vast-training.md §5·§12-4).

## 왜 보험이 필수 경로인가

실측(§12-4): `push_to_hub` 는 **학습이 끝날 때 한 번만** 올린다. `save_freq` 로
체크포인트가 다섯 개 생겨도 Hub 커밋은 종료 시각에 한 묶음뿐이다. 즉 **인스턴스가
중간에 죽으면 Hub 에는 아무것도 없다** — 몇 시간을 돌린 결과가 통째로 사라진다.

그래서 이건 "있으면 좋은 것" 이 아니다. 푸시가 안 된 것을 확인했으면 **파기하기 전에**
`scp` 로 직접 가져온다. ssh 는 이미 열려 있으니 추가 비용은 전송비뿐이다.

## ⚠ `training_state` 는 안 받는다

실측: `last/pretrained_model` 198MB 대 `last/training_state` **394MB**. 후자는 옵티마이저
상태라 추론에 안 쓰고, Vast 는 나가는 트래픽에 GB 당 과금한다(실측 $0.017/GB). 두 배를
더 내고 안 쓸 것을 받을 이유가 없다 — 이어서 학습할 게 아니면.

## ⚠ 회수는 마감이 있다

느린 호스트에서 200MB 를 끄는 동안에도 과금은 계속된다. 상한을 넘기면 **포기하고
파기한다** — 보험이 본체보다 비싸지면 보험이 아니다.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: 회수에 쓸 최대 시간. 200MB 를 못 끄는 회선이면 그 기계는 애초에 느린 것이다.
RESCUE_TIMEOUT_S = 600.0

#: 이것만 받는다. `training_state` 는 두 배 크고 추론에 안 쓴다.
WANTED = "pretrained_model"


def snapshot_dir(models_dir: Path, repo_id: str) -> Path:
    """모델 스캐너가 읽는 자리를 만든다.

    ⚠ 스캐너는 HF 캐시 모양(`models--org--name/snapshots/<hash>/config.json`)만 읽는다.
    아무 데나 떨어뜨리면 파일은 있는데 **화면에 안 뜬다** — 회수했다고 믿는데 못 쓰는
    상태가 된다.
    """
    org, _, name = repo_id.partition("/")
    return models_dir / f"models--{org}--{name}" / "snapshots" / "rescued"


def _remote_checkpoint(target, run) -> str | None:
    """원격에서 `last/pretrained_model` 의 실제 경로를 찾는다.

    ⚠ lerobot 이 `outputs/train/<날짜>/<시각>_<정책>/` 로 자기 디렉토리를 정하므로
    우리가 미리 알 수 없다 — 물어봐야 한다.
    """
    r = run(target, f"ls -d /root/outputs/train/*/*/checkpoints/last/{WANTED} 2>/dev/null | head -1")
    path = (r.stdout or "").strip()
    return path or None


def rescue(target, repo_id: str, models_dir: Path, *,
           run=None, timeout: float = RESCUE_TIMEOUT_S) -> Path | None:
    """`last/pretrained_model` 을 끌어온다. 받은 자리를 돌려준다. 못 받으면 `None`.

    ⚠ **예외를 올리지 않는다.** 보험이 실패했다고 파기가 막히면 본전도 못 찾는다 —
    기계가 계속 돌면서 요금이 나간다. 실패는 로그로 남기고 넘어간다.
    """
    from app.services.training.runners.ssh import _run, _ssh_argv

    run = run or _run
    try:
        src = _remote_checkpoint(target, run)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("원격 체크포인트를 찾지 못했습니다: %s", exc)
        return None
    if not src:
        logger.warning("원격에 %s 가 없습니다 — 학습이 체크포인트를 남기기 전에 죽었을 수 있습니다",
                       WANTED)
        return None

    dest = snapshot_dir(models_dir, repo_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    # ⚠ `scp` 는 ssh 와 **같은 옵션**이 필요하다(포트·키·known_hosts). `_ssh_argv` 가
    #   만든 것에서 옵션만 떼어 쓴다 — 여기서 따로 조립하면 둘이 갈린다.
    argv = _ssh_argv(target, "")
    opts = argv[1:-2]                       # 'ssh' 와 (대상, 원격명령) 사이
    # scp 는 포트를 -P 로 받는다 (ssh 는 -p)
    opts = ["-P" if o == "-p" else o for o in opts]
    dest_str = str(dest)
    host = argv[-2]
    cmd = ["scp", "-r", *opts, f"{host}:{src}", dest_str]

    logger.info("가중치 회수: %s → %s", src, dest_str)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("가중치 회수가 %.0f초를 넘겼습니다 — 포기합니다(파기는 계속)", timeout)
        return None
    except Exception as exc:                                        # noqa: BLE001
        logger.error("가중치 회수 실패(파기는 계속): %s", exc)
        return None
    if r.returncode != 0:
        logger.error("가중치 회수 실패(파기는 계속): %s", (r.stderr or "").strip()[:200])
        return None

    # ⚠ **파일이 왔다고 쓸 수 있는 게 아니다.** 스캐너는 `config.json` 을 보고 정책인지
    #   판단한다 — 없으면 회수했는데 화면에 안 뜬다.
    if not (dest / "config.json").is_file():
        logger.error("회수했지만 config.json 이 없습니다: %s", dest_str)
        return None
    logger.info("가중치 회수 완료: %s", dest_str)
    return dest
