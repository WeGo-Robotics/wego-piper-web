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
import time

from app.core.config import settings
from app.services.cloud.lifecycle import Budget, CloudJob, Phase
from app.services.cloud import retrieve
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
    # ⚠ **벽시계**다(`Budget.started_at` 은 `monotonic` 이라 Hub 시각과 못 비교한다).
    #   이번 회차의 푸시와 지난 회차의 푸시를 가르는 기준이 된다.
    started_at = time.time()
    # ⚠ 지난 회차의 회수 상태를 지운다. 안 지우면 새 임대를 걸었는데 화면이 **지난번
    #   가중치**를 "받았음" 으로 계속 보여 준다 — 이번 것이 온 줄 안다.
    retrieve.reset()

    async def _start_training(target: SSHTarget, cap_h: float) -> None:
        # ⚠ 러너를 바꿔 끼운다. 위쪽(메트릭·WS·재부착)은 로그가 어디서 왔는지 모른다.
        runner = SSHRunner(job_id=job_id, host=target)
        runner.set_log_callback(train_manager._intercept_log)
        runner.set_state_callback(train_manager._intercept_state)
        train_manager.runner = runner
        await train_manager.start(args, total_steps=total_steps,
                                  output_dir=output_dir, env_extra=env or {},
                                  max_hours=cap_h)

    async def _retrieve(target) -> None:
        """**기계가 살아 있는 동안** 해야 할 회수 (§5·§12-4).

        ⚠ 확인 없이 항상 `scp` 로 받으면 200MB 전송비를 매번 낸다. 확인 없이 **안**
        받으면 푸시가 실패한 회차의 결과를 통째로 잃는다 — 푸시는 종료 시점 한
        번뿐이라 다시 올라올 기회가 없다(§12-4).

        Hub 에 있으면 여기서는 **안 받는다.** 받는 것은 파기 뒤다(`retrieve` 머리말).

        ⚠ `since` 를 넘기는 이유: 같은 저장소로 두 번째를 돌렸는데 이번 회차가 죽으면
        거기 있는 것은 **지난번 가중치**다. 시각을 안 보면 그걸 성공으로 읽는다.
        """
        await retrieve.before_destroy(target, repo_id, settings.models_dir,
                                      since=started_at)

    async def _go() -> None:
        global _job
        try:
            await procure_and_train(
                provider=provider, job_id=job_id, offer_id=offer_id,
                template_hash=template_hash, disk_gb=disk_gb, budget=budget,
                start_training=_start_training,
                is_running=lambda: train_manager.is_running,
                stop=train_manager.stop, target_for=_target_for,
                rescue_if_needed=_retrieve,
                on_phase=lambda j: _remember(j))
        except Exception as exc:                                    # noqa: BLE001
            logger.error("임대 학습 실패: %s", exc)
        finally:
            # ⚠ **러너를 반드시 되돌린다.** 안 되돌리면 다음 로컬 학습이 죽은 호스트로
            #   붙으려 하고, 러너 선택은 프로세스 수명 동안 다시 평가되지 않는다.
            train_manager.runner = original
            original.set_log_callback(train_manager._intercept_log)
        # ⚠ **기계가 없어진 뒤에** 받는다 — Hub 는 기계를 안 탄다. 파기 전에 받으면
        #   200MB 를 내려받는 동안 빌린 GPU 요금을 그대로 내고, 무엇보다 파기가
        #   그만큼 늦어진다. 이 기능의 가장 중요한 불변식을 회선 속도에 묶을 수 없다.
        # ⚠ 러너를 되돌린 **뒤**다. 다운로드가 오래 걸려도 원격 러너가 꽂힌 채로
        #   남지 않게 한다.
        await retrieve.after_destroy(repo_id, settings.models_dir)

    _job = CloudJob(job_id=job_id, label=f"piper-{job_id}", budget=budget)
    _task = asyncio.create_task(_go())
    return _job


def _remember(job: CloudJob) -> None:
    """살아 있는 job 을 기억하고, **인스턴스 번호를 레지스트리에 남긴다.**

    ⚠ 이 두 줄이 없으면 고아 스캐너가 **지금 도는 임대를 고아로 본다.** `known` 은
    레지스트리의 `instance_id` 로 만들어지는데 그 칸을 채우는 코드가 없었다 —
    필드만 있고 쓰는 사람이 없어서 `known` 이 늘 비었고, `piper-` 가 붙은 것은
    전부 고아였다. 10분마다 울리는 알람에게 그건 치명적이다(늘 울리는 알람은 꺼진
    알람이다).

    ⚠ **끝난 뒤에는 번호를 비운다.** 특히 `ORPHAN` 에서 — 파기를 확인 못 한 기계는
    "우리가 관리 중" 이 아니라 **사람이 봐야 할 것**이다. 번호를 남겨두면 스캐너가
    그걸 아는 척하고 조용해진다. 돈이 나가는 쪽에서 그건 최악의 침묵이다.
    """
    global _job
    _job = job
    try:
        from app.services.training.jobs import JobRecord, job_registry

        rec = job_registry.get(job.job_id) or JobRecord(job_id=job.job_id)
        rec.provider = "vast"
        rec.instance_id = ("" if job.phase in (Phase.DESTROYED, Phase.ORPHAN)
                           else str(job.instance_id or ""))
        job_registry.put(rec)
    except Exception as exc:                                        # noqa: BLE001
        # 레코드를 못 써도 학습은 계속된다. 다만 스캐너가 거짓 경보를 낼 수 있어서
        # 조용히 넘기지 않는다 — `known_instances()` 가 메모리의 job 으로 보완한다.
        logger.warning("임대 레코드 갱신 실패(학습은 계속): %s", exc)


#: 중지한 뒤 조달 흐름이 스스로 마무리하기를 기다리는 시간.
#: 틱(30초)에 회수·파기까지 여유를 둔 값이다.
STOP_GRACE_S = 90.0


async def stop_now(provider) -> CloudJob | None:
    """사람이 멈춘다. **학습을 세우고 기계를 파기한다.**

    ⚠ 학습만 세우고 끝내면 기계가 남아 과금된다 — 중지는 파기까지다.

    ⚠ **여기서 곧바로 파기하면 가중치를 잃는다.** 조달 흐름은 학습이 멈춘 것을 보고
    `retrieving` 에서 회수를 한 다음 파기한다. 중지가 그걸 앞질러 기계를 없애면
    `scp` 가 사라진 호스트를 향하고, 푸시도 안 된 회차라면 **결과가 통째로 사라진다** —
    보험을 넣은 이유가 바로 그 경우다. 그래서 흐름이 살아 있으면 **맡기고 기다린다.**

    ⚠ 다만 무한정 믿지는 않는다. 흐름이 없거나 시간 안에 안 끝나면 여기서 직접
    파기한다 — 기다리다 못 끄는 것이 제일 나쁘다.
    """
    job = _job
    if job is None:
        return None
    try:
        await train_manager.stop()
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("중지 실패(파기는 계속): %s", exc)

    task = _task
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=STOP_GRACE_S)
            logger.info("조달 흐름이 스스로 마무리했습니다 (회수 포함)")
            return _job or job
        except asyncio.TimeoutError:
            # ⚠ **기계가 이미 없으면 파기할 것도 없다.** 흐름에는 파기 뒤 Hub
            #   다운로드가 붙어 있어서(§12-12), 200MB 를 받는 중이면 90초를 넘기는
            #   것이 정상이다. 그때 "직접 파기합니다" 를 찍으면 로그가 거짓말을 한다 —
            #   읽는 사람은 파기가 늦어진 줄 안다.
            if job.phase in (Phase.DESTROYED, Phase.ORPHAN):
                logger.info("기계는 이미 파기됐습니다 — 가중치 회수는 배경에서 계속됩니다")
                return _job or job
            logger.warning("조달 흐름이 %.0f초 안에 안 끝났습니다 — 직접 파기합니다",
                           STOP_GRACE_S)
        except Exception as exc:                                    # noqa: BLE001
            logger.warning("조달 흐름이 예외로 끝났습니다(파기는 계속): %s", exc)

    # 흐름이 없거나 못 끝냈다 — 여기서 끝낸다. `finish()` 는 멱등이라 겹쳐도 안전하다.
    await asyncio.to_thread(job.finish, provider, "사람이 중지했습니다")
    return job
