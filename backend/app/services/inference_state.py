"""지금 돌고 있는 추론이 **무엇인지** 기억한다.

`process_manager` 는 프로세스가 살아 있는지만 안다 — 어떤 정책인지, 어느
체크포인트인지는 명령줄 문자열 안에 묻혀 있다. 그걸 다시 파싱해서 알아내는
코드가 생기기 시작하면 인자 조립 규칙이 두 벌이 된다.

오케스트레이터가 이 값을 봐야 하는 이유:

  ACT 는 `task` 를 **안 쓴다** (`policies.takes_language`). 그런데 루프는
  판단 결과를 `task` 로만 내보낸다. 그래서 ACT 를 올려둔 채 루프를 돌리면
  LLM 이 매번 다른 판단을 내려도 로봇은 같은 동작만 반복하고, 저널에는 판단이
  꼬박꼬박 쌓인다 — **돌아가는 것처럼 보이는 실패**다.

정책을 여럿 올렸으면(feature/multi-policy.md) 이름 → 체크포인트와 지금 정책도 든다.
지금 정책은 **wrapper 텔레메트리가 확인해 준 값**이다(`note`) — 바꾸라고 보냈다고 바뀐 것이 아니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunningPolicy:
    policy_type: str
    checkpoint_path: str
    policies: dict[str, str] = field(default_factory=dict)   # 이름 → 체크포인트. 직접 편집한 인자면 비어 있다
    active: str = ""                                          # 지금 정책 이름
    holding: bool = False                                     # 마지막 액션 유지 중


_running: RunningPolicy | None = None


def set_running(policy_type: str, checkpoint_path: str,
                policies: dict[str, str] | None = None, active: str = "") -> None:
    global _running
    _running = RunningPolicy(policy_type=policy_type, checkpoint_path=checkpoint_path,
                             policies=dict(policies or {}), active=active)
    logger.info("추론 정책 기록: %s (%s)%s", policy_type, checkpoint_path,
                f" — 정책 {', '.join(_running.policies)}" if len(_running.policies) > 1 else "")


def note(active: str | None, holding: bool) -> None:
    """wrapper 텔레메트리가 알려 준 지금 정책·유지. 정책이 하나면 텔레메트리에 이름이 없다 — 그대로 둔다."""
    global _running
    cur = _running
    if cur is None:
        return
    name = active if active in cur.policies else cur.active
    if name != cur.active or holding != cur.holding:
        _running = replace(cur, active=name, holding=holding)
        if name != cur.active:
            logger.info("지금 정책: %s", name)


def clear() -> None:
    global _running
    _running = None


def get() -> RunningPolicy | None:
    """살아 있지 않으면 None.

    ⚠ **프로세스 상태를 여기서 같이 본다.** 기록만 남기면 추론이 죽은 뒤에도
    "무엇이 돌고 있다"고 말하게 된다 — 그 거짓말이 곧 잘못된 판단으로 이어진다.
    """
    if _running is None:
        return None
    from app.services.exclusivity import Activity, is_running

    return _running if is_running(Activity.INFERENCE) else None
