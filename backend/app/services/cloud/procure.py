"""조달 — 빌리고, 학습을 얹고, 반드시 파기한다 (feature/vast-training.md §3·§6).

## 이 파일의 유일한 약속

```
try:
    create → ssh 대기 → 학습 → 회수
finally:
    job.finish(provider)      # ← 어느 경로로 왔든 여기를 지난다
```

예외든 예산 초과든 사람이 중지를 눌렀든, 나가는 길은 하나다. 학습이 실패하면 다시
돌리면 되지만 **파기가 실패하면 아무도 안 보는 동안 요금이 나간다.**

## ⚠ 러너를 갈아끼울 뿐이다

조달이 책임지는 것은 **"SSH 되는 박스를 내놓는 것"** 까지다(§3). 학습 실행·메트릭
파싱·WS 중계·재부착은 이미 있는 코드가 한다 — `TrainManager` 에 `SSHRunner` 를 꽂아
주면 위쪽은 로그가 어디서 왔는지 모른다.

## ⚠ `running` 은 접속 가능과 다르다

실측(2026-09-16): 인스턴스가 `running` 이 된 뒤에도 SSH 가 거부됐다. 원인은
`authorized_keys` 권한이었고, 상태만 보고 학습을 걸었다면 **빌린 채로 아무것도 못 하고
요금만 나갔다.** 그래서 상태가 아니라 **실제로 붙어 본다.**
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.services.cloud.lifecycle import Budget, CloudJob, Phase
from app.services.training.runners.ssh import SSHTarget, available

logger = logging.getLogger(__name__)

#: 인스턴스가 떠서 SSH 가 열릴 때까지 기다리는 상한. 실측: pull 이 1~3분, Vast 가
#: 우리 이미지 위에 자체 빌드를 얹느라 더 걸릴 때가 있어 8분 안쪽이었다. 12분이면
#: 넉넉하고, 그걸 넘으면 **느린 호스트를 시계를 켜 둔 채 기다리는 것**이다.
SSH_WAIT_S = 720.0
#: 예산·시간 상한을 몇 초마다 보나.
TICK_S = 30.0


class ProcureError(RuntimeError):
    """조달 실패. ⚠ 이걸 던져도 `finally` 가 파기를 지난다."""


async def wait_for_ssh(provider, instance_id: int, target_for,
                       *, timeout: float = SSH_WAIT_S,
                       poll: float = 10.0) -> SSHTarget:
    """`running` 이 되고 **실제로 붙을 때까지** 기다린다.

    ⚠ 상태만 보면 안 된다(모듈 설명 참고). `available()` 은 접속·tmux·작업 디렉토리를
    한 번에 확인하므로, 이게 통과하면 학습을 걸 수 있다는 뜻이다.
    """
    end = time.monotonic() + timeout
    last = "아직 상태를 못 받았습니다"
    while time.monotonic() < end:
        inst = await asyncio.to_thread(provider.status, instance_id)
        if inst is None:
            raise ProcureError(f"인스턴스 {instance_id} 가 사라졌습니다")
        if inst.status in ("exited", "offline"):
            raise ProcureError(f"인스턴스가 {inst.status} 입니다: {inst.message}")
        if inst.running and inst.ssh:
            t = target_for(inst)
            ok, why = await asyncio.to_thread(available, t)
            if ok:
                return t
            last = why
        else:
            last = f"{inst.status}: {inst.message}" if inst.message else inst.status
        await asyncio.sleep(poll)
    raise ProcureError(f"{timeout / 60:.0f}분 안에 접속하지 못했습니다 — {last}")


async def run_until_done(job: CloudJob, is_running, stop, *,
                         tick: float = TICK_S) -> str:
    """학습이 끝날 때까지 지켜본다. **끝난 사유를 돌려준다.**

    ⚠ 여기가 사람 대신 상한을 보는 자리다. 넘으면 **중지시킨다** — 알림만 하고
    그냥 두면 상한이 아니라 장식이다(RENT 탭의 예산 칸이 한동안 그랬다).
    """
    while True:
        if not is_running():
            return ""
        why = job.over_budget()
        if why:
            logger.warning("[%s] %s — 학습을 중지한다", job.job_id, why)
            try:
                await stop()
            except Exception as exc:                                # noqa: BLE001
                # ⚠ 중지 실패가 파기를 막으면 안 된다. 어차피 파기가 기계를 없앤다.
                logger.error("[%s] 중지 실패(파기는 계속): %s", job.job_id, exc)
            return why
        await asyncio.sleep(tick)


async def procure_and_train(
    *, provider, job_id: str, offer_id: int, template_hash: str, disk_gb: float,
    budget: Budget, start_training, is_running, stop, target_for,
    on_phase=None, tick: float = TICK_S, ssh_timeout: float = SSH_WAIT_S,
) -> CloudJob:
    """한 바퀴. **`finally` 가 이 함수의 요점이다.**

    `start_training(target, max_hours)` 는 러너를 꽂고 학습을 거는 콜백이다 — 조달은
    학습이 무엇인지 모른다(§3: "SSH 되는 박스를 내놓는 것까지").
    """
    label = f"piper-{job_id}"
    job = CloudJob(job_id=job_id, label=label, budget=budget)

    def phase(p: Phase, reason: str = "") -> None:
        job.set_phase(p, reason)
        if on_phase:
            on_phase(job)

    try:
        phase(Phase.CREATING)
        inst = await asyncio.to_thread(
            provider.create, offer_id,
            template_hash=template_hash, disk_gb=disk_gb, label=label)
        # ⚠ **시계는 여기서 시작한다** — 이 줄 앞에서 실패하면 빌린 것이 없다.
        job.note_started(inst.id, inst.rate_usd_h)
        phase(Phase.SSH_WAIT)

        target = await wait_for_ssh(provider, inst.id, target_for,
                                    timeout=ssh_timeout)
        phase(Phase.TRAINING)
        # 남은 예산·시간 중 **짧은 쪽**을 학습 자체의 상한으로 준다. 게이트웨이가
        # 죽어도 학습은 그 안에 끝난다(§6-1).
        cap_h = min(budget.max_hours or 1e9,
                    (budget.usd / budget.rate_usd_h) if budget.rate_usd_h else 1e9)
        await start_training(target, cap_h if cap_h < 1e9 else 0.0)

        reason = await run_until_done(job, is_running, stop, tick=tick)
        phase(Phase.RETRIEVING, reason)
        return job
    except Exception as exc:                                        # noqa: BLE001
        logger.error("[%s] 조달/학습 실패: %s", job_id, exc)
        job.reason = str(exc)
        raise
    finally:
        # ⚠ **여기가 이 파일의 이유다.** 성공이든 예외든 예산 초과든 파기를 지난다.
        #   확인이 안 되면 `finish()` 가 `ORPHAN` 으로 남기고 화면이 빨간 배너를 띄운다.
        job.finish(provider, job.reason)
        if on_phase:
            on_phase(job)
