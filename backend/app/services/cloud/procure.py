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

#: 인스턴스가 떠서 SSH 가 열릴 때까지 기다리는 상한.
#:
#: 실측(2026-09-16~17): 빠른 호스트는 pull+부팅이 **6분 20초**, 1분대도 있었다. 반면
#: 느린 호스트는 12분을 기다려도 `Pulling fs layer` 에서 못 벗어났고 — 두 번 그랬다 —
#: 그동안 요금은 계속 나갔다. 기다림을 늘려도 그런 호스트는 안 온다.
#:
#: ⚠ **8분은 §11-5 의 감시① 과 같은 값이다.** 사람이 손으로 돌릴 때 쓰던 기준과
#: 코드가 쓰는 기준이 다르면, 둘 중 하나는 반드시 틀린 기대를 만든다. 12분이었을 때
#: 느린 호스트에 4분을 더 태웠다($0.013 vs $0.008).
SSH_WAIT_S = 480.0
#: 학습 스택이 깔릴 때까지 기다리는 상한. **SSH 가 열린 뒤부터 잰다.**
#:
#: ⚠ SSH 가 되는 것과 학습을 걸 수 있는 것은 다르다. `slim` 이미지는 lerobot 을 담고
#: 있지 않고 **첫 부팅 때** `install-stack.sh` 로 깐다(`bootstrap.sh`). 그래서 접속은
#: 되는데 `python -m lerobot.scripts.lerobot_train` 은 아직 없는 구간이 있다.
#:
#: ⚠ **실측(2026-09-17)**: 그 구간에 학습을 걸어 3초 만에 죽었다 —
#: `No module named 'lerobot'`. 기계를 빌리고 이미지를 받고 접속까지 한 뒤였다
#: ($0.0145). `bootstrap.sh` 머리말은 "SSHRunner 는 `.ready` 를 보고 시작한다" 고
#: 적고 있었지만, **그걸 보는 코드가 어디에도 없었다.**
#:
#: full 이미지는 `.ready` 가 구울 때 박히므로 여기서 한 번에 통과한다.
STACK_WAIT_S = 900.0

#: 스택이 깔렸다는 표시. `bootstrap.sh` 가 마지막에 쓴다.
READY_FILE = "/opt/piper/.ready"
#: 설치가 실패했을 때 사유가 있는 곳 — 사람에게 이 경로를 그대로 준다.
BOOTSTRAP_LOG = "/opt/piper/bootstrap.log"

#: 예산·시간 상한을 몇 초마다 보나. 요금이 시간당이라 30초면 최악 오차가 `rate/120`
#: 달러다($0.1/h 기준 $0.0008) — 더 자주 볼 이유가 없다.
TICK_S = 30.0

#: "학습이 끝났나" 를 보는 주기. 상한과 달리 **빨리 알아야 한다** — 끝난 뒤의 시간은
#: 빈 기계에 내는 돈이고, 사람이 [중지]를 눌렀다면 회수가 그만큼 늦어진다.
_DONE_POLL_S = 3.0


class ProcureError(RuntimeError):
    """조달 실패. ⚠ 이걸 던져도 `finally` 가 파기를 지난다."""


def _check(guard) -> None:
    """예산·시간 상한을 넘었으면 **기다림을 끊는다.**

    ⚠ 대기 구간에도 요금은 똑같이 나간다. 예전에는 상한을 학습 중에만 봤는데, 그때는
    학습 전 대기가 최대 8분이라 눈에 안 띄었다. 스택 설치 대기가 붙으면서 학습 한 줄도
    안 돌고 **23분**까지 갈 수 있게 됐다 — 그 사이 예산을 넘기면 "$0.20 상한" 은 상한이
    아니라 장식이다.
    """
    why = guard() if guard else None
    if why:
        raise ProcureError(why)


async def wait_for_ssh(provider, instance_id: int, target_for,
                       *, timeout: float = SSH_WAIT_S,
                       poll: float = 10.0, guard=None) -> SSHTarget:
    """`running` 이 되고 **실제로 붙을 때까지** 기다린다.

    ⚠ 상태만 보면 안 된다(모듈 설명 참고). `available()` 은 접속·tmux·작업 디렉토리를
    한 번에 확인하므로, 이게 통과하면 학습을 걸 수 있다는 뜻이다.
    """
    end = time.monotonic() + timeout
    last = "아직 상태를 못 받았습니다"
    while time.monotonic() < end:
        _check(guard)
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


def stack_ready(target, run=None) -> tuple[bool, str]:
    """학습 스택이 깔렸나. **`.ready` 가 그 표시다**(`deploy/train/bootstrap.sh`).

    ⚠ 이 검사를 `runners.ssh.available()` 에 넣지 않는다. 그건 사내 박스
    (`PIPER_TRAIN_SSH_HOST`)에도 쓰이는데 거기엔 `/opt/piper` 가 없다 — 넣으면 임대와
    상관없는 원격 학습이 통째로 막힌다. 이건 **임대 인스턴스의 조건**이다.
    """
    if run is None:
        from app.services.training.runners.ssh import _run as run

    # ⚠ 판정은 **종료 코드**로 한다. `.ready` 의 내용을 문자열로 뒤지면 `bootstrap.sh`
    #   의 문구에 묶인다 — 거기 한 글자만 바뀌어도 여기가 조용히 틀린다.
    # ⚠ 한 번의 왕복으로 "됐나" 와 "왜 안 됐나" 를 같이 가져온다. 안 됐을 때 다시
    #   붙어서 로그를 읽으면, 그 왕복 동안에도 요금이 나간다.
    r = run(target, f'if [ -f {READY_FILE} ]; then cat {READY_FILE}; '
                    f'else tail -5 {BOOTSTRAP_LOG} 2>/dev/null; exit 3; fi')
    # ⚠ **stdout 을 먼저 본다.** 둘을 이어 붙였더니 화면에 뜬 것이
    #   `bash: warning: setlocale: LC_ALL: cannot change locale` 이었다 — 설치 로그는
    #   stdout 이고 저 잡음은 stderr 인데, 이어 붙이면 잡음이 **마지막 줄**이 된다.
    #   사람이 보는 한 줄이 진행 상황이 아니라 로케일 경고면 아무 소용이 없다.
    detail = (r.stdout or "").strip() or (r.stderr or "").strip()
    return r.returncode == 0, detail or "아직 설치 로그가 없습니다"


#: 설치 로그에서 **사람에게 보일 만한 줄**만 고른다.
#:
#: ⚠ `bootstrap.sh` 의 출력에는 로케일 경고·pip 진행 막대 같은 잡음이 섞인다. 그걸
#: 그대로 올리면 화면이 `bash: warning: setlocale…` 을 "진행 상황" 이라고 보여 준다 —
#: §12-13 에서 로그가 그랬던 것과 같은 실수를 화면에서 반복하는 셈이다.
def install_note(detail: str) -> str:
    """설치 로그 꼬리 → 화면에 보일 한 줄. 볼 게 없으면 빈 문자열."""
    for line in reversed((detail or "").splitlines()):
        t = line.strip()
        if not t or t.startswith("bash:") or t.startswith("WARNING"):
            continue
        # `install-stack.sh` 가 찍는 절 제목(`== …`)이 제일 쓸모 있다
        if t.startswith("=="):
            return t.lstrip("= ").strip()[:80]
        if t.startswith(("Collecting", "Downloading", "Installing", "Successfully")):
            return t[:80]
    return ""


async def wait_for_stack(target, *, timeout: float = STACK_WAIT_S,
                         poll: float = 15.0, run=None, guard=None,
                         on_progress=None) -> str:
    """스택이 깔릴 때까지 기다린다. **접속되는 것과 학습할 수 있는 것은 다르다.**

    ⚠ 여기서 기다리는 시간은 낭비가 아니다 — slim 은 이 구간에 torch 를 받는다. 반면
    이걸 **안** 기다리면 빌린 값을 다 치르고 3초 만에 죽는다(실측 $0.0145).
    """
    end = time.monotonic() + timeout
    last = "아직 확인 못 했습니다"
    while time.monotonic() < end:
        _check(guard)
        try:
            ok, detail = await asyncio.to_thread(stack_ready, target, run)
        except Exception as exc:                                    # noqa: BLE001
            last = str(exc)[:200]
        else:
            if ok:
                return detail
            last = detail
            note = install_note(detail)
            logger.info("학습 스택 설치 중: %s", note or "(설치 로그 없음)")
            if on_progress:
                # ⚠ 화면에 가는 문구는 **여기서 고른다.** 로그 꼬리를 그대로 올리면
                #   로케일 경고가 "진행 상황" 으로 뜬다.
                on_progress(note or "학습 스택 설치 중")
        await asyncio.sleep(poll)
    raise ProcureError(
        f"{timeout / 60:.0f}분 안에 학습 스택이 준비되지 않았습니다 — "
        f"인스턴스에서 {BOOTSTRAP_LOG} 를 보세요. 마지막 줄: {last[:200]}")


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
        # ⚠ **상한을 보는 주기와 "끝났나" 를 보는 주기는 다르다.** 돈은 30초마다 봐도
        #   되지만(요금이 시간당이라 오차가 rate/120 달러다), 학습이 끝난 것은 빨리
        #   알아야 한다 — 그만큼 빈 기계가 켜져 있고, 사람이 [중지]를 눌렀다면 회수가
        #   그만큼 늦어진다. 그래서 틱 하나를 잘게 쪼개서 본다.
        waited = 0.0
        while waited < tick:
            step = min(_DONE_POLL_S, tick - waited)
            await asyncio.sleep(step)
            waited += step
            if not is_running():
                return ""


async def procure_and_train(
    *, provider, job_id: str, offer_id: int, template_hash: str, disk_gb: float,
    budget: Budget, start_training, is_running, stop, target_for,
    on_phase=None, tick: float = TICK_S, ssh_timeout: float = SSH_WAIT_S,
    stack_timeout: float = STACK_WAIT_S, stack_poll: float = 15.0,
    rescue_if_needed=None,
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

        # ⚠ 대기 구간에도 상한을 본다. 안 보면 학습 한 줄 못 돌고 예산을 넘긴 채로
        #   계속 기다리게 된다 — 상한이 상한이 아니게 된다.
        target = await wait_for_ssh(provider, inst.id, target_for,
                                    timeout=ssh_timeout, guard=job.over_budget)
        # ⚠ **접속된 것과 학습할 수 있는 것은 다르다.** slim 이미지는 여기서 아직
        #   torch·lerobot 을 받는 중이다. 안 기다리면 빌린 값을 다 치르고 3초 만에
        #   `No module named 'lerobot'` 로 죽는다 (실측 2026-09-17, $0.0145).
        def _note(text: str) -> None:
            job.note = text
            if on_phase:
                on_phase(job)

        ready = await wait_for_stack(target, timeout=stack_timeout,
                                     poll=stack_poll, guard=job.over_budget,
                                     on_progress=_note)
        logger.info("[%s] 학습 스택 준비됨: %s", job_id, ready.splitlines()[-1][:120])
        phase(Phase.TRAINING)
        # 남은 예산·시간 중 **짧은 쪽**을 학습 자체의 상한으로 준다. 게이트웨이가
        # 죽어도 학습은 그 안에 끝난다(§6-1).
        cap_h = min(budget.max_hours or 1e9,
                    (budget.usd / budget.rate_usd_h) if budget.rate_usd_h else 1e9)
        await start_training(target, cap_h if cap_h < 1e9 else 0.0)

        reason = await run_until_done(job, is_running, stop, tick=tick)

        # ⚠ **회수는 파기보다 먼저다.** 실측(§12-4): 푸시는 학습이 끝날 때 한 번뿐이라,
        #   중간에 죽었거나 푸시가 실패했으면 Hub 에는 아무것도 없다. 여기서 안 끌어오면
        #   몇 시간짜리 결과가 기계와 함께 사라진다.
        phase(Phase.RETRIEVING, reason)
        if rescue_if_needed is not None:
            try:
                await rescue_if_needed(target)
            except Exception as exc:                                # noqa: BLE001
                # ⚠ 보험 실패가 파기를 막으면 본전도 못 찾는다 — 기계가 계속 돈다.
                logger.error("[%s] 가중치 회수 실패(파기는 계속): %s", job_id, exc)
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
