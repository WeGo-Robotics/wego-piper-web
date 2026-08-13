"""CAN 대신 `/dev/shm` 으로 말하는 모터 버스 (refactor/robot-transport.md).

## 왜 `Robot` 이 아니라 여기서 자르나

`PiperFollower`(175줄)는 이미 얇고, 실제 CAN 접근은 전부 `self.bus` 에 있다.
`Robot` 층에서 자르면 feature dict 구성·카메라 병렬 읽기·안전 클램핑까지 전부
다시 구현해야 하고 그 순간부터 계약이 드리프트한다. **버스에서 자르면
`PiperFollower` 를 그대로 재사용**하므로 `observation_features`/`action_features`
가 `bus.motors` 에서 파생되어 자동으로 일치한다.

## 여기에 캘리브레이션이 없는 이유

캘리브레이션의 단일 소유자는 robotd 다. 프록시가 자기 것을 들면
`05-joint-calibration.md` 가 지적한 중복이 2곳에서 3곳으로 는다.
shm 에는 **정규화된 값만** 흐르므로 `_normalize`/`_unnormalize` 는 항등이다.

## 블로킹이 없다

원래 `PiperMotorsBus` 도 CAN 을 기다리지 않는다 — `GetArmJointMsgs()` 는
piper_sdk 백그라운드 스레드가 채운 **캐시 읽기**이고 `JointCtrl()` 은
fire-and-forget 송신이다. 그래서 그대로 shm 으로 옮길 수 있다:
상태는 최신 슬롯 복사, 명령은 슬롯에 쓰고 seq 증가.
"""

import logging

from lerobot.motors import Motor, MotorCalibration
from lerobot.motors.motors_bus import MotorsBusBase
from piper_shm import JOINTS, ActionWriter, ArmSegmentError, StateReader

logger = logging.getLogger(__name__)

# 소비자가 선언하는 자기 제어 주기의 상한. robotd 는 이 시간 동안 명령 seq 가
# 안 늘면 **소비자가 죽었다고 보고 팔을 세운다.**
# ⚠ 30fps 제어(33ms)의 여유를 두되 너무 길면 데드맨의 의미가 없다.
DEFAULT_DEADMAN_MS = 300

# 상태가 이보다 오래되면 읽기를 실패로 본다. robotd 가 죽었는데 마지막 자세를
# 계속 돌려주면 **정책이 멈춘 팔을 움직이는 팔로 착각한다.**
STALE_STATE_S = 1.0


class PiperShmMotorsBus(MotorsBusBase):
    """`piper-robotd` 와 `/dev/shm` 으로 통신하는 프록시 버스.

    메서드 이름·시그니처는 `PiperMotorsBus` 그대로다 — `PiperFollower` 가
    그대로 이 객체를 쓰기 때문이다.
    """

    apply_drive_mode = False

    def __init__(self, id: str, port: str, motors: dict[str, Motor],
                 calibration: dict[str, MotorCalibration] | None = None,
                 deadman_ms: int = DEFAULT_DEADMAN_MS) -> None:
        super().__init__(port, motors, calibration)
        self.id = id
        self.iface = port           # 여기서 port 는 CAN 인터페이스 이름이다
        self.deadman_ms = deadman_ms
        self._state: StateReader | None = None
        self._action: ActionWriter | None = None
        self._last_goal: dict[str, float] = {}

    def __str__(self) -> str:
        return f"{self.id} PiperShmMotorsBus({self.iface})"

    # ---- MotorsBusBase 필수 구현 ----

    @property
    def is_connected(self) -> bool:
        return self._state is not None and self._action is not None

    def connect(self, handshake: bool = True) -> None:
        """robotd 의 상태 세그먼트를 열고 명령 세그먼트를 만든다.

        ⚠ **상태 세그먼트가 없으면 여기서 실패해야 한다.** 조용히 넘어가면
        정책이 0 자세(정규화 좌표의 "가운데")를 관측으로 받고, 그건 그럴듯해 보인다.
        """
        try:
            self._state = StateReader(self.iface)
        except ArmSegmentError as exc:
            raise ConnectionError(f"{self}: robotd 상태를 열 수 없습니다 — {exc}") from exc
        # 명령 세그먼트는 소비자가 만든다 — **존재 자체가 "누가 조종 중"이라는 뜻**이다
        self._action = ActionWriter(self.iface, deadman_ms=self.deadman_ms)
        logger.info("%s connected (deadman %dms)", self, self.deadman_ms)

    def disconnect(self, disable_torque: bool = True) -> None:
        """명령 세그먼트를 지운다 = 조종을 놓는다.

        `disable_torque` 는 여기서 처리하지 않는다 — 토크는 CAN 을 쥔 robotd 의
        권한이고, 세그먼트가 사라지면 robotd 가 데드맨으로 알아서 정지시킨다.
        """
        if self._action is not None:
            self._action.close()
            self._action = None
        if self._state is not None:
            self._state.close()
            self._state = None

    def read(self, data_name: str, motor: str) -> int | float:
        return self.get_action().get(motor, 0)

    def write(self, data_name: str, motor: str, value: int | float) -> None:
        current = self.get_action()
        current[motor] = value
        self.set_action(current, is_conv=True)

    def sync_read(self, data_name: str,
                  motors: str | list[str] | None = None) -> dict[str, int | float]:
        pos = self.get_action()
        if motors is None:
            return pos
        if isinstance(motors, str):
            motors = [motors]
        return {m: pos[m] for m in motors if m in pos}

    def sync_write(self, data_name: str, values: dict[str, int | float]) -> None:
        self.set_action(values, is_conv=True)

    def enable_torque(self, motors=None, num_retry: int = 0) -> None:
        """robotd 가 CAN 을 영구 소유하므로 토크는 이미 켜져 있다.

        여기서 켜려면 CAN 을 열어야 하고, 그러면 프록시의 존재 이유가 없어진다.
        토크 제어는 버스 RPC(Redis) 로 간다 — 핫패스가 아니다.
        """
        logger.debug("%s: enable_torque 는 robotd 소관이라 건너뛴다", self)

    def disable_torque(self, motors=None, num_retry: int = 0) -> None:
        logger.debug("%s: disable_torque 는 robotd 소관이라 건너뛴다", self)

    def read_calibration(self) -> dict[str, MotorCalibration]:
        return self.calibration

    def write_calibration(self, calibration_dict, cache: bool = True) -> None:
        self.calibration = calibration_dict

    # ---- Piper 고유 ----

    @property
    def is_calibrated(self) -> bool:
        return True

    def clear_gripper(self) -> None:
        logger.debug("%s: clear_gripper 는 robotd 소관이라 건너뛴다", self)

    def parking(self) -> None:
        """파킹을 **robotd 에 시킨다.**

        프록시가 흉내내면 "다 왔는지"를 모르는 채 명령만 반복하게 된다 —
        완료 판정에 CAN 상태 폴링이 필요하고 그건 CAN 을 쥔 쪽만 할 수 있다.

        실패해도 예외를 올리지 않는다: 추론 종료 경로에서 불리는데, 여기서 죽으면
        토크 해제·정리가 통째로 건너뛰어진다.
        """
        try:
            from piper_bus import contract as C
            from piper_bus.client import Bus

            Bus().rpc_call(C.ROBOTD, "go_parking", [self.iface], timeout=30)
        except Exception as exc:
            logger.warning("%s: robotd 파킹 실패 — %s", self, exc)

    def set_slave(self) -> None:
        logger.debug("%s: set_slave 는 robotd 소관이라 건너뛴다", self)

    def set_master(self) -> None:
        logger.debug("%s: set_master 는 robotd 소관이라 건너뛴다", self)

    def get_action(self) -> dict[str, float]:
        """현재 관절 상태(정규화). robotd 가 발행한 최신 레코드를 읽는다."""
        if self._state is None:
            raise ConnectionError(f"{self} is not connected.")
        got = self._state.read()
        if got is None:
            raise ConnectionError(f"{self}: robotd 가 아직 상태를 발행하지 않았습니다")
        age = self._state.age_s()
        if age > STALE_STATE_S:
            # 멈춘 값을 돌려주면 정책이 **움직이지 않는 팔을 움직인다고 믿는다**
            raise ConnectionError(f"{self}: 상태가 {age:.1f}s 묵었습니다 (robotd 가 살아 있나요?)")
        return got["values"]

    def get_control(self) -> dict[str, float]:
        """마지막으로 보낸 목표값. CAN 왕복 없이 우리가 쓴 것을 그대로 돌려준다."""
        return dict(self._last_goal)

    def set_action(self, action: dict[str, float], is_conv: bool = True) -> dict[str, float]:
        """목표 위치를 명령 세그먼트에 쓴다. **fire-and-forget 이다.**

        `is_conv` 는 원래 정규화 여부 플래그인데, shm 에는 정규화 값만 흐르므로
        어느 쪽이든 그대로 쓴다. 인자를 남겨 두는 건 `PiperFollower` 가
        `is_conv=True` 로 부르기 때문이다.
        """
        if self._action is None:
            raise ConnectionError(f"{self} is not connected.")
        # 빠진 관절은 현재 상태로 채운다 — 0으로 채우면 팔이 "가운데"로 튄다
        goal = {**self.get_action(), **{k: float(v) for k, v in action.items() if k in JOINTS}}
        self._action.publish(goal)
        self._last_goal = goal
        return goal
