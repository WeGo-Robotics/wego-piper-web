"""로봇팔 shm 발행/소비 — **robotd 자리를 임시로 메우는 코드** (refactor/robot-transport.md 3단계).

카메라와 **같은 순서**로 간다: 데몬을 쪼개기 전에 전송 계층부터 검증한다.
지금은 게이트웨이의 `robot_manager` 가 CAN 을 쥐고 있으므로, 그 위에 얇게 얹어
`/dev/shm` 세그먼트를 채운다. 4단계에서 이 파일의 내용이 그대로 robotd 로 이사한다.

```
  [robot_manager (CAN)]                    [LeRobot 프록시 드라이버]
        │  read_joints_normalized()              │
        ├──────────► piper.arm.<iface>.state ────┤ get_action()
        │                                         │
        │  JointCtrl/GripperCtrl                  │ set_action()
        └◄───────── piper.arm.<iface>.action ◄────┘
```

## 여기에 데드맨이 없는 이유

데드맨(소비자가 죽으면 팔을 **적극적으로** 세운다)은 4단계 robotd 의 몫이다.
다만 지금도 **소비자가 죽으면 명령이 끊긴다** — 이 브리지는 새 레코드가 올 때만
CAN 으로 보내므로, 마지막 명령을 반복하지 않는다. 직접 드라이버와 같은 수준이고
그 이상은 CAN 을 영구 소유하는 프로세스에서 해야 의미가 있다.
"""

import logging
import threading
import time

from app.core.joints import denormalize_all
from piper_shm import ActionReader, ArmSegmentError, StateWriter
from piper_shm import arm as A

logger = logging.getLogger(__name__)

# 상태 발행 주기. CAN 캐시 읽기라 왕복이 없어 100Hz 가 부담이 아니다.
# 소비자(30fps 제어)보다 충분히 빨라야 `read_new` 가 매 사이클 신선한 값을 받는다.
STATE_HZ = 100.0

# 명령 폴링 주기. 1kHz 면 최대 +1ms — 30fps 제어(33ms)의 3%다.
# 문제가 되면 eventfd/futex 로 µs 수준까지 내릴 수 있다 (문서 위험 #1).
ACTION_POLL_S = 0.001

# 에러 코드·제어 모드 갱신 주기. 화면용 진단 값이라 관절만큼 급하지 않다.
DIAG_PERIOD_S = 0.1

# 소비자가 명령 세그먼트를 만들기를 기다리는 주기. `/dev/shm` stat 한 번이라
# 촘촘해도 공짜다. **넉넉하게 잡으면 그만큼 첫 명령들이 버려진다** —
# 0.2s 였을 때 30fps 기준 첫 4개가 통째로 날아갔다.
ATTACH_POLL_S = 0.02


class ArmBridge:
    """팔 하나. 상태를 흘리고 명령을 받아 CAN 으로 보낸다."""

    def __init__(self, arm) -> None:
        self.arm = arm                    # robot_manager 의 `ArmInfo`
        self.iface = arm.iface
        self._state: StateWriter | None = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self.sent = 0                     # 진단용 — CAN 으로 보낸 명령 수
        self.published = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        # ⚠ 기동 순서: **상태를 먼저 연다.** 소비자는 상태 세그먼트가 있어야 연결되고,
        # 명령 세그먼트는 소비자가 만든다 — 있으면 "누가 조종 중"이라는 뜻이다.
        self._state = StateWriter(self.iface)
        self._running = True
        self._threads = [
            threading.Thread(target=self._publish_loop, daemon=True,
                             name=f"arm-state-{self.iface}"),
            threading.Thread(target=self._command_loop, daemon=True,
                             name=f"arm-cmd-{self.iface}"),
        ]
        for t in self._threads:
            t.start()
        logger.info("팔 shm 브리지 시작: %s", self.iface)

    def stop(self) -> None:
        # ⚠ **순서가 중요하다.** 스레드를 먼저 멈춘다 — 세그먼트를 먼저 닫으면
        # 그 사이 루프가 한 번 더 발행해 세그먼트를 되살린다 (rsd 에서 겪었다).
        self._running = False
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2)
        self._threads = []
        if self._state is not None:
            self._state.close()
            self._state = None
        # 소비자가 남긴 명령 세그먼트도 치운다 — 남으면 다음 기동에서
        # 죽은 소비자의 마지막 명령이 살아있는 것처럼 보인다
        A.unlink(A.segment_name(self.iface, A.KIND_ACTION))
        logger.info("팔 shm 브리지 정지: %s (발행 %d, 송신 %d)",
                    self.iface, self.published, self.sent)

    def _publish_loop(self) -> None:
        period = 1.0 / STATE_HZ
        next_diag = 0.0
        err_code = ctrl_mode = 0
        while self._running:
            t0 = time.monotonic()
            try:
                # 진단 필드는 핫패스가 아니다. 매 사이클 읽으면 100Hz 로 락을
                # 세 번씩 잡게 되고, 정작 급한 관절 읽기가 그만큼 늦어진다.
                if t0 >= next_diag:
                    err_code = int((self.arm.read_error() or {}).get("err_code") or 0)
                    # ⚠ `arm.ctrl_mode` 는 화면용 **문자열**("0x06")이다 — 정수는 이쪽이다
                    ctrl_mode = self.arm.refresh_ctrl_mode() or 0
                    next_diag = t0 + DIAG_PERIOD_S

                values = self.arm.read_joints_normalized()
                if values is not None and self._state is not None:
                    self._state.publish(values, err_code=err_code, ctrl_mode=ctrl_mode)
                    self.published += 1
            except Exception as exc:
                logger.warning("상태 발행 실패 (%s): %s", self.iface, exc)
                time.sleep(0.1)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _command_loop(self) -> None:
        """명령 세그먼트가 생기면 붙어서 소비한다.

        소비자가 붙기 전에는 세그먼트가 없다 — 그래서 계속 열어보고,
        소비자가 떠나면(세그먼트 삭제) 다시 기다린다.
        """
        reader: ActionReader | None = None
        while self._running:
            if reader is None:
                try:
                    reader = ActionReader(self.iface)
                    logger.info("소비자 접속 (%s)", self.iface)
                except ArmSegmentError:
                    time.sleep(ATTACH_POLL_S)
                    continue
            try:
                got = reader.read_new(timeout_s=0.2, poll_s=ACTION_POLL_S)
            except Exception as exc:
                logger.warning("명령 읽기 실패 (%s): %s", self.iface, exc)
                reader.close()
                reader = None
                continue
            if got is None:
                # 세그먼트가 사라졌으면 소비자가 떠난 것이다
                if not A.segment_path(A.segment_name(self.iface, A.KIND_ACTION)).exists():
                    logger.info("소비자 종료 (%s)", self.iface)
                    reader.close()
                    reader = None
                continue
            # 목표는 **절대 위치**라 중간 것을 건너뛰어도 무해하다 — 최신이 이긴다.
            # (누적 증분이었다면 건너뛴 만큼 팔이 덜 움직였을 것이다.)
            self._send(got["values"])
        if reader is not None:
            reader.close()

    def _send(self, values: dict[str, float]) -> None:
        """정규화 목표 → CAN. `PiperMotorsBus.set_action` 과 **같은 순서**로 보낸다."""
        raw = denormalize_all(values)
        piper = getattr(self.arm, "_piper", None)
        if piper is None:
            return
        try:
            with self.arm._lock:
                piper.ModeCtrl(0x01, 0x01, 30, 0x00)
                piper.JointCtrl(raw["joint1"], raw["joint2"], raw["joint3"],
                                raw["joint4"], raw["joint5"], raw["joint6"])
                piper.GripperCtrl(abs(raw["gripper"]), 1000, 0x03, 0)
            self.sent += 1
        except Exception as exc:
            logger.warning("CAN 송신 실패 (%s): %s", self.iface, exc)


class ArmBridgeManager:
    """인터페이스별 브리지. 게이트웨이가 추론·녹화 시작 전에 켠다."""

    def __init__(self) -> None:
        self.bridges: dict[str, ArmBridge] = {}

    def start(self, arm) -> ArmBridge:
        b = self.bridges.get(arm.iface)
        if b is None:
            b = self.bridges[arm.iface] = ArmBridge(arm)
        b.start()
        return b

    def stop(self, iface: str) -> None:
        b = self.bridges.pop(iface, None)
        if b is not None:
            b.stop()

    def stop_all(self) -> None:
        for iface in list(self.bridges):
            self.stop(iface)

    def sweep_stale(self) -> list[str]:
        """지난 프로세스가 남긴 팔 세그먼트 정리. **기동 시 한 번만.**

        남겨두면 소비자가 발행자 없는 세그먼트를 열고 **멈춘 자세**를 관측으로 받는다.
        """
        stale = [n for n in A.list_segments() if n not in self._live_names()]
        for name in stale:
            A.unlink(name)
        if stale:
            logger.info("남은 팔 세그먼트 %d개 정리: %s", len(stale), stale)
        return stale

    def _live_names(self) -> set[str]:
        return {
            A.segment_name(iface, kind)
            for iface, b in self.bridges.items() if b.running
            for kind in (A.KIND_STATE, A.KIND_ACTION)
        }


arm_bridge_manager = ArmBridgeManager()
