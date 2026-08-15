"""
파킹 전용 컨트롤러.

추론 종료 후 로봇팔을 원점(INITIALIZE_POSITION)으로 되돌린다. 모든 축을 동시에
움직이면 파킹 궤적 중 그리퍼가 바닥을 긁는 문제가 있어, 단계별로 수행한다:

  1단계(lift): joint2(숄더)만 먼저 원점으로 보내 팔을 세워 그리퍼를 바닥에서 들어올림
               (나머지 축은 현재 위치 유지).
  2단계(full): 나머지 전체 축을 원점으로 보냄.

각 단계는 관절 위치 델타로 정지를 감지해 타임아웃 전이라도 팔이 멈추면 다음 단계로
넘어간다. 파킹 실행 전 관절 이상을 진단하고, 이상이 있으면 모션을 수행하지 않고 중단한다.

범용 원시동작은 ArmController에 있고, 여기서는 파킹 시퀀스만 조합한다.
"""

import logging
from typing import Any

from arm_controller import ArmController

logger = logging.getLogger(__name__)

# 1단계에서 먼저 원점으로 보내 그리퍼를 들어올릴 축
LIFT_JOINTS = ("joint2",)

# 정지 감지 파라미터
SETTLE_THRESHOLD = 0.5   # 정규화 스케일(-100~100). 이 미만 델타면 "안 움직임"
STABLE_COUNT = 3         # 연속 STABLE_COUNT회 정지 상태면 완료
POLL_INTERVAL = 0.1      # 폴링 간격(초)
LIFT_TIMEOUT = 8.0       # 1단계 최대 대기(초)
FULL_TIMEOUT = 10.0      # 2단계 최대 대기(초)


class ParkingController:
    """단계별 파킹 + 관절 이상 진단. 양팔(bi_piper_*)이면 두 팔을 **병렬로 각각**
    2단계 파킹한다 — 순차로 하면 한 팔이 다른 팔이 끝날 때까지 뻗은 채 기다린다."""

    def __init__(self, robot: Any):
        # robot 은 PiperFollower(.bus 노출) 또는 BiPiperFollower(.left_arm/.right_arm)
        self.robot = robot
        arms = [robot.left_arm, robot.right_arm] if hasattr(robot, "left_arm") else [robot]
        self.arms = [ArmController(a.bus) for a in arms]
        self.arm = self.arms[0]  # 기존 단팔 호출부 호환

    def run(self) -> bool:
        """파킹 수행. 정상 완료 시 True, 관절 이상으로 중단 시 False (하나라도)."""
        if len(self.arms) == 1:
            return self._run_one(self.arms[0])
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(len(self.arms), thread_name_prefix="park") as ex:
            return all(ex.map(self._run_one, self.arms))

    def _run_one(self, arm: "ArmController") -> bool:
        # 0. 건강 체크 — 이상이면 모션 없이 중단
        ok, diag = arm.check_healthy()
        if not ok:
            logger.warning(
                "관절 이상 감지 — 파킹 중단. err_code=0x%04X problems=%s",
                diag.get("err_code", 0), diag.get("problems", []),
            )
            for name, m in diag.get("motors", {}).items():
                if m.get("faults"):
                    logger.warning(
                        "  %s: faults=%s vol=%s motor_temp=%s foc_temp=%s",
                        name, m["faults"], m.get("vol"), m.get("motor_temp"), m.get("foc_temp"),
                    )
            return False

        from lerobot_robot_piper.motors.tables import INITIALIZE_POSITION
        home = INITIALIZE_POSITION

        # 1. 리프트: joint2만 원점, 나머지 현재값 유지 → 그리퍼를 바닥에서 들어올림
        lift_targets = {j: home[j] for j in LIFT_JOINTS if j in home}
        logger.info("Phase 1 (lift %s): raising gripper off the floor", ", ".join(LIFT_JOINTS))
        settled = arm.move_and_wait(
            lift_targets, hold_current=True,
            timeout=LIFT_TIMEOUT, threshold=SETTLE_THRESHOLD,
            stable_count=STABLE_COUNT, poll_interval=POLL_INTERVAL,
        )
        logger.info("Phase 1 %s", "settled" if settled else "timed out")

        # 2. 나머지 전체 축을 원점으로
        logger.info("Phase 2 (full home): moving all joints to origin")
        settled = arm.move_and_wait(
            home, hold_current=False,
            timeout=FULL_TIMEOUT, threshold=SETTLE_THRESHOLD,
            stable_count=STABLE_COUNT, poll_interval=POLL_INTERVAL,
        )
        logger.info("Phase 2 %s", "settled" if settled else "timed out")
        return True

    def disable_torque(self) -> None:
        for arm in self.arms:
            arm.disable_torque()
