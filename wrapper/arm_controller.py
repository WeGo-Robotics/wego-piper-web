"""
로봇팔 모션 컨트롤러 (재사용 가능한 추상 계층).

파킹 같은 특정 시퀀스를 모르는 범용 원시동작만 제공한다:
  - 관절 상태 읽기 / 상세 진단 / 건강 체크
  - 목표 위치로 이동(지정 축만, 나머지는 현재값 유지 가능)
  - 관절 위치 델타 기반 정지 감지 (타임아웃 전 조기 종료)
  - 토크 차단

PiperMotorsBus(lerobot_robot_piper)를 주입받아 동작한다. 파킹 외 다른 특수
동작(warmup 포즈 등)도 이 위에서 조합할 수 있다.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# GetArmStatus().arm_status.err_code 비트 → 의미 (백엔드 robot_manager._ERR_BITS와 동일)
_ERR_BITS = {
    0: "joint1_comm", 1: "joint2_comm", 2: "joint3_comm",
    3: "joint4_comm", 4: "joint5_comm", 5: "joint6_comm",
    8: "joint1_angle_limit", 9: "joint2_angle_limit", 10: "joint3_angle_limit",
    11: "joint4_angle_limit", 12: "joint5_angle_limit", 13: "joint6_angle_limit",
}

# LowSpd 피드백 foc_status의 이상 플래그(정상 시 모두 0/False 여야 함).
# driver_enable_status는 이상이 아니라 상태이므로 제외한다.
_FAULT_FLAGS = (
    "driver_error_status",
    "driver_overcurrent",
    "driver_overheating",
    "motor_overheating",
    "stall_status",
    "voltage_too_low",
    "collision_status",
)


class ArmController:
    """PiperMotorsBus를 감싸는 얇은 모션/진단 래퍼."""

    def __init__(self, bus: Any):
        # bus는 get_action / set_action / disable_torque / piper 를 노출한다.
        self.bus = bus

    # ── 상태/진단 ──

    def read_joints(self) -> dict[str, float] | None:
        """정규화된 현재 관절 위치(joint1~6 + gripper). 실패 시 None."""
        try:
            return self.bus.get_action()
        except Exception as e:
            logger.debug("read_joints failed: %s", e)
            return None

    def read_diagnostics(self) -> dict:
        """관절별 이상 상태를 상세 진단한다.

        반환: {
          "err_code": int, "err_flags": [str, ...],
          "motors": { "motor_1": {"vol","motor_temp","foc_temp","faults":[...]}, ... },
        }
        일부 필드 읽기 실패는 무시한다(best-effort).
        """
        # ⚠ shm 프록시 버스에는 SDK 핸들이 없다 — CAN 은 robotd 가 쥔다.
        # 원래 best-effort 진단이므로 없으면 빈 결과로 넘어간다.
        # 이게 없으면 파킹이 `AttributeError` 로 죽는다(실제로 겪었다).
        piper = getattr(self.bus, "piper", None)
        diag: dict = {"err_code": 0, "err_flags": [], "motors": {}}
        if piper is None:
            return diag

        try:
            err_code = int(piper.GetArmStatus().arm_status.err_code)
            diag["err_code"] = err_code
            diag["err_flags"] = [name for bit, name in _ERR_BITS.items() if err_code & (1 << bit)]
        except Exception as e:
            logger.debug("read err_code failed: %s", e)

        try:
            low = piper.GetArmLowSpdInfoMsgs()
            for i in range(1, 7):
                m = getattr(low, f"motor_{i}", None)
                if m is None:
                    continue
                foc = getattr(m, "foc_status", None)
                faults = []
                if foc is not None:
                    faults = [f for f in _FAULT_FLAGS if getattr(foc, f, 0)]
                diag["motors"][f"motor_{i}"] = {
                    "vol": getattr(m, "vol", None),
                    "motor_temp": getattr(m, "motor_temp", None),
                    "foc_temp": getattr(m, "foc_temp", None),
                    "faults": faults,
                }
        except Exception as e:
            logger.debug("read low-spd info failed: %s", e)

        return diag

    def check_healthy(self) -> tuple[bool, dict]:
        """진단 결과로 건강 여부 판정.

        err_code≠0 이거나 어떤 관절에 이상 플래그가 있으면 (False, diag).
        피드백이 아직 안 들어와 필드가 비어있는 경우는 이상으로 보지 않는다
        (파킹을 잘못 막지 않기 위한 안전한 기본값).
        """
        diag = self.read_diagnostics()
        problems = list(diag["err_flags"])
        for name, m in diag["motors"].items():
            for f in m.get("faults", []):
                problems.append(f"{name}_{f}")
        diag["problems"] = problems
        return (len(problems) == 0, diag)

    # ── 모션 ──

    def move_to(self, targets: dict[str, float], hold_current: bool = True) -> dict[str, float]:
        """목표 위치로 1회 전송하고, 실제로 보낸 action(고정 목표)을 반환한다.

        hold_current=True면 현재 위치에 targets만 덮어써서 지정 축만 이동하고
        나머지는 유지한다. False면 targets를 그대로 사용한다(7축 모두 포함해야 함).
        """
        if hold_current:
            action = self.read_joints() or {}
            action.update(targets)
        else:
            action = dict(targets)
        self.bus.set_action(action, is_conv=True)
        return action

    def wait_until_settled(
        self,
        resend_action: dict[str, float] | None = None,
        timeout: float = 10.0,
        threshold: float = 0.5,
        stable_count: int = 3,
        poll_interval: float = 0.1,
    ) -> bool:
        """관절 위치 델타가 threshold 미만인 상태가 stable_count회 연속되면 정지로 판정(True).

        timeout(초) 초과 시 False. resend_action이 주어지면 매 폴링마다 같은 고정
        목표를 재전송한다(도달 보장; 기존 parking() 재전송 패턴).
        """
        prev = self.read_joints()
        stable = 0
        elapsed = 0.0
        while elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            if resend_action is not None:
                try:
                    self.bus.set_action(resend_action, is_conv=True)
                except Exception as e:
                    logger.debug("resend during settle failed: %s", e)
            cur = self.read_joints()
            if prev and cur:
                delta = max((abs(cur[k] - prev[k]) for k in cur if k in prev), default=0.0)
                if delta < threshold:
                    stable += 1
                    if stable >= stable_count:
                        return True
                else:
                    stable = 0
            if cur:
                prev = cur
        return False

    def move_and_wait(
        self,
        targets: dict[str, float],
        hold_current: bool = True,
        **settle_kwargs: Any,
    ) -> bool:
        """targets로 이동한 뒤 정지할 때까지 대기. 정지 감지 시 True, 타임아웃 시 False."""
        action = self.move_to(targets, hold_current=hold_current)
        return self.wait_until_settled(resend_action=action, **settle_kwargs)

    # ── 토크 ──

    def disable_torque(self) -> None:
        try:
            self.bus.disable_torque()
        except Exception as e:
            logger.warning("disable_torque failed: %s", e)
