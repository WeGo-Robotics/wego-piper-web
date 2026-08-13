"""robotd 안전층 — **순수 함수** (refactor/robotd-safety.md).

## 왜 순수 함수여야 하나

문서가 이걸 핵심 설계 속성으로 못 박았다. 두 가지가 걸려 있다:

1. **하드웨어 없이 경계 조건을 시험한다.** 안전 로직을 실제 팔로 검증하는 건
   그 자체가 위험하다.
2. **기존 데이터셋에 리플레이할 수 있다.** 이미 녹화한 에피소드를 통과시켜
   "켰다면 몇 %의 프레임에서 발동했을까"를 **로봇을 켜기 전에** 측정한다.
   발동률이 높으면 필터가 아니라 캘리브레이션·지오메트리가 틀린 것이고,
   그걸 실제 팔로 알아내면 위험하다.

## 여기 없는 것 — 기구학 필터

바닥면·자기충돌 방지는 URDF 가 있어야 한다. 저장소에 URDF·메시가 하나도 없어서
(별도 트랙 E의 선결 조건) 이 파일에는 **URDF 가 필요 없는 두 가지만** 넣는다:

- **하드 관절 리밋** — 범위 + 한 스텝 최대 변화량
- **데드맨** — 소비자가 죽거나 멈추면 정지

기구학이 들어와도 이 파일의 계약(`filter(q_now, q_goal, cfg) -> (q_applied, reason)`)은
그대로다. 필터가 하나 늘 뿐이다.

## 왜 robotd 인가 — 명령자가 넷이다

팔에 목표를 주는 주체가 넷이다: 정책 추론, 텔레오퍼레이션 녹화, 웹 수동 제어, 파킹.
필터를 프록시 드라이버에 두면 **LeRobot 경로만 보호되고 나머지 셋은 무방비**다.
robotd 에 두면 CAN 으로 나가는 모든 명령이 한 곳을 통과한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from piper_robot.joints import JOINT_ORDER

# 정규화 좌표의 절대 한계. 캘리브레이션 범위가 그대로 -100..100 (그리퍼 0..100) 이므로
# 이 밖은 물리적으로 도달 불가능한 값이다.
NORM_MIN = -100.0
NORM_MAX = 100.0
GRIPPER_MIN = 0.0
GRIPPER_MAX = 100.0

# 범위를 벗어났다고 **보고할** 최소 크기.
# ⚠ 팔이 관절 한계에 앉아 있는 건 정상이고 흔하다(파킹 자세가 그렇다). 그 값이
# shm 을 float32 로 왕복하면 ±100 이 100.0000076 으로 돌아와 매 프레임
# `clamped_range` 가 뜬다 — 진짜 위반이 그 소음에 묻힌다.
# 자르기는 그대로 하되, 이 이하는 조용히 자른다. 실제 위반은 자릿수가 다르다.
RANGE_REPORT_EPS = 1e-3


class Reason(str, Enum):
    """명령이 왜 변형됐는지. 텔레메트리로 나가고 사람이 읽는다."""

    OK = "ok"
    CLAMPED_RANGE = "clamped_range"        # **명령**이 관절 범위를 벗어남
    STATE_OUT_OF_RANGE = "state_out_of_range"  # **현재 자세**가 이미 범위 밖
    CLAMPED_RATE = "clamped_rate"          # 한 스텝 변화량이 너무 큼
    DEADMAN = "deadman"                    # 소비자가 죽었다 — 현재 자세 유지
    NOT_FINITE = "not_finite"              # NaN/inf 가 들어왔다


@dataclass(frozen=True)
class SafetyConfig:
    """robotd 하드 리밋. **프록시(컨테이너 안)를 신뢰하지 않는다.**

    LeRobot 의 `max_relative_target` 클램프는 소비자 프로세스 안에 있어서, 그게
    오작동하면 무방비다. 여기 것은 CAN 을 쥔 프로세스 안에 있으므로 최종 방어선이다.
    이중이지만 그게 요점이다.
    """

    # 한 스텝(제어 주기 1회)에 허용할 최대 정규화 변화량.
    # ⚠ 너무 조이면 정상적인 빠른 동작이 잘리고, 너무 풀면 방어선이 아니다.
    # 30fps 기준 20이면 전 구간(200)을 약 0.33초에 지나가는 속도다 — 충분히 빠르되,
    # 정책이 어긋난 관측으로 팔을 한 프레임에 날려보내는 것은 막는다.
    max_step: float = 20.0

    # 관절별 예외. **그리퍼는 기본이 무제한이다** — 여닫는 게 원래 빠른 동작이라
    # 제한을 걸면 파지가 굼떠지고, 팔과 달리 크게 휘두를 물건도 아니다.
    max_step_per_joint: dict[str, float] = field(
        default_factory=lambda: {"gripper": 0.0}
    )

    # 데드맨: 명령이 이 시간 넘게 안 오면 소비자가 죽은 것으로 본다.
    # 0이면 끈다 — **판정 불가와 안전을 헷갈리면 안 된다** (텔레오퍼레이션처럼
    # 사람이 보고 있는 경우를 위해 남긴다).
    deadman_ms: int = 300

    # 관절 범위 클램프. 끌 수 없다 — 범위 밖은 물리적으로 도달 불가능한 값이라
    # 그대로 보내면 SDK 가 무엇을 할지 모른다.

    def step_limit(self, joint: str) -> float:
        return self.max_step_per_joint.get(joint, self.max_step)


def clamp_range(joint: str, value: float) -> tuple[float, bool]:
    """정규화 값을 관절 범위로. `(값, **보고할 만큼** 잘렸는가)`.

    두 번째 값이 "잘렸는가"가 아니라 "보고할 만큼 잘렸는가"인 이유는
    `RANGE_REPORT_EPS` 주석에 있다.
    """
    lo, hi = (GRIPPER_MIN, GRIPPER_MAX) if joint == "gripper" else (NORM_MIN, NORM_MAX)
    if value < lo:
        return lo, (lo - value) > RANGE_REPORT_EPS
    if value > hi:
        return hi, (value - hi) > RANGE_REPORT_EPS
    return value, False


def filter_goal(q_now: dict[str, float], q_goal: dict[str, float],
                cfg: SafetyConfig, *, deadman_tripped: bool = False,
                ) -> tuple[dict[str, float], Reason]:
    """목표를 안전하게 만든다. **하드웨어 없이 호출 가능하고 부작용이 없다.**

    데드맨이 걸리면 **현재 자세를 유지**한다. 토크를 끊지 않는 이유는 그게 더
    위험하기 때문이다 — 팔이 중력으로 떨어진다. 정지 = 그 자리에 서기다.

    반환하는 이유는 **가장 강한 것 하나**다. 여러 개가 겹치면 데드맨 > 유한성 >
    변화량 > 범위 순인데, 사람이 로그에서 보고 싶은 것이 그 순서이기 때문이다.
    """
    if deadman_tripped:
        return dict(q_now), Reason.DEADMAN

    applied: dict[str, float] = {}
    clamped_rate = clamped_range = not_finite = False

    for joint in JOINT_ORDER:
        if joint not in q_goal:
            # 안 온 관절은 **현재 자세를 유지**한다. 0으로 채우면 정규화 좌표의
            # "가운데"라 그럴듯해 보이고, 그게 명령이 되면 팔이 튄다.
            applied[joint] = q_now.get(joint, 0.0)
            continue

        want = q_goal[joint]
        now = q_now.get(joint)
        # NaN/inf 는 클램프를 그냥 통과한다(비교가 전부 False) — 먼저 걸러낸다.
        if not _finite(want):
            not_finite = True
            applied[joint] = now if now is not None and _finite(now) else 0.0
            continue

        if now is not None and _finite(now):
            limit = cfg.step_limit(joint)
            if limit > 0 and abs(want - now) > limit:
                want = now + limit * (1.0 if want > now else -1.0)
                clamped_rate = True

        want, hit = clamp_range(joint, want)
        clamped_range = clamped_range or hit
        applied[joint] = want

    if not_finite:
        return applied, Reason.NOT_FINITE
    if clamped_rate:
        return applied, Reason.CLAMPED_RATE
    if clamped_range:
        # ⚠ **원인이 둘인데 증상이 같다.** 명령이 이상한 것과 캘리브레이션이 좁은 것을
        # 한 사유로 뭉뚱그리면, 정책을 의심해야 할지 캘리브레이션을 의심해야 할지
        # 로그만 보고는 알 수 없다.
        #
        # 실제로 겪었다: joint3 의 캘리브레이션 최대가 0 인데 팔이 raw 2103 에 앉아
        # 있어서, 현재 자세를 그대로 되보내는 정상 명령마다 범위 위반이 떴다.
        if _any_out_of_range(q_now):
            return applied, Reason.STATE_OUT_OF_RANGE
        return applied, Reason.CLAMPED_RANGE
    return applied, Reason.OK


def _any_out_of_range(q: dict[str, float]) -> bool:
    return any(clamp_range(j, v)[1] for j, v in q.items() if j in JOINT_ORDER)


def _finite(v: float) -> bool:
    # NaN 은 자기 자신과 다르고, inf 는 유한 비교로 걸러진다.
    return v == v and -1e30 < v < 1e30
