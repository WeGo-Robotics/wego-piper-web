"""로봇팔 shm 발행/소비 — robotd 의 핫패스 (refactor/robot-transport.md 4단계).

게이트웨이의 `arm_bridge.py` 에서 그대로 옮겨왔다. 거기서 먼저 돌려본 이유는
카메라와 같다 — **데몬을 쪼개기 전에 전송 계층부터 검증**했다.

```
  [robotd (CAN 독점)]                      [LeRobot 프록시 드라이버]
        │  read_joints_normalized()              │
        ├──────────► piper.arm.<iface>.state ────┤ get_action()
        │                                         │
        │  JointCtrl/GripperCtrl                  │ set_action()
        └◄───────── piper.arm.<iface>.action ◄────┘
```

## 안전층은 여기 있다

CAN 으로 나가는 **모든** 명령이 `piper_robot.safety.filter_goal` 을 통과한다.
필터를 프록시 드라이버에 두면 LeRobot 경로만 보호되고 나머지 셋(웹 수동 제어,
파킹, 텔레오퍼레이션)은 무방비다 — 그래서 CAN 을 쥔 쪽에 둔다.

데드맨은 **소비자가 선언한 `deadman_ms`** 로 판정한다. 걸리면 명령을 버리고
현재 자세를 유지한다. 토크를 끊지 않는 이유는 그게 더 위험하기 때문이다 —
팔이 중력으로 떨어진다. 정지 = 그 자리에 서기다.
"""

import logging
import threading
import time

from piper_robot.can import iface_exists
from piper_robot.joints import denormalize_all
from dataclasses import replace

from piper_robot import load_store, safety_store
from piper_robot.load import LoadLimits, LoadWatch, describe as describe_load
from piper_robot.safety import (
    FloorConfig, Reason, SafetyConfig, filter_goal,
)
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

# 관절 부하(전류·토크) 표본 주기. 20Hz — `load.DEFAULT_DWELL_S`(0.30초) 안에
# 여섯 표본이 들어가므로 지속 판정이 표본 하나에 좌우되지 않는다.
#
# ⚠ **100Hz 로 올리지 마라.** 발행 루프가 이미 매 사이클 팔 락을 잡는데, 여기서
#   한 번 더 잡으면 정작 급한 관절 읽기가 그만큼 밀린다. 대신 표본 사이의 피크는
#   놓친다 — 우리가 쫓는 준정적 과부하는 초 단위라 문제가 안 되지만, 충돌 순간의
#   최대 토크를 재는 용도로는 이 값이 부족하다는 뜻이기도 하다.
LOAD_PERIOD_S = 0.05

# 소비자가 명령 세그먼트를 만들기를 기다리는 주기. `/dev/shm` stat 한 번이라
# 촘촘해도 공짜다. **넉넉하게 잡으면 그만큼 첫 명령들이 버려진다** —
# 0.2s 였을 때 30fps 기준 첫 4개가 통째로 날아갔다.
ATTACH_POLL_S = 0.02

# 명령이 없을 때 데드맨을 확인하는 주기. **이만큼이 정지까지의 추가 지연이다** —
# `deadman_ms` 300 에 이 값이 더해져 최악 350ms 안에 팔이 선다.
DEADMAN_CHECK_S = 0.05


class ArmBridge:
    """팔 하나. 상태를 흘리고 명령을 받아 CAN 으로 보낸다."""

    def __init__(self, arm, safety: SafetyConfig | None = None) -> None:
        self.arm = arm                    # `piper_robot.arm.Arm`
        self.iface = arm.iface
        # 팔이 사라졌다고 **데몬이 판정한** 시각. 게이트웨이가 세그먼트 나이로
        # 추론하지 않게 하려는 것이다 (`lost()` RPC 로 나간다).
        self.lost_at: float = 0.0
        self.safety = safety or SafetyConfig()
        self._state: StateWriter | None = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self.sent = 0                     # 진단용 — CAN 으로 보낸 명령 수
        self.published = 0
        self.filtered = 0                 # 안전층이 명령을 바꾼 횟수
        self.last_reason = Reason.OK
        #: 직전 CAN 오류 카운터. 증가분만 로그로 낸다 (절대값은 재열거에서 0이 된다).
        self._err_counters: dict | None = None
        self._deadman_held = False
        self._last_logged = Reason.OK
        #: 관절 부하 감시 (층 1). ⚠ **소비자가 붙어 있는지와 무관하게 돈다** —
        #: 슬립은 조그·파킹·텔레옵 중에도 나고, 그때가 오히려 최다 트리거다.
        self.load = LoadWatch(name=arm.iface)

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        # ⚠ **판정을 지우고 시작한다.** 발행을 다시 여는 것은 팔이 돌아왔다는
        # 결정적 증거다 — `stop()` 이 지우는 것과 같은 근거인데 이쪽만 빠져 있었다.
        # 안 지우면 `lost()` 가 재연결 뒤에도 계속 사라졌다고 말한다: 실기에서
        # 아침 09:26 의 USB 뽑힘이 11시간 뒤까지 "USB 를 확인하세요" 로 떴고,
        # 그 경보가 `mark_absent()` 를 불러 멀쩡한 팔의 연결까지 내렸다.
        self.lost_at = 0.0
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
        # 사용자가 일부러 끊는 것 — 사라진 게 아니므로 판정을 지운다
        self.lost_at = 0.0
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

    # CAN 인터페이스가 아직 있는지 보는 주기. `/sys/class/net` 조회라 사실상 공짜다.
    PRESENCE_S = 1.0
    # 인터페이스는 남아 있는데 읽기만 계속 실패하는 경우의 상한 (100Hz 기준 ~2초).
    MAX_READ_FAILS = 200

    #: CAN 오류 카운터를 다시 볼 간격 (초). `ip` 호출이 3~4ms 라 자주 부르면 안 된다.
    ERR_SAMPLE_S = 10.0

    def _sample_can_errors(self) -> None:
        """오류 카운터가 **올랐을 때만** 로그에 남긴다.

        ⚠ `can_state()` 는 *지금* 나쁜지만 본다. 잠깐 error-passive 로 내려갔다
          돌아오는 버스는 물어보는 순간마다 늘 ERROR-ACTIVE 라 영영 안 잡힌다 —
          실측(can3)에서 누적 **34,794회**였는데 상태 조회로는 한 번도 안 걸렸다.

          "통신이 좀 불안정한 것 같다" 를 숫자로 바꾸는 자리다. 절대값은 인터페이스를
          다시 열면 0 이 되므로 **증가분만** 뜻이 있다.
        """
        from piper_robot.can import error_counters

        now = error_counters(self.iface)
        if not now:
            return
        before = self._err_counters
        self._err_counters = now
        if before is None:
            return
        grew = {k: now[k] - before[k] for k in now if now[k] > before.get(k, 0)}
        if grew:
            logger.warning("CAN 오류가 늘었습니다 (%s, 최근 %.0f초): %s — "
                           "케이블·종단저항·허브를 보세요",
                           self.iface, self.ERR_SAMPLE_S,
                           ", ".join(f"{k} +{v}" for k, v in sorted(grew.items())))

    def _publish_loop(self) -> None:
        period = 1.0 / STATE_HZ
        next_diag = 0.0
        next_presence = 0.0
        next_err = 0.0
        next_load = 0.0
        fails = 0
        err_code = ctrl_mode = 0
        while self._running:
            t0 = time.monotonic()
            # ⚠ **인터페이스가 사라진 것이 결정적 증거다.** USB-CAN 어댑터를 뽑으면
            # 커널이 `can0` 을 즉시 지운다 — 카메라의 `/dev/videoN` 과 같은 신호다.
            # 읽기 실패는 버스가 조용한 것일 수도 있어 그것만으로는 판정하지 않는다.
            if t0 >= next_err:
                next_err = t0 + self.ERR_SAMPLE_S
                self._sample_can_errors()
            if t0 >= next_presence:
                next_presence = t0 + self.PRESENCE_S
                if not iface_exists(self.iface):
                    self._declare_lost("CAN 인터페이스가 사라졌습니다")
                    return
            try:
                # 진단 필드는 핫패스가 아니다. 매 사이클 읽으면 100Hz 로 락을
                # 세 번씩 잡게 되고, 정작 급한 관절 읽기가 그만큼 늦어진다.
                if t0 >= next_diag:
                    err_code = int((self.arm.read_error() or {}).get("err_code") or 0)
                    # ⚠ `arm.ctrl_mode` 는 화면용 **문자열**("0x06")이다 — 정수는 이쪽이다
                    ctrl_mode = self.arm.refresh_ctrl_mode() or 0
                    next_diag = t0 + DIAG_PERIOD_S
                if t0 >= next_load:
                    next_load = t0 + LOAD_PERIOD_S
                    self._sample_load()

                values = self.arm.read_joints_normalized()
                if values is not None and self._state is not None:
                    self._state.publish(values, err_code=err_code, ctrl_mode=ctrl_mode)
                    self.published += 1
                    fails = 0
                else:
                    fails += 1
            except Exception as exc:
                fails += 1
                logger.warning("상태 발행 실패 (%s): %s", self.iface, exc)
                time.sleep(0.1)
            if fails >= self.MAX_READ_FAILS:
                self._declare_lost("연속으로 관절 상태를 읽지 못했습니다")
                return
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _sample_load(self) -> None:
        """관절 부하를 한 표본 먹이고 **새로 선** 경보만 로그로 낸다 (층 1).

        ⚠ **일정은 monotonic, 표시는 wall clock 이다.** 루프 스케줄링은 시계가
          뒤로 가면 안 되므로 monotonic 이지만, 그 값을 그대로 기록에 넣으면
          화면이 "언제 피크였나" 를 사람의 시각으로 못 옮긴다 (`bus_watch` 도
          `time.time()` 을 쓴다). NTP 점프는 `load.MAX_GAP_S` 가 흡수한다.

        ⚠ **읽기 실패는 조용히 넘긴다.** 부하 감시가 상태 발행을 막으면 안 된다 —
          관절을 못 읽는 것은 이미 `fails` 가 세고 있고 판정도 거기서 한다.
        """
        rows = self.arm.read_load()
        if not rows:
            return
        for ev in self.load.feed(
                time.time(),
                {j: r["effort_nm"] for j, r in rows.items()},
                {j: r["current_a"] for j, r in rows.items()}):
            logger.warning("%s", describe_load(self.iface, ev))

    def _declare_lost(self, why: str) -> None:
        """팔이 없어졌다고 판정하고 **발행을 끊는다.**

        세그먼트를 남기지 않는 것이 중요하다: 남겨두면 소비자(추론·녹화)가
        열어놓고 **멈춘 자세**를 관측으로 받는다 — 로봇이 실제로는 없는데
        정책은 마지막 자세를 계속 보는 상태가 된다.
        """
        logger.warning("%s: %s — 발행을 중단합니다", self.iface, why)
        self.lost_at = time.time()
        self._running = False
        if self._state is not None:
            try:
                self._state.close()
            except Exception:
                pass
            self._state = None
        A.unlink(A.segment_name(self.iface, A.KIND_ACTION))

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
                got = reader.read_new(timeout_s=DEADMAN_CHECK_S, poll_s=ACTION_POLL_S)
            except Exception as exc:
                logger.warning("명령 읽기 실패 (%s): %s", self.iface, exc)
                reader.close()
                reader = None
                continue
            if got is None:
                # 세그먼트가 사라졌으면 소비자가 **깨끗이** 떠난 것이다 (disconnect).
                if not A.segment_path(A.segment_name(self.iface, A.KIND_ACTION)).exists():
                    logger.info("소비자 종료 (%s)", self.iface)
                    reader.close()
                    reader = None
                    self._deadman_held = False
                    continue
                # ⚠ 세그먼트는 있는데 새 명령이 없다 = **소비자가 살아있지만 멈췄다.**
                # hang·GIL 잠김·OOM 직전이 여기로 온다. 데드맨이 잡아야 할 경우다.
                if reader.is_stale():
                    self._hold(first=not self._deadman_held, deadman_ms=reader.deadman_ms)
                continue
            if self._deadman_held:
                logger.info("데드맨 해제 (%s)", self.iface)
                self._deadman_held = False
            # 목표는 **절대 위치**라 중간 것을 건너뛰어도 무해하다 — 최신이 이긴다.
            # (누적 증분이었다면 건너뛴 만큼 팔이 덜 움직였을 것이다.)
            self._send(got["values"])
        if reader is not None:
            reader.close()

    def _hold(self, *, first: bool, deadman_ms: int) -> None:
        """데드맨 — 팔을 **지금 자리에 세운다.**

        ⚠ **명령을 멈추는 것과 팔을 세우는 것은 다르다.** 마지막 명령이 먼 목표였으면
        팔은 소비자가 죽은 뒤에도 계속 그리로 간다. 현재 자세를 실제로 명령해야 선다.

        토크는 끊지 않는다 — 끊으면 팔이 중력으로 떨어진다. 정지 = 그 자리에 서기다.
        멈춰 있는 동안 계속 보내는 이유는, 한 번만 보내면 그 사이 관성으로 밀린 만큼
        되돌아오지 않기 때문이다.
        """
        self._deadman_held = True
        if first:
            logger.warning("데드맨 (%s): %dms 동안 명령 없음 — 현재 자세로 정지",
                           self.iface, deadman_ms)
        # 목표는 안 넘긴다 — `_deadman_held` 라 필터가 현재 자세를 돌려준다
        self._send({})

    def _send(self, values: dict[str, float]) -> None:
        """정규화 목표 → **안전층** → CAN.

        `PiperMotorsBus.set_action` 과 같은 순서로 보내되, 그 앞에 필터가 있다.
        여기를 통과하지 않고 CAN 으로 나가는 경로가 있으면 그게 구멍이다.
        """
        now = self.arm.read_joints_normalized()
        values, reason = filter_goal(now or {}, values, self.safety,
                                     deadman_tripped=self._deadman_held)
        self.last_reason = reason
        if reason is not Reason.OK:
            self.filtered += 1
            # 매 프레임 로그를 뱉으면 30fps 로 로그가 묻힌다 — 바뀔 때만 남긴다
            if reason is not self._last_logged:
                if reason is Reason.STATE_OUT_OF_RANGE:
                    # 정책이 아니라 **캘리브레이션**을 의심해야 하는 경우다
                    logger.warning(
                        "현재 자세가 캘리브레이션 범위 밖입니다 (%s): %s. "
                        "필터가 아니라 캘리브레이션을 확인하세요.",
                        self.iface, _out_of_range(now or {}),
                    )
                else:
                    logger.warning("안전층이 명령을 바꿨다 (%s): %s",
                                   self.iface, reason.value)
        self._last_logged = reason

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
    """인터페이스별 브리지. robotd 가 팔에 연결되면 곧바로 켠다 —
    **소비자가 붙기를 기다리지 않는다.** 세그먼트가 있어야 소비자가 붙을 수 있다."""

    def __init__(self) -> None:
        self.bridges: dict[str, ArmBridge] = {}
        # 바닥 필터 설정은 **매니저가 들고 있다.** 브리지마다 따로 두면 팔을
        # 뽑았다 꽂을 때 기본값으로 돌아가고, 그러면 꺼둔 줄 알았던 필터가
        # 조용히 다시 켜진다.
        self._floor = safety_store.load()
        # ⚠ 부하 임계는 **팔마다** 다르다 (`load_store` 머리말). 바닥 필터처럼
        #   하나로 묶으면, 슬립이 잦은 한 관절을 잡으려고 조인 임계가 멀쩡한
        #   나머지 팔을 종일 울린다.
        self._load: dict[str, LoadLimits] = load_store.load()

    def start(self, arm) -> ArmBridge:
        b = self.bridges.get(arm.iface)
        if b is None:
            b = self.bridges[arm.iface] = ArmBridge(
                arm, SafetyConfig(floor=self._floor))
        # ⚠ **연결할 때마다 다시 걸어준다.** 브리지 객체는 재연결에 재사용되지만
        #   저장된 임계가 자기 발로 따라오지는 않는다 — 안 걸면 팔을 뽑았다 꽂는
        #   순간 잠정 기본값으로 조용히 돌아간다.
        b.load.retune(self._load.get(arm.iface, LoadLimits()))
        b.start()
        return b

    def floor_config(self) -> "FloorConfig":
        return self._floor

    def set_floor(self, patch: dict) -> "FloorConfig":
        """바닥 필터 설정을 바꾸고 **살아 있는 브리지에 곧바로 적용**한다.

        저장만 하고 적용을 안 하면 다음 연결까지 안 바뀌는데, 사용자는 화면에서
        바꿨으니 바뀐 줄 안다 — 안전 설정에서 그 어긋남은 위험하다.
        """
        self._floor = safety_store._apply(self._floor, patch)
        safety_store.save(self._floor)
        for b in self.bridges.values():
            b.safety = replace(b.safety, floor=self._floor)
        return self._floor

    def stop(self, iface: str) -> None:
        b = self.bridges.pop(iface, None)
        if b is not None:
            b.stop()

    def stop_all(self) -> None:
        for iface in list(self.bridges):
            self.stop(iface)

    def lost(self) -> list[dict]:
        """**데몬이 판정한** 사라진 팔들. 게이트웨이가 추론하지 않게 하려는 것이다."""
        return [{"id": iface, "at": b.lost_at}
                for iface, b in self.bridges.items() if b.lost_at]

    def load_limits(self, iface: str) -> LoadLimits:
        """저장된 임계. 없는 팔은 잠정 기본값."""
        return self._load.get(iface, LoadLimits())

    def set_load_limits(self, iface: str, patch: dict) -> LoadLimits:
        """임계를 바꾸고 **살아 있는 브리지에 곧바로 적용**한다.

        저장만 하고 적용을 안 하면 다음 연결까지 안 바뀌는데 사용자는 화면에서
        바꿨으니 바뀐 줄 안다 — 바닥 필터에서와 같은 이유로 안전 설정에서 그
        어긋남은 위험하다.
        """
        cfg = load_store._apply(self.load_limits(iface), patch)
        self._load[iface] = cfg
        load_store.save(self._load)
        b = self.bridges.get(iface)
        if b is not None:
            b.load.retune(cfg)
        return cfg

    def load_snapshot(self, iface: str) -> dict:
        """관절 부하 현황 (층 1). 브리지가 없으면 빈 dict — 연결 안 된 팔이다."""
        b = self.bridges.get(iface)
        return b.load.snapshot() if b else {}

    def load_snapshot_all(self) -> dict[str, dict]:
        """살아 있는 브리지 전부의 부하 현황.

        ⚠ **팔마다 부르면 안 되는 자리가 있다.** 게이트웨이의 경보 폴러는 2초
        루프 안에서 도는데, RPC 하나가 최대 20초(`RPC_TIMEOUT_S`)까지 기다린다 —
        팔 4대면 최악 80초 동안 장치 감시가 통째로 멎는다. 한 번에 받으면
        그 위험이 `lost()` 와 같은 크기로 준다.
        """
        return {iface: b.load.snapshot()
                for iface, b in self.bridges.items() if b.running}

    def load_history(self, iface: str, limit: int | None = None) -> dict:
        b = self.bridges.get(iface)
        return b.load.history(limit) if b else {"joints": [], "rows": []}

    def load_reset(self, iface: str) -> dict:
        """누적을 버리기 **전에** 그 창의 기록을 돌려준다.

        ⚠ 버리기만 하면 창이 닫힌 순간의 값이 사라진다 — 이걸 부르는 자리가
        0x150 리셋(층 2)이라, 실제 슬립각과 짝지을 부하 기록이 바로 그 값이다.
        """
        b = self.bridges.get(iface)
        if b is None:
            return {}
        snap = b.load.snapshot()
        b.load.reset()
        return snap

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


def _out_of_range(q: dict[str, float]) -> dict[str, float]:
    """범위를 벗어난 관절만. 어느 관절이 문제인지 로그에 남긴다."""
    from piper_robot.safety import clamp_range

    return {j: round(v, 2) for j, v in q.items() if clamp_range(j, v)[1]}


arm_bridge_manager = ArmBridgeManager()
