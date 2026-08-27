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

import numpy as np

from piper_robot import kinematics as K
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
    FLOOR = "floor"                        # 바닥(또는 여유)을 뚫는 경로


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

    # 바닥 필터. 아래 `FloorConfig` 참고. 녹화 중에는 끌 수 있어야 하므로
    # (필터가 켜진 채 녹화하면 **실행되지 않은 동작이 데이터셋에 들어간다**)
    # 설정으로 뺀다. 기본은 켜짐이다.
    floor: "FloorConfig" = field(default_factory=lambda: FloorConfig())

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

    반환하는 이유는 **가장 강한 것 하나**다. 여러 개가 겹치면

        데드맨 > 유한성 > **바닥** > 변화량 > 범위

    순인데, 사람이 로그에서 보고 싶은 것이 그 순서이기 때문이다. 바닥이 변화량·
    범위보다 위인 이유: "팔이 테이블을 칠 뻔했다"가 "값을 잘랐다"보다 먼저 알아야
    할 일이다. 범위 밖 명령이 마침 바닥을 향하면 바닥으로 보고된다 — 둘 다 참이고,
    고쳐야 할 것은 그 명령 하나다.
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

    # ⚠ **범위·변화율을 자른 뒤에** 검사한다. 실제로 나갈 값이 이거라서다 —
    #   자르기 전 값을 검사하면 있지도 않을 경로를 막게 된다.
    #   줄이는 방향은 항상 `q_now` 쪽이라 변화율 상한을 다시 깨지 않는다.
    applied, floored = floor_guard(q_now, applied, cfg.floor)
    if floored:
        return applied, Reason.FLOOR

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


# ── 바닥 필터 (기구학) ────────────────────────────────────────────────────────
#
# 위 두 필터는 관절값만 본다. 이건 **팔이 공간 어디에 있는지**를 본다 —
# URDF FK 로 링크 자세를 구하고, 가장 낮은 점이 바닥 아래로 내려가는지 검사한다.
#
# ⚠ **끝점만 보면 안 된다.** 정책은 33ms 간격의 웨이포인트를 준다. 그 사이는 팔이
#   내부적으로 보간하므로, 시작과 끝이 안전해도 **중간 경로가 바닥을 뚫고 지날 수
#   있다.** 그래서 구간을 나눠 전부 검사한다(문서의 (a) 스윕 검사).


@dataclass(frozen=True)
class FloorConfig:
    """바닥 평면 하나. 자기충돌은 아직 아니다 (문서: 바닥면부터)."""

    enabled: bool = True

    # 어떤 링크도 이 높이 아래로 못 간다 (m, `base_link` 기준).
    #
    # 평면과 여유를 두 값으로 나누지 않는다 — 수평 평면 하나에서는 **둘의 합만
    # 의미가 있고**, 나눠 두면 어느 쪽을 고쳐야 하는지가 애매해진다.
    #
    # ⚠ **기본값이 0(장착면)이 아니다.** 실측: 네 데이터셋 84,065프레임을
    #   `tools/replay_safety.py` 로 통과시킨 발동률이다.
    #
    #       한계   bolt_two1  bolt_sort  min_jenga  min_cube
    #        0cm      4.38%      —          —          —
    #       -2cm      0.00%     6.28%      4.12%      2.19%
    #       -3cm      0.00%     0.26%      0.04%      0.00%
    #       -4cm      0.00%     0.00%      0.00%      0.00%   ← 기본값
    #
    #   즉 **정상 작업이 장착면보다 3cm 가까이 아래까지 내려간다.** 메시 기준
    #   실제 최저점은 덮개보다 0.68cm 높으므로 진짜로는 -2.6cm 쯤이다.
    #
    # ⚠ 이 3cm 이 무엇인지는 **아직 물리적으로 확인 안 됐다.** 둘 중 하나다:
    #     (a) 팔이 작업면보다 3cm 높은 판·브래킷 위에 얹혀 있다 → 정상
    #     (b) 캘리브레이션이나 URDF 원점이 그만큼 어긋나 있다 → 문서가 말한
    #         "가장 위험한 경우". 이 경우 필터의 절대 높이가 통째로 틀린 것이다.
    #   확인 전까지 이 값은 **실측 경계이지 물리적 사실이 아니다.**
    #
    #   설치마다 다르다. `tools/replay_safety.py` 로 자기 데이터에 대고 정한다.
    min_z: float = -0.04

    # `q_now → q_goal` 을 몇 등분해 검사하나.
    #
    # ⚠ **끝점만 보면 안 된다.** 정책은 33ms 간격의 웨이포인트를 준다. 그 사이는
    #   팔이 내부적으로 보간하므로, 시작과 끝이 안전해도 중간이 바닥을 뚫고 지날
    #   수 있다. 무작위 자세쌍 4000개에서 그런 경로가 8건 나왔다.
    #   (변화율 상한 20 안에서는 0건이었다 — 지금은 상한이 먼저 막아 준다는 뜻이지
    #   이 검사가 필요 없다는 뜻이 아니다. 상한을 올리면 바로 필요해진다.)
    sweep_steps: int = 8

    # 바닥 아래에 이미 들어가 있을 때, 위로 올라가는 명령은 허용한다.
    # ⚠ 이걸 끄면 **복구가 불가능해진다** — 팔이 바닥에 박힌 채 굳는다.
    allow_escape: bool = True


def _arm_vector(q: dict[str, float]) -> "np.ndarray | None":
    """정규화 dict → (6,) 배열. 팔 관절이 하나라도 없으면 None (검사 불가)."""
    try:
        return np.array([q[j] for j in K.ARM_JOINTS], dtype=float)
    except KeyError:
        return None


def floor_guard(q_now: dict[str, float], q_goal: dict[str, float],
                cfg: FloorConfig) -> tuple[dict[str, float], bool]:
    """바닥을 뚫는 명령을 **접촉 직전까지**로 줄인다. 순수 함수다.

    거부(정지)가 아니라 줄이기인 이유는 문서에 있다 — 거부하면 정책이 계속 바닥을
    향해 명령하는 동안 팔이 굳는다. 마지막 안전 지점까지 가면 불연속이 없고 자연히
    감속한다.

    반환: `(적용할 목표, 바꿨는가)`
    """
    if not cfg.enabled or not K.available():
        return q_goal, False
    now = _arm_vector(q_now)
    goal = _arm_vector(q_goal)
    if now is None or goal is None:
        return q_goal, False

    limit = cfg.min_z
    # 0 번째가 현재 자세, 마지막이 목표. 현재도 함께 재야 "이미 박혀 있음"을 안다.
    s = np.linspace(0.0, 1.0, cfg.sweep_steps + 1)
    path = now + s[:, None] * (goal - now)
    try:
        z = K.lowest_z(K.norm_to_rad(path))
    except Exception:
        # 기구학이 안 되면 **이 필터만** 빠진다. 범위·변화율·데드맨은 그대로다.
        return q_goal, False

    # ⚠ **끝점이 안전하다고 통과시키면 안 된다.** 그게 바로 이 필터가 막으려는
    #   경우다 — 양끝이 안전해도 중간이 바닥을 뚫고 지날 수 있다. `z.min()` 이다.
    bad = np.flatnonzero(z < limit)
    if len(bad) == 0:
        return q_goal, False                      # 경로 전체가 안전

    if bad[0] == 0:
        # 이미 한계 아래에 있다. 위로 가는 명령이면 통과시킨다 — 아니면 복구가 안 된다.
        if cfg.allow_escape and z[-1] > z[0]:
            return q_goal, False
        return dict(q_now), True

    # ⚠ **처음 걸리는 지점 직전**까지다. `z >= limit` 인 마지막 칸이 아니다 —
    #   경로가 내려갔다 올라오면 그 뒤 칸도 안전해 보이지만, 거기 가려면
    #   뚫는 구간을 지나야 한다.
    safe = int(bad[0]) - 1
    applied = dict(q_goal)
    for i, name in enumerate(K.ARM_JOINTS):
        applied[name] = float(path[safe, i])
    return applied, True
