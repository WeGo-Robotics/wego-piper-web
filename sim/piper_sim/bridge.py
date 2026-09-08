"""SimArmBridge — World ↔ shm 팔 세그먼트 (robotd ArmBridge 와 같은 골격).

상태 100Hz 발행, action 세그먼트 소비(데드맨 → 그 자리에 서기), 명령은
**`piper_robot.safety.filter_goal` 을 통과**한다 — 바닥·범위·변화율이 시뮬에서도
산다. 그래서 필터 자체를 위험 없이 검증할 수 있다 (feature/sim-env.md §2).
"""

import logging
import threading
import time

from piper_robot.safety import Reason, SafetyConfig, filter_goal
from piper_shm import ActionReader, ArmSegmentError, StateWriter
from piper_shm import arm as A

logger = logging.getLogger(__name__)

STATE_HZ = 100.0
ACTION_POLL_S = 0.001
DEADMAN_CHECK_S = 0.05


class SimArmBridge:
    def __init__(self, arm_name: str, world, safety: SafetyConfig | None = None) -> None:
        self.arm_name = arm_name
        self.world = world
        self.safety = safety or SafetyConfig()
        self.published = 0
        self.sent = 0
        self.filtered = 0
        self.last_reason = Reason.OK
        self.lost_at = 0.0            # 시뮬 팔은 사라지지 않는다 — 계약상 자리만
        self.torque_on = True
        self._estopped = False
        self._deadman_held = False
        self._running = False
        self._threads: list[threading.Thread] = []
        self._state: StateWriter | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._state = StateWriter(self.arm_name)
        self._running = True
        self._threads = [
            threading.Thread(target=self._publish_loop, daemon=True, name=f"sim-state-{self.arm_name}"),
            threading.Thread(target=self._command_loop, daemon=True, name=f"sim-cmd-{self.arm_name}"),
        ]
        for t in self._threads:
            t.start()
        logger.info("시뮬 팔 브리지 시작: %s", self.arm_name)

    def stop(self) -> None:
        self._running = False
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2)
        self._threads = []
        if self._state is not None:
            self._state.close()
            self._state = None
        A.unlink(A.segment_name(self.arm_name, A.KIND_ACTION))
        logger.info("시뮬 팔 브리지 정지: %s (발행 %d, 송신 %d)", self.arm_name, self.published, self.sent)

    def estop(self) -> None:
        """E-stop — 그 자리에 서고 이후 명령을 무시한다 (release 까지)."""
        self._estopped = True
        self.world.hold()
        logger.warning("E-stop (%s): 정지·명령 무시", self.arm_name)

    def _publish_loop(self) -> None:
        period = 1.0 / STATE_HZ
        while self._running:
            t0 = time.monotonic()
            try:
                if self._state is not None:
                    self._state.publish(self.world.snapshot())
                    self.published += 1
            except Exception as exc:
                logger.warning("상태 발행 실패 (%s): %s", self.arm_name, exc)
                time.sleep(0.1)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _command_loop(self) -> None:
        reader: ActionReader | None = None
        while self._running:
            if reader is None:
                try:
                    reader = ActionReader(self.arm_name)
                    logger.info("소비자 접속 (%s)", self.arm_name)
                except ArmSegmentError:
                    time.sleep(0.02)
                    continue
            try:
                got = reader.read_new(timeout_s=DEADMAN_CHECK_S, poll_s=ACTION_POLL_S)
            except Exception as exc:
                logger.warning("명령 읽기 실패 (%s): %s", self.arm_name, exc)
                reader.close(); reader = None
                continue
            if got is None:
                if not A.segment_path(A.segment_name(self.arm_name, A.KIND_ACTION)).exists():
                    logger.info("소비자 종료 (%s)", self.arm_name)
                    reader.close(); reader = None
                    self._deadman_held = False
                    self.world.hold()
                    continue
                if reader.is_stale() and not self._deadman_held:
                    self._deadman_held = True
                    self.world.hold()
                    logger.warning("데드맨 (%s): %dms 동안 명령 없음 — 현재 자세로 정지",
                                   self.arm_name, reader.deadman_ms)
                continue
            if self._estopped:
                continue
            self._deadman_held = False
            self._send(got["values"])

    def _send(self, values: dict[str, float]) -> None:
        now = self.world.snapshot()
        goal, reason = filter_goal(now, values, self.safety, deadman_tripped=False)
        self.last_reason = reason
        if reason is not Reason.OK:
            self.filtered += 1
        self.world.set_goal(goal)
        self.sent += 1
