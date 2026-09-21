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

from app.services.cloud import layout

logger = logging.getLogger(__name__)

#: 회수에 쓸 최대 시간. 200MB 를 못 끄는 회선이면 그 기계는 애초에 느린 것이다.
RESCUE_TIMEOUT_S = 600.0

#: 이것만 받는다. `training_state` 는 두 배 크고 추론에 안 쓴다.
#: ⚠ 이름은 `layout` 것을 쓴다 — 여기서 따로 적으면 둘이 갈린다.
WANTED = layout.WANTED


def dest_for(remote_path: str, repo_id: str, step: str,
             root: Path | None = None) -> Path:
    """원격 경로가 알려 준 자리 — **로컬 학습과 같은 모양**으로 (layout.py).

    ⚠ 예전에는 여기서 HF 캐시 모양(`models--org--name/snapshots/rescued`)을 지어냈다.
    파일은 화면에 떴지만 로컬 학습과 다른 덩어리로 읽혀서, 같은 학습의 중간 체크포인트와
    최종본이 목록에서 갈라졌다. 이름은 **원격이 이미 지어 놨다** — 받아 쓰면 된다.
    """
    name = layout.run_name(remote_path) or layout.fallback_run(repo_id)
    return layout.checkpoint_dir(name, step, root)


def _scp_opts(target) -> tuple[list[str], str]:
    """`scp` 용 옵션과 대상. **`ssh` 와 같은 것을 쓴다.**

    ⚠ `scp` 는 ssh 와 같은 옵션이 필요하다(포트·키·known_hosts). `_ssh_argv` 가 만든
    것에서 옵션만 떼어 쓴다 — 여기서 따로 조립하면 둘이 갈린다. 포트만 `-p` → `-P` 다.
    """
    from app.services.training.runners.ssh import _ssh_argv

    argv = _ssh_argv(target, "")
    opts = ["-P" if o == "-p" else o for o in argv[1:-2]]
    return opts, argv[-2]


def _remote_checkpoint(target, run) -> str | None:
    """원격에서 `last/pretrained_model` 의 실제 경로를 찾는다.

    ⚠ lerobot 이 `outputs/train/<날짜>/<시각>_<정책>/` 로 자기 디렉토리를 정하므로
    우리가 미리 알 수 없다 — 물어봐야 한다.
    """
    r = run(target, f"ls -d /root/outputs/train/*/*/checkpoints/last/{WANTED} 2>/dev/null | head -1")
    path = (r.stdout or "").strip()
    return path or None


def rescue(target, repo_id: str, root: Path | None = None, *,
           run=None, timeout: float = RESCUE_TIMEOUT_S) -> Path | None:
    """`last/pretrained_model` 을 끌어온다. 받은 자리를 돌려준다. 못 받으면 `None`.

    ⚠ **예외를 올리지 않는다.** 보험이 실패했다고 파기가 막히면 본전도 못 찾는다 —
    기계가 계속 돌면서 요금이 나간다. 실패는 로그로 남기고 넘어간다.
    """
    from app.services.training.runners.ssh import _run

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

    dest = dest_for(src, repo_id, layout.LAST, layout.resolve_root(root, ensure=True))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    opts, host = _scp_opts(target)
    dest_str = str(dest)
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


def fetch_checkpoints(target, repo_id: str, root: Path | None = None, *,
                      run=None, timeout: float = RESCUE_TIMEOUT_S) -> list[Path]:
    """**중간 체크포인트**를 전부 끌어온다. 받은 자리들을 돌려준다.

    ## ⚠ 왜 따로 필요한가

    `push_to_hub` 는 학습이 끝날 때 **한 번만** 올린다(§12-4) — 그래서 `save_freq` 를
    아무리 잘게 줘도 **Hub 에는 최종본뿐**이다. 중간 것은 기계 안에만 있고, 기계를
    파기하면 같이 사라진다. 20K 학습에 5000마다 저장했는데 최종 하나만 돌아오는 것이
    그 때문이다.

    ## ⚠ `last` 는 건너뛴다

    최종본은 Hub 든 `scp` 든 이미 받는다. 또 받으면 같은 200MB 를 두 번 내는 것이다.

    ## ⚠ 실패해도 예외를 올리지 않는다

    이건 덤이다. 이것 때문에 파기가 막히면 본전도 못 찾는다 — 기계가 계속 돈다.
    """
    from app.services.training.runners.ssh import _run

    run = run or _run
    try:
        r = run(target, "ls -d /root/outputs/train/*/*/checkpoints/*/"
                        f"{WANTED} 2>/dev/null")
        paths = [p.strip() for p in (r.stdout or "").splitlines() if p.strip()]
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("중간 체크포인트 목록을 못 읽었습니다: %s", exc)
        return []

    # `checkpoints/<step>/pretrained_model` → step 이 마지막에서 두 번째.
    # ⚠ **step 이름은 원격 것을 그대로 쓴다.** `020000` 의 제로패딩은 lerobot 이 붙인
    #   것이고, 여기서 `20000` 으로 고쳐 쓰면 로컬 학습과 이름이 갈린다.
    wanted = [(p.rsplit("/", 2)[1], p) for p in paths]
    wanted = [(step, p) for step, p in wanted if step != layout.LAST]
    if not wanted:
        logger.info("중간 체크포인트가 없습니다 (save_freq 를 안 줬거나 아직 안 찍혔습니다)")
        return []

    base = layout.resolve_root(root, ensure=True)
    opts, host = _scp_opts(target)
    got: list[Path] = []
    for step, src in sorted(wanted, key=lambda t: (len(t[0]), t[0])):
        dest = dest_for(src, repo_id, step, base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        logger.info("중간 체크포인트 회수: step %s", step)
        try:
            out = subprocess.run(["scp", "-r", *opts, f"{host}:{src}", str(dest)],
                                 capture_output=True, text=True, timeout=timeout)
        except Exception as exc:                                    # noqa: BLE001
            logger.error("step %s 회수 실패(계속): %s", step, exc)
            continue
        if out.returncode != 0:
            logger.error("step %s 회수 실패(계속): %s", step, (out.stderr or "").strip()[:160])
            continue
        # ⚠ 스캐너는 `config.json` 을 보고 정책인지 판단한다 — 없으면 화면에 안 뜬다
        if not (dest / "config.json").is_file():
            logger.error("step %s: config.json 이 없습니다 — 건너뜁니다", step)
            continue
        got.append(dest)
    logger.info("중간 체크포인트 %d개를 받았습니다", len(got))
    return got
