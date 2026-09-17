"""임대 인스턴스의 수명 — **어느 경로로 끝나든 파기를 지난다** (feature/vast-training.md §3·§6).

```
searching → creating → ssh_wait → training → retrieving → destroying → destroyed
                          │ 실패·중지·예산초과·시간초과 — 어느 쪽이든
                          └────────────────────────────► destroy
                                                           └ 재조회 실패 → orphan
```

## 왜 이 파일이 따로 있나

학습을 돌리는 일(`TrainManager`)과 기계를 빌리는 일은 **실패의 대가가 다르다.** 학습이
실패하면 다시 돌리면 되지만, 파기가 실패하면 **아무도 안 보는 동안 돈이 나간다.** 그래서
조달과 파기를 한 객체 안에 묶고, 그 객체의 유일한 불변식을 "빠져나갈 때 반드시 파기를
시도한다" 로 둔다.

## ⚠ 자폭은 안 한다

인스턴스 **안에서** `vastai destroy` 를 부르는 방법이 있지만, 그건 계정 API 키를 남의
하드웨어에 두는 일이다. 대신 두 겹으로 간다 — 학습 스크립트의 `timeout`(§6-1, 가장
안쪽)과 여기(바깥). 스스로 죽는 대신 **반드시 눈치채는** 구조다.

## ⚠ 낙관적으로 죽지 않는다

파기했다고 **믿지** 않는다. `destroy()` 가 목록으로 확인하고, 확인이 안 되면 상태를
`orphan` 으로 남긴다. 조용히 성공으로 처리하는 것이 이 경로에서 가장 비싼 거짓말이다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class Phase(str, Enum):
    """수명의 한 칸. **`ORPHAN` 은 실패가 아니라 경고다** — 사람이 봐야 한다."""

    SEARCHING = "searching"
    CREATING = "creating"
    SSH_WAIT = "ssh_wait"
    TRAINING = "training"
    RETRIEVING = "retrieving"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    #: 학습은 끝났고 **기계는 그대로 있다.** 사람이 클라우드 페이지에서 일부러 빌린
    #: 기계에서 학습한 경우다 — 끄는 것은 인스턴스 탭에서 사람이 한다.
    #: ⚠ `DESTROYED` 로 적으면 안 된다. 살아서 과금 중인 기계를 "파기됨" 이라고 말하는
    #: 것이 이 화면에서 제일 비싼 거짓말이다.
    FINISHED = "finished"
    #: 파기를 시도했는데 **사라진 것을 확인하지 못했다.** 살아 있을 수 있다 = 과금 중일
    #: 수 있다. 자동으로 다시 시도하지 않는다(다른 게이트웨이의 것일 수 있다 — §10 결정 5).
    ORPHAN = "orphan"


#: 상한을 안 넘겼는지 몇 초마다 보나. 실측 요금이 시간당이므로 30초면 최악 오차가
#: `rate/120` 달러다 — 0.1$/h 기준 0.0008$. 더 자주 볼 이유가 없다.
TICK_S = 30.0


@dataclass
class Budget:
    """돈과 시간의 상한. **둘 다 없으면 상한이 없다는 뜻이고, 그건 허용하지 않는다.**"""

    usd: float = 10.0
    max_hours: float = 6.0
    rate_usd_h: float = 0.0
    started_at: float = 0.0

    def elapsed_h(self, now: float | None = None) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, ((now or time.monotonic()) - self.started_at) / 3600.0)

    def accrued_usd(self, now: float | None = None) -> float:
        """지금까지 나간 돈(어림). ⚠ Vast 의 정산이 아니라 **우리 시계 × 요금**이다 —
        청구가 진실이고 이건 상한을 걸기 위한 근사다."""
        return self.rate_usd_h * self.elapsed_h(now)

    def exceeded(self, now: float | None = None) -> str | None:
        """넘겼으면 **왜** 넘겼는지. 안 넘겼으면 `None`."""
        if self.max_hours and self.elapsed_h(now) >= self.max_hours:
            return f"시간 상한 {self.max_hours:g}시간을 넘겼습니다"
        if self.usd and self.accrued_usd(now) >= self.usd:
            return f"예산 ${self.usd:g} 를 넘겼습니다 (약 ${self.accrued_usd(now):.2f})"
        return None


@dataclass
class CloudJob:
    """빌린 기계 하나의 수명. **`finish()` 는 반드시 파기를 지난다.**"""

    job_id: str
    label: str
    budget: Budget = field(default_factory=Budget)
    phase: Phase = Phase.SEARCHING
    instance_id: int | None = None
    #: 사람이 읽을 마지막 사유 — 왜 끝났는지. 비면 아직 안 끝났다는 뜻이다.
    reason: str = ""
    #: 이 기계를 **우리가 만들었나.** 한 묶음(`rent.start`)이면 참이고, 사람이 빌려 둔
    #: 기계에 학습만 얹은 경우(`rent.train_on`)면 거짓이다.
    #:
    #: ⚠ 이게 거짓이면 `finish()` 는 **파기하지 않는다.** 남의 기계를 우리 job 이
    #: 끝났다는 이유로 끄면 안 된다 — 다음 학습을 준비하던 사람의 기계가 사라진다.
    #: 사람이 [중지]를 눌러도 마찬가지다.
    owns_instance: bool = True

    def set_phase(self, phase: Phase, reason: str = "") -> None:
        if phase != self.phase:
            logger.info("[%s] %s → %s%s", self.job_id, self.phase.value,
                        phase.value, f" ({reason})" if reason else "")
        self.phase = phase
        if reason:
            self.reason = reason

    def note_started(self, instance_id: int, rate_usd_h: float) -> None:
        """조달이 끝난 순간. **여기서부터 시계가 돈다.**"""
        self.instance_id = instance_id
        self.budget.rate_usd_h = rate_usd_h
        self.budget.started_at = time.monotonic()

    def over_budget(self) -> str | None:
        return self.budget.exceeded()

    def finish(self, provider, reason: str = "") -> Phase:
        """끝낸다. **어느 경로로 왔든 여기를 지난다.**

        ⚠ 파기 실패를 예외로 올리지 않는다. 올리면 호출부의 `finally` 가 또 다른
        예외에 묻혀 **아무도 이 사실을 못 본다.** 대신 `ORPHAN` 으로 남겨 화면이
        빨간 배너를 띄우게 한다.

        ⚠ 이미 끝난 job 에 다시 불려도 안전하다(멱등) — `finally` 는 중첩되기 쉽다.
        """
        if self.phase in (Phase.DESTROYED, Phase.ORPHAN, Phase.FINISHED):
            return self.phase
        if not self.owns_instance:
            # ⚠ 우리가 만든 기계가 아니다 — 끄지 않는다. 유휴로 떠 있는 것은
            #   `sweeper.idle_boxes()` 가 말해 준다.
            self.set_phase(Phase.FINISHED, reason or "학습이 끝났습니다 (기계는 그대로)")
            return self.phase
        if self.instance_id is None:
            self.set_phase(Phase.DESTROYED, reason or "빌린 기계가 없습니다")
            return self.phase

        self.set_phase(Phase.DESTROYING, reason)
        try:
            gone = provider.destroy(self.instance_id)
        except Exception as exc:                                    # noqa: BLE001
            logger.error("[%s] 파기 중 예외: %s", self.job_id, exc)
            gone = False

        if gone:
            self.set_phase(Phase.DESTROYED, reason)
        else:
            # ⚠ 자동으로 다시 시도하지 않는다. 다른 게이트웨이가 돌리는 학습일 수
            #   있고(§10 결정 5), 남의 것을 끄는 쪽이 더 나쁘다. 사람에게 넘긴다.
            self.set_phase(
                Phase.ORPHAN,
                f"인스턴스 {self.instance_id} 가 사라진 것을 확인하지 못했습니다 — "
                f"`vastai show instances` 로 확인하고 직접 파기하세요")
            logger.error("[%s] %s", self.job_id, self.reason)
        return self.phase

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "label": self.label,
            "phase": self.phase.value,
            "instance_id": self.instance_id,
            "reason": self.reason,
            "cost": {
                "rate_usd_h": self.budget.rate_usd_h,
                "accrued_usd": round(self.budget.accrued_usd(), 4),
                "budget_usd": self.budget.usd,
                "elapsed_h": round(self.budget.elapsed_h(), 3),
                "max_hours": self.budget.max_hours,
            },
        }


def find_orphans(provider, known: set[int], prefix: str = "piper-",
                 owned_prefix: str = "piper-box-") -> list:
    """우리 라벨이 붙었는데 **아무도 관리하지 않는** 인스턴스.

    ⚠ 이게 §6-3 의 고아 스캐너다. 레지스트리를 잃어도 라벨이 남으므로 알아볼 수 있다.
    ⚠ **자동 파기는 안 한다.** 다른 기계의 게이트웨이가 돌리는 학습일 수 있다
    (§10 결정 5) — 경고와 버튼까지가 우리 몫이다.

    ⚠ **사람이 일부러 빌린 기계(`owned_prefix`)는 고아가 아니다.** 관리하는 태스크가
    없는 것이 그쪽의 정상이다 — 클라우드 페이지에서 기계를 만들고 학습 페이지에서 골라
    쓰는 흐름이라, 학습 사이사이에는 아무 job 도 안 붙어 있다. 그걸 고아라고 부르면
    10분마다 빨간 경보가 울리고, 늘 울리는 알람은 꺼진 알람이다.
    """
    return [i for i in provider.list_instances()
            if i.label.startswith(prefix)
            and not i.label.startswith(owned_prefix)
            and i.id not in known]
