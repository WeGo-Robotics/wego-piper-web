"""고아 스캐너 — **아무도 안 보는 동안 도는 기계**를 찾아낸다 (feature/vast-training.md §6-3).

```
기동 30초 뒤 → 훑는다 → 10분 → 훑는다 → 10분 → ...
                  │
                  └ 라벨이 `piper-` 인데 레지스트리가 모른다 → 경고 (파기는 안 한다)
```

## 왜 주기적으로 훑나

화면을 열면 인스턴스 탭이 고아를 표시한다. 그런데 **고아가 생기는 상황이 곧 아무도
화면을 안 보는 상황이다** — 게이트웨이가 죽었다 살아났거나, 배포로 재시작했거나,
`finish()` 가 파기를 확인하지 못한 채 프로세스가 내려간 경우다. 그때 요금은 계속
나가고, 알아채는 유일한 방법이 "누군가 그 탭을 열어 보는 것" 이면 그건 안전장치가
아니다. 그래서 **서버가 스스로 본다.** 브라우저가 하나도 안 붙어 있어도 로그에는
남는다.

## ⚠ 절대 자동으로 파기하지 않는다

§10 결정 5 다. 라벨이 `piper-` 라고 해서 **이 게이트웨이의 것이라는 보장이 없다** —
다른 기계의 게이트웨이가 돌리는 학습일 수 있고, 남의 6시간짜리 학습을 끄는 쪽이
요금 몇 푼보다 나쁜 실수다. 경고하고, 버튼을 주고, 사람이 누르게 한다.

## ⚠ 레지스트리가 비면 전부 고아로 보인다

`known` 은 레지스트리의 `instance_id` 로 만들어진다. 그 칸을 채우는 코드가 없던
동안에는 `known` 이 늘 비어서 **지금 학습 중인 인스턴스까지 고아**였다. 10분마다
빨간 경보를 울리는 스캐너에게 그건 치명적이다 — 늘 울리는 알람은 꺼진 알람이다.
그래서 `rent._remember()` 가 인스턴스 번호를 레지스트리에 남기고, 여기서는 그것에
더해 **지금 도는 임대 job** 까지 `known` 에 넣는다(레지스트리 쓰기가 실패해도
거짓 경보가 안 나게).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time

from app.services.cloud.lifecycle import find_orphans

logger = logging.getLogger(__name__)

#: 훑는 주기. §6-3 이 정한 값이다. 10분이면 최악의 경우 $0.5/h 짜리 기계가
#: 눈치채기 전에 $0.08 를 쓴다 — 더 자주 볼 만큼 비싸지 않고, 더 뜸하게 볼 만큼
#: 싸지도 않다. `vastai show instances` 는 서브프로세스라 공짜가 아니다.
SWEEP_S = 600.0

#: 기동 후 첫 스캔까지. **복원이 먼저다** — 학습·정책서버·데이터셋 job 재부착이
#: 끝나고 레지스트리가 제자리를 찾은 뒤에 봐야 한다. 그 전에 보면 살아 있는 임대를
#: 고아로 부를 수 있다. 몇 시간 돌던 고아가 30초를 더 돈다고 달라지지 않는다.
FIRST_SCAN_S = 30.0

#: 우리 라벨. 이게 레지스트리를 잃고도 남는 유일한 단서다.
PREFIX = "piper-"

#: 마지막 스캔 결과. 화면은 이걸 받아 간다 — 배너를 그리려고 20초짜리 Vast 조회를
#: 다시 하지 않는다.
_last: dict = {
    "orphans": [],
    "scanned_at": 0.0,
    "error": "",
    "scanned": False,
}
#: 방금 그 오류를 또 찍지 않으려고 들고 있는 값. API 키가 없는 설치에서 10분마다
#: 같은 경고를 남기면 로그가 그 문장으로 덮인다.
_last_error = ""


def known_instances() -> set[int]:
    """**우리가 관리 중인** 인스턴스 번호.

    ⚠ "예전에 이 번호를 본 적이 있다" 가 아니다. 파기됐거나 고아로 판정된 job 은
    번호를 비우므로(`rent._remember`), 고아는 계속 고아로 보인다 — 한 번 경고하고
    잊어버리면 요금은 계속 나간다.
    """
    out: set[int] = set()
    try:
        from app.services.training.jobs import job_registry

        for r in job_registry.list():
            if str(r.instance_id or "").isdigit():
                out.add(int(r.instance_id))
    except Exception as exc:                                        # noqa: BLE001
        # ⚠ 레지스트리를 못 읽으면 `known` 이 비고 **전부 고아로 보인다.** 그래서
        #   조용히 넘기지 않는다 — 다만 스캔 자체를 멈추지도 않는다.
        logger.warning("임대 레지스트리를 못 읽었습니다(거짓 경보 가능): %s", exc)
    try:
        from app.services.cloud import rent

        live = rent.current()
        if live is not None and live.instance_id:
            out.add(int(live.instance_id))
    except Exception:                                               # noqa: BLE001
        pass
    return out


def release_claims() -> list[int]:
    """기동 시 **임대 주장을 비운다.** 비운 인스턴스 번호를 돌려준다.

    ## ⚠ 재기동했다면 관리 중인 임대는 **하나도 없다**

    임대 태스크(`rent._task`)는 asyncio 태스크라 **프로세스와 함께 죽는다.** 학습은
    tmux 에 남아 재부착되지만(`restore_running_process`) 임대에는 그런 경로가 없다 —
    게이트웨이가 죽는 순간 그 기계는 아무도 관리하지 않는다.

    ## ⚠ 그런데 번호는 Redis 에 남는다

    그대로 두면 `known_instances()` 가 "관리 중" 으로 읽고, 고아 스캐너가 **조용해진다** —
    스캐너가 있어야 할 바로 그 경우에. 실측(2026-09-17)으로 크래시를 재현해 확인했다:

    ```
    임대 태스크 살아있나: False   ← 아무도 관리 안 함
    스캐너가 아는 인스턴스: {99999} ← 그런데 "관리 중" 으로 봄
    고아로 잡히나: []             ← 경고 안 뜸 → 조용히 과금
    ```

    ## ⚠ 파기는 여기서 하지 않는다

    비우는 것은 "아는 척을 그만두는 것" 일 뿐이다. 끄는 것은 사람이 인스턴스 탭에서
    한다(§10 결정 5) — 비웠다고 남의 것일 가능성이 사라지지는 않는다.
    """
    freed: list[int] = []
    try:
        from app.services.training.jobs import job_registry

        for r in job_registry.list():
            if not str(r.instance_id or "").isdigit():
                continue
            freed.append(int(r.instance_id))
            r.instance_id = ""
            job_registry.put(r)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("임대 주장을 비우지 못했습니다: %s", exc)
    return freed


def _describe(inst) -> dict:
    """사람이 읽을 한 줄까지 **백엔드가 만든다.**

    ⚠ 화면이 문장을 조립하면 판정과 문구가 따로 고쳐져 어긋난다(`DeviceAlerts` 와
    같은 규칙). 요금을 문장에 넣는 이유는 그게 사람을 움직이는 숫자라서다.
    """
    rate = f"${inst.rate_usd_h:.3f}/시간" if inst.rate_usd_h else "요금 미상"
    return {
        "id": inst.id,
        "label": inst.label,
        "status": inst.status,
        "gpu_name": inst.gpu_name,
        "rate_usd_h": inst.rate_usd_h,
        "text": (f"임대 기계 {inst.id}({inst.label})이 아직 살아 있습니다 — "
                 f"{inst.gpu_name or 'GPU'} {rate}. 클라우드 GPU → 인스턴스에서 "
                 f"확인하고 파기하세요"),
    }


def scan(provider) -> list[dict]:
    """한 번 훑는다. **파기는 안 한다** — 이 함수는 `provider.destroy` 를 부르지 않는다."""
    orphans = find_orphans(provider, known_instances(), PREFIX)
    return [_describe(i) for i in orphans]


async def scan_once(provider_factory=None) -> list[dict]:
    """한 바퀴 — 조회·판정·기록·알림. **예외를 밖으로 내보내지 않는다.**"""
    global _last_error

    if shutil.which("vastai") is None:
        # Vast 를 안 쓰는 설치가 대부분이다. 고장이 아니므로 조용히 넘어간다.
        logger.debug("vastai 가 없습니다 — 고아 스캔을 건너뜁니다")
        return []

    if provider_factory is None:
        from app.services.cloud.providers import vast

        provider_factory = vast.VastProvider

    before = {o["id"] for o in _last["orphans"]}
    try:
        found = await asyncio.to_thread(scan, provider_factory())
    except Exception as exc:                                        # noqa: BLE001
        msg = str(exc)[:200]
        # 같은 오류를 10분마다 경고로 찍으면 로그가 그 문장으로 덮인다.
        # 처음(또는 달라졌을 때)만 경고, 그다음부터는 debug.
        if msg != _last_error:
            logger.warning("고아 스캔 실패: %s", msg)
            _last_error = msg
        else:
            logger.debug("고아 스캔 실패(계속): %s", msg)
        _last["error"] = msg
        _last["scanned_at"] = time.time()
        return list(_last["orphans"])

    _last_error = ""
    _last["error"] = ""
    _last["orphans"] = found
    _last["scanned_at"] = time.time()
    _last["scanned"] = True

    now = {o["id"] for o in found}
    # ⚠ **있는 동안 매번 남긴다.** 전이에서만 찍으면 재시작 뒤 로그를 보는 사람은
    #   지금 돈이 나가는 중인지 알 수 없다. 화면 알림과 달리 로그는 반복이 싸다.
    for o in found:
        logger.warning("고아 인스턴스: %s — `vastai destroy instance %s` 로 끌 수 있습니다",
                       o["text"], o["id"])
    if before and not now:
        logger.info("고아 인스턴스가 정리됐습니다")

    if now != before:
        await _broadcast(found)
    return found


async def _broadcast(orphans: list[dict]) -> None:
    """**전이에서만** 화면에 민다 (`device_alert` 와 같은 규칙)."""
    try:
        from app.routers.ws import broadcast_cloud_orphans

        await broadcast_cloud_orphans(orphans)
    except Exception as exc:                                        # noqa: BLE001
        logger.debug("고아 알림 전파 실패: %s", exc)


def snapshot() -> dict:
    """마지막 스캔 결과. **여기서 Vast 를 부르지 않는다** — 화면이 붙을 때마다
    20초짜리 조회를 하면 배너 하나가 페이지를 느리게 만든다."""
    return {
        "orphans": list(_last["orphans"]),
        "count": len(_last["orphans"]),
        "scanned_at": _last["scanned_at"],
        "scanned": _last["scanned"],
        "error": _last["error"],
        "interval_s": SWEEP_S,
    }


async def run_sweeper(*, interval: float = SWEEP_S, first_delay: float = FIRST_SCAN_S,
                      provider_factory=None) -> None:
    """기동 시 한 번, 그다음 주기적으로. **어떤 실패도 이 태스크를 죽이지 못한다.**

    ⚠ 루프가 죽으면 조용히 죽는다 — 아무도 "스캐너가 멈췄다" 를 안 본다. 그래서
    예외를 여기서 삼킨다. 취소(`CancelledError`)만 통과시킨다.
    """
    try:
        await asyncio.sleep(first_delay)
        while True:
            try:
                await scan_once(provider_factory)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                                # noqa: BLE001
                logger.warning("고아 스캐너가 한 바퀴를 못 돌았습니다(계속 돕니다): %s", exc)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.debug("고아 스캐너를 멈춥니다")
        raise
