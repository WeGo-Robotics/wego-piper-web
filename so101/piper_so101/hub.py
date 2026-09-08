"""So101Hub — so101d 의 본체. 어댑터 스캔·팔 연결·shm 발행 (feature/so101d.md).

robotd 의 `ArmBridge` 와 같은 골격이다: 상태 스레드가 shm 으로 흘리고, 명령
스레드가 action 세그먼트를 소비한다. 다른 점은 바닥이 CAN 이 아니라 Feetech
시리얼이라는 것뿐 — 계약(레코드·deadman·lost 판정)은 동일하다.
"""

import logging
import threading
import time
from pathlib import Path

from piper_shm import ActionReader, ArmSegmentError, StateWriter
from piper_shm import arm as A

from piper_so101 import calibration as cal_mod
from piper_so101.calibration import CalibrationError, MotorCal
from piper_so101.feetech import BAUD, FeetechBus, FeetechError
from piper_so101.joints import MOTOR_IDS, SO101_JOINTS, from_record, to_record

logger = logging.getLogger(__name__)

#: 상태 발행 주기. sync read 실측 685Hz 라 60Hz 는 여유가 크다 (문서 §열린질문).
STATE_HZ = 60.0
#: 연속 읽기 실패 상한 (60Hz 기준 ~2초) — robotd 의 MAX_READ_FAILS 와 같은 발상.
MAX_READ_FAILS = 120
#: 어댑터가 아직 꽂혀 있는지 보는 주기. /dev stat 이라 공짜다.
PRESENCE_S = 1.0
#: 명령 폴링 (robotd 와 동일).
ACTION_POLL_S = 0.001
DEADMAN_CHECK_S = 0.05
#: 스텝당 최대 틱 이동 — Feetech 는 먼 목표를 즉시 추종하려다 과전류로 뻗는다.
#: 4096틱=360° 기준 약 8.8°. 소프트 클램프가 곧 퓨즈다 (문서 §6).
MAX_STEP_TICKS = 100

#: 아는 어댑터 (VID). CH34x 계열 — 실측: 1a86:55d3 (CH343).
_KNOWN_VID = ("usb-1a86_",)

_BY_ID = Path("/dev/serial/by-id")


class So101Error(RuntimeError):
    pass


class So101Bridge:
    """팔 하나: Feetech 버스 ↔ shm. 상태를 흘리고 명령을 받아 쓴다."""

    def __init__(self, arm_name: str, bus: FeetechBus,
                 cal: dict[str, MotorCal], calibrated: bool) -> None:
        self.arm_name = arm_name
        self.bus = bus
        self.cal = cal
        self.calibrated = calibrated
        self.lost_at = 0.0
        self.published = 0
        self.sent = 0
        self.torque_on = False
        self._estopped = False
        self._running = False
        self._threads: list[threading.Thread] = []
        self._state: StateWriter | None = None
        self._last_ticks: dict[str, int] = {}
        self._deadman_held = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self.lost_at = 0.0
        self.bus.setup_sync([MOTOR_IDS[n] for n in SO101_JOINTS])
        # 기동 순서: 상태 먼저 (robotd 와 동일 — 소비자는 상태가 있어야 붙는다)
        self._state = StateWriter(self.arm_name)
        self._running = True
        self._threads = [
            threading.Thread(target=self._publish_loop, daemon=True,
                             name=f"so101-state-{self.arm_name}"),
            threading.Thread(target=self._command_loop, daemon=True,
                             name=f"so101-cmd-{self.arm_name}"),
        ]
        for t in self._threads:
            t.start()
        logger.info("SO-101 브리지 시작: %s (calibrated=%s)",
                    self.arm_name, self.calibrated)

    def stop(self) -> None:
        # 스레드 먼저, 세그먼트 나중 (닫은 세그먼트를 루프가 되살린다 — rsd 전례)
        self.lost_at = 0.0
        self._running = False
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2)
        self._threads = []
        if self._state is not None:
            self._state.close()
            self._state = None
        A.unlink(A.segment_name(self.arm_name, A.KIND_ACTION))
        self.bus.close()
        logger.info("SO-101 브리지 정지: %s (발행 %d, 송신 %d)",
                    self.arm_name, self.published, self.sent)

    def estop(self) -> None:
        """토크를 끊고 명령 적용을 멈춘다. SO-101 은 가벼워 낙하 위험이 작다 —
        쥔 물건은 떨어진다는 것을 알고 내린 기본값이다 (문서 §열린질문)."""
        self._estopped = True
        for name in SO101_JOINTS:
            self.bus.set_torque(MOTOR_IDS[name], False)
        self.torque_on = False
        logger.warning("E-stop (%s): 토크 OFF", self.arm_name)

    def _declare_lost(self, why: str) -> None:
        """세그먼트를 남기지 않는다 — 남기면 소비자가 멈춘 자세를 관측으로 받는다."""
        logger.warning("%s: %s — 발행을 중단합니다", self.arm_name, why)
        self.lost_at = time.time()
        self._running = False
        if self._state is not None:
            try:
                self._state.close()
            except Exception:
                pass
            self._state = None
        A.unlink(A.segment_name(self.arm_name, A.KIND_ACTION))

    def _publish_loop(self) -> None:
        period = 1.0 / STATE_HZ
        fails = 0
        next_presence = 0.0
        while self._running:
            t0 = time.monotonic()
            if t0 >= next_presence:
                next_presence = t0 + PRESENCE_S
                # USB 시리얼을 뽑으면 커널이 노드를 즉시 지운다 — 결정적 신호
                if not Path(self.bus.port_name).exists():
                    self._declare_lost("USB 시리얼 어댑터가 사라졌습니다")
                    return
            ticks_by_id = self.bus.sync_read_positions()
            if ticks_by_id is None:
                fails += 1
                if fails >= MAX_READ_FAILS:
                    self._declare_lost("연속으로 모터 상태를 읽지 못했습니다 — "
                                       "전원·데이지체인 케이블을 보세요")
                    return
                time.sleep(period)
                continue
            fails = 0
            ticks = {name: ticks_by_id[MOTOR_IDS[name]] for name in SO101_JOINTS}
            norm = cal_mod.normalize(ticks, self.cal)
            if self._state is not None:
                self._state.publish(to_record(norm))
                self.published += 1
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _command_loop(self) -> None:
        """action 세그먼트가 생기면 소비한다. 리더 용법에서는 아무도 안 만들므로
        이 스레드는 조용히 기다린다 — 팔로워가 오면 이 경로가 그대로 선다."""
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
                reader.close()
                reader = None
                continue
            if got is None:
                if not A.segment_path(
                        A.segment_name(self.arm_name, A.KIND_ACTION)).exists():
                    logger.info("소비자 종료 (%s)", self.arm_name)
                    reader.close()
                    reader = None
                    self._deadman_held = False
                    continue
                if reader.is_stale() and not self._deadman_held:
                    # 위치 제어라 마지막 목표에 그대로 선다 — 보내기를 멈추는
                    # 것이 곧 정지다 (Piper 와 달리 관성 있는 원거리 목표를
                    # 스텝 클램프가 애초에 막는다).
                    self._deadman_held = True
                    logger.warning("데드맨 (%s): 명령 중단 — 현 위치 유지",
                                   self.arm_name)
                continue
            if self._estopped:
                continue                    # E-stop 후에는 새 정합까지 무시
            self._deadman_held = False
            self._apply(from_record(got["values"]))

    def _apply(self, norm: dict[str, float]) -> None:
        goal = cal_mod.denormalize(norm, self.cal)
        if not self.torque_on:
            for name in SO101_JOINTS:
                self.bus.set_torque(MOTOR_IDS[name], True)
            self.torque_on = True
            cur = self.bus.sync_read_positions() or {}
            self._last_ticks = {n: cur.get(MOTOR_IDS[n], goal[n])
                                for n in SO101_JOINTS}
            logger.info("첫 명령 (%s): 토크 ON", self.arm_name)
        for name in SO101_JOINTS:
            want = goal[name]
            last = self._last_ticks.get(name, want)
            step = max(-MAX_STEP_TICKS, min(MAX_STEP_TICKS, want - last))
            target = last + step
            if self.bus.write_goal(MOTOR_IDS[name], target):
                self._last_ticks[name] = target
        self.sent += 1


class So101Hub:
    """so101d 의 RPC 표면. 데몬 계약 동사 + 진단."""

    def __init__(self) -> None:
        self.bridges: dict[str, So101Bridge] = {}     # arm_name → bridge
        self._ports: dict[str, str] = {}              # arm_name → by-id

    # ── 스캔 ──

    def scan(self) -> list[dict]:
        """`/dev/serial/by-id` 의 아는 어댑터들. **다시 보이면 lost 를 지운다**
        — rsd 카메라에서 안 지워서 스캔이 살린 장치를 감시가 도로 없음
        처리했다 (커밋 1b036f0). 같은 병을 여기서는 처음부터 막는다."""
        entries = []
        seen_ids = set()
        if _BY_ID.exists():
            for p in sorted(_BY_ID.iterdir()):
                if not p.name.startswith(_KNOWN_VID):
                    continue
                seen_ids.add(p.name)
                attached = next((a for a, bid in self._ports.items()
                                 if bid == p.name), None)
                entries.append({
                    "id": p.name, "port": str(p.resolve()), "baud": BAUD,
                    "model": "so101", "transport": "serial",
                    "arm": attached, "present": True,
                })
        for arm, by_id in self._ports.items():
            b = self.bridges.get(arm)
            if b and b.lost_at and by_id in seen_ids:
                logger.info("SO-101 어댑터 %s 가 다시 보입니다 — 잃어버림 해제", by_id)
                b.lost_at = 0.0
        return entries

    # ── 연결 수명주기 ──

    def attach(self, by_id: str, arm_name: str, calib: str | None = None) -> dict:
        import re
        if not re.fullmatch(r"so101_[a-z0-9_]+", arm_name):
            # 접두사가 계약이다 — sweep_stale 이 자기 세그먼트를 이걸로 가른다
            raise So101Error(f"팔 이름은 so101_ 로 시작해야 합니다: {arm_name}")
        if arm_name in self.bridges and self.bridges[arm_name].running:
            raise So101Error(f"{arm_name} 은 이미 연결돼 있습니다")
        port = _BY_ID / by_id
        if not port.exists():
            raise So101Error(f"어댑터가 없습니다: {by_id}")
        try:
            bus = FeetechBus(str(port.resolve()))
        except FeetechError as exc:
            raise So101Error(str(exc))

        # **6모터 전수 핑** — 조립 팔의 데이지체인은 케이블 하나로 끊긴다
        # (실측 2026-09-08: 팔꿈치→손목 접촉 불량으로 4·5·6 무응답).
        answered = bus.ping_all([MOTOR_IDS[n] for n in SO101_JOINTS])
        missing = [mid for mid, model in answered.items() if model is None]
        if missing:
            bus.close()
            names = [n for n in SO101_JOINTS if MOTOR_IDS[n] in missing]
            hint = ("팔꿈치→손목 3핀 케이블을 보세요"
                    if missing == [4, 5, 6] else "전원과 데이지체인 케이블을 보세요")
            raise So101Error(
                f"모터 무응답: {', '.join(f'{n}(ID{MOTOR_IDS[n]})' for n in names)}"
                f" — {hint}")

        calibrated = True
        path = cal_mod.find_calibration(calib or arm_name)
        if path is None:
            cal = cal_mod.default_calibration()
            calibrated = False
            logger.warning("%s: 캘리브레이션 파일이 없습니다 — 전범위 폴백. "
                           "이 상태로 등록·수집하면 안 됩니다", arm_name)
        else:
            try:
                cal = cal_mod.load_calibration(path)
            except CalibrationError:
                bus.close()
                raise
            logger.info("%s: 캘리브레이션 %s", arm_name, path)

        # 연결 직후는 사람이 팔을 만지는 시간 — 토크를 끈다 (Piper attach 와 동일)
        for name in SO101_JOINTS:
            bus.set_torque(MOTOR_IDS[name], False)

        bridge = So101Bridge(arm_name, bus, cal, calibrated)
        bridge.start()
        self.bridges[arm_name] = bridge
        self._ports[arm_name] = by_id
        return self.info(arm_name)

    def release(self, arm_name: str) -> bool:
        b = self.bridges.pop(arm_name, None)
        self._ports.pop(arm_name, None)
        if b is not None:
            b.stop()
        return b is not None

    def release_all(self) -> bool:
        for arm in list(self.bridges):
            self.release(arm)
        return True

    def estop(self, arm_name: str = "") -> list[str]:
        hit = [a for a in self.bridges if not arm_name or a == arm_name]
        for a in hit:
            self.bridges[a].estop()
        return hit

    # ── 조회 ──

    def info(self, arm_name: str = "") -> dict:
        def one(a: str, b: So101Bridge) -> dict:
            return {
                "arm": a, "by_id": self._ports.get(a, ""),
                "running": b.running, "calibrated": b.calibrated,
                "published": b.published, "sent": b.sent,
                "torque_on": b.torque_on,
                # 데몬 계약 §4.2 — UI 는 이걸 보고 버튼을 그린다
                "capabilities": {
                    "model": "so101", "transport": "serial", "dof": 5,
                    "gripper": True, "kinematics": "so101",
                    "joint_names": list(SO101_JOINTS),
                    "features": {"master_slave": False, "hw_zero": False,
                                 "slip_reset": False, "parking": False,
                                 "rate_limit": True},
                },
            }
        if arm_name:
            b = self.bridges.get(arm_name)
            if b is None:
                raise So101Error(f"모르는 팔: {arm_name}")
            return one(arm_name, b)
        return {"arms": [one(a, b) for a, b in self.bridges.items()]}

    def lost(self) -> list[dict]:
        """**데몬이 판정한** 사라진 팔들 (robotd·rsd 와 같은 계약)."""
        return [{"id": a, "at": b.lost_at}
                for a, b in self.bridges.items() if b.lost_at]

    def sweep_stale(self) -> list[str]:
        """지난 프로세스가 남긴 **so101 세그먼트만** 정리. robotd 것을 지우면
        그쪽 소비자가 깨진다 — 자기 접두사만 만진다 (camerad 전례)."""
        live = {A.segment_name(a, k) for a, b in self.bridges.items()
                if b.running for k in (A.KIND_STATE, A.KIND_ACTION)}
        stale = [n for n in A.list_segments()
                 if ".so101_" in n and n not in live]
        for name in stale:
            A.unlink(name)
        if stale:
            logger.info("남은 so101 세그먼트 %d개 정리: %s", len(stale), stale)
        return stale
