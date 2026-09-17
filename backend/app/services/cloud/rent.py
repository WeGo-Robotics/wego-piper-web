"""[빌리기] 한 번에 도는 것 — 조달·학습·회수·파기를 하나의 배경 태스크로.

⚠ **이 모듈이 있어야 버튼을 켤 수 있다.** 그전까지 [빌리기]가 비활성이었던 이유는
디자인이 아니라 사실이었다 — 끄는 코드가 없었다(§9-2). 이제 상한 셋이 붙었다:
학습 스크립트의 `timeout`(가장 안쪽) · 예산/시간 틱(바깥) · 보장된 파기(`finally`).

## ⚠ 동시에 하나만

`MAX_CONCURRENT_JOBS` 가 1 이고 `TrainManager` 도 러너를 하나만 든다. 두 번째 요청을
받아 주면 **첫 번째 인스턴스의 핸들을 잃는다** — 그게 곧 고아다.

## ⚠ 러너를 바꿔 끼우고 되돌린다

`TrainManager.runner` 를 원격으로 바꾸므로, 끝나면 반드시 원래대로 돌린다. 안 그러면
다음 로컬 학습이 죽은 호스트로 붙으려 하고, `_default_runner()` 는 프로세스 수명 동안
다시 평가되지 않는다.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.services.cloud.lifecycle import Budget, CloudJob, Phase
from app.services.cloud.procure import procure_and_train
from app.services.training import train_manager
from app.services.training.runners.ssh import SSHRunner, SSHTarget

logger = logging.getLogger(__name__)

#: 지금 도는 조달 태스크. **하나뿐이다.**
_task: asyncio.Task | None = None
_job: CloudJob | None = None


def current() -> CloudJob | None:
    return _job


def busy() -> bool:
    return _task is not None and not _task.done()


def _target_for(inst) -> SSHTarget:
    """인스턴스 주소 + **게이트웨이 전용 키**.

    ⚠ 사람의 `~/.ssh` 를 안 쓴다 — 배포판 게이트웨이는 컨테이너라 그게 아예 없고,
    Vast 계정에 등록된 것은 게이트웨이 키다(§9-1).
    """
    from app.services.cloud import sshkey

    return SSHTarget(host=inst.ssh.host, port=inst.ssh.port, user="root",
                     key_path=str(sshkey.private_path()),
                     known_hosts=str(sshkey.key_dir() / "known_hosts"))


def _pushed(repo_id: str) -> bool:
    """가중치가 Hub 에 **실제로** 있나.

    ⚠ 파일 목록을 본다 — repo 가 존재하는 것과 가중치가 올라간 것은 다르다. lerobot 이
    `initial commit` 으로 repo 만 만들고 죽을 수 있다(실측: 푸시는 커밋 넷으로 나뉜다).
    """
    try:
        from huggingface_hub import HfApi

        files = {s.rfilename for s in (HfApi().model_info(repo_id).siblings or [])}
    except Exception as exc:                                        # noqa: BLE001
        # ⚠ 모르면 **받는 쪽**으로 기운다. 전송비는 몇 센트지만 잃은 학습은 몇 시간이다.
        logger.warning("Hub 확인 실패(회수 쪽으로 진행): %s", exc)
        return False
    return "model.safetensors" in files


async def start(*, provider, offer_id: int, template_hash: str, disk_gb: float,
                budget: Budget, args: list[str], total_steps: int,
                output_dir: str = "", env: dict | None = None,
                repo_id: str = "") -> CloudJob:
    """빌려서 학습을 건다. **배경으로 돌고 즉시 돌아온다.**

    돌아온 `CloudJob` 은 살아 있는 객체다 — 단계·비용이 그 위에서 갱신된다.
    """
    global _task, _job
    if busy():
        raise RuntimeError("이미 임대 학습이 돌고 있습니다 — 하나씩만 돌립니다")

    original = train_manager.runner
    job_id = train_manager.job_id

    async def _start_training(target: SSHTarget, cap_h: float) -> None:
        # ⚠ 러너를 바꿔 끼운다. 위쪽(메트릭·WS·재부착)은 로그가 어디서 왔는지 모른다.
        runner = SSHRunner(job_id=job_id, host=target)
        runner.set_log_callback(train_manager._intercept_log)
        runner.set_state_callback(train_manager._intercept_state)
        train_manager.runner = runner
        await train_manager.start(args, total_steps=total_steps,
                                  output_dir=output_dir, env_extra=env or {},
                                  max_hours=cap_h)

    async def _rescue_if_needed(target) -> None:
        """Hub 에 갔는지 **확인하고**, 안 갔으면 직접 끌어온다.

        ⚠ 확인 없이 항상 받으면 200MB 전송비를 매번 낸다. 확인 없이 **안** 받으면
        푸시가 실패한 회차의 결과를 통째로 잃는다 — 실측으로 푸시는 종료 시점 한
        번뿐이라 다시 올라올 기회가 없다(§12-4).
        """
        from app.services.cloud.rescue import rescue

        if not repo_id:
            return
        if await asyncio.to_thread(_pushed, repo_id):
            logger.info("Hub 에 가중치가 있습니다 — 회수 보험은 건너뜁니다: %s", repo_id)
            return
        logger.warning("Hub 에 가중치가 없습니다 — 파기 전에 직접 끌어옵니다: %s", repo_id)
        await asyncio.to_thread(rescue, target, repo_id, settings.models_dir)

    async def _go() -> None:
        global _job
        try:
            await procure_and_train(
                provider=provider, job_id=job_id, offer_id=offer_id,
                template_hash=template_hash, disk_gb=disk_gb, budget=budget,
                start_training=_start_training,
                is_running=lambda: train_manager.is_running,
                stop=train_manager.stop, target_for=_target_for,
                rescue_if_needed=_rescue_if_needed,
                on_phase=lambda j: _remember(j))
        except Exception as exc:                                    # noqa: BLE001
            logger.error("임대 학습 실패: %s", exc)
        finally:
            # ⚠ **러너를 반드시 되돌린다.** 안 되돌리면 다음 로컬 학습이 죽은 호스트로
            #   붙으려 하고, 러너 선택은 프로세스 수명 동안 다시 평가되지 않는다.
            train_manager.runner = original
            original.set_log_callback(train_manager._intercept_log)

    _job = CloudJob(job_id=job_id, label=f"piper-{job_id}", budget=budget)
    _task = asyncio.create_task(_go())
    return _job


def _remember(job: CloudJob) -> None:
    global _job
    _job = job


async def stop_now(provider) -> CloudJob | None:
    """사람이 멈춘다. **학습을 세우고 기계를 파기한다.**

    ⚠ 학습만 세우고 끝내면 기계가 남아 과금된다 — 중지는 파기까지다.
    """
    job = _job
    if job is None:
        return None
    try:
        await train_manager.stop()
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("중지 실패(파기는 계속): %s", exc)
    await asyncio.to_thread(job.finish, provider, "사람이 중지했습니다")
    return job
