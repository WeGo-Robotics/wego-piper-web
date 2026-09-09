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


def _unwrap(raw: int, last_raw: int | None, last_uw: int | None) -> int:
    """롤오버 이어붙이기. 첫 견본은 raw 그대로."""
    if last_raw is None or last_uw is None:
        return int(raw)
    d = int(raw) - int(last_raw)
    if d > 2048:
        d -= 4096
    elif d < -2048:
        d += 4096
    return int(last_uw) + d


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
        # 캘리브레이션 위저드 상태 (feature/so101d.md §3 — UI 단계 진행).
        # 발행 루프가 60Hz 로 이미 raw 를 읽으므로 범위 스윕은 여기서 공짜다.
        # 추적은 **언랩 좌표**로 한다: 60Hz 면 사람 손 이동이 스텝당 수십 틱이라
        # 2048 틱 넘는 점프 = 0/4095 롤오버로 판정해 이어붙인다 — 가동범위가
        # 롤오버를 걸치는 조립도 min/max 가 안 찢어진다.
        self.calib_stage: str | None = None      # None | "range"
        self.last_raw: dict[str, int] = {}
        self._calib_min: dict[str, int] = {}
        self._calib_max: dict[str, int] = {}
        self._uw: dict[str, int] = {}            # 언랩 누적 위치
        self._uw_last_raw: dict[str, int] = {}
        self._cal_backup: dict | None = None
        # EEPROM 쓰기 동안 읽기 루프를 쉰다 — 60Hz sync read 와 겹치면 응답이
        # 엉켜 쓰기가 간헐적으로 실패했다 (실기: 저장을 여러 번 눌러야 했다)
        self.io_pause = False
        # 좌/우 지정 — 텔레옵 짝짓기용. 데몬 세션에 by_id 별로 남는다
        self.side: str = ""

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
        # 포트도 놓는다 — 물고 있으면 재연결 attach 가 죽은 fd 와 같은 포트를
        # 두고 싸운다 (실측: 브리지 11개가 한 포트에서 multiple access 예외)
        self.bus.close()

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
                # ⚠ 누가 내 세그먼트를 unlink 하면(robotd 의 기동 정리가 그랬다 —
                #   2026-09-09) 열린 fd 로는 계속 써져 **조용히** 깨진다: published 는
                #   오르는데 경로로는 아무도 못 연다. 다시 만든다.
                if self._state is not None and self._state.orphaned:
                    logger.warning("%s: 상태 세그먼트가 사라졌습니다(누가 unlink) — 다시 만듭니다",
                                   self.arm_name)
                    self._state.close(unlink_segment=False)
                    self._state = StateWriter(self.arm_name)
            if self.io_pause:
                time.sleep(period)
                continue
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
            self.last_raw = ticks
            if self.calib_stage == "range":
                for name, v in ticks.items():
                    u = _unwrap(v, self._uw_last_raw.get(name),
                                self._uw.get(name))
                    self._uw_last_raw[name] = v
                    self._uw[name] = u
                    if name not in self._calib_min or u < self._calib_min[name]:
                        self._calib_min[name] = u
                    if name not in self._calib_max or u > self._calib_max[name]:
                        self._calib_max[name] = u
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
            if self._estopped or self.calib_stage is not None:
                continue    # E-stop 후·캘리브레이션 중에는 명령을 안 받는다
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


#: 좌/우 지정이 남는 곳 — 팔의 정체는 어댑터(by_id)라 그 열쇠로 저장한다.
_SESSION_PATH = Path.home() / ".config" / "piper-web" / "so101_session.json"


class So101Hub:
    """so101d 의 RPC 표면. 데몬 계약 동사 + 진단."""

    def __init__(self) -> None:
        self.bridges: dict[str, So101Bridge] = {}     # arm_name → bridge
        self._ports: dict[str, str] = {}              # arm_name → by-id
        self._session: dict[str, dict] = self._load_session()

    @staticmethod
    def _load_session() -> dict[str, dict]:
        import json
        try:
            return json.loads(_SESSION_PATH.read_text())
        except Exception:
            return {}

    def _save_session(self) -> None:
        import json
        try:
            _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SESSION_PATH.write_text(json.dumps(self._session, indent=2))
        except Exception as exc:
            logger.warning("so101 세션 저장 실패: %s", exc)

    def set_side(self, arm_name: str, side: str) -> dict:
        """좌/우 지정 — 텔레옵 짝짓기(어느 팔로워를 몰 것인가)의 재료.
        어댑터(by_id) 열쇠로 세션에 남아 재연결·재기동에도 유지된다."""
        if side not in ("left", "right", ""):
            raise So101Error(f"side 는 left/right/빈값이어야 합니다: {side}")
        b = self.bridges.get(arm_name)
        if b is None:
            raise So101Error(f"모르는 팔: {arm_name}")
        b.side = side
        by_id = self._ports.get(arm_name)
        if by_id:
            self._session.setdefault(by_id, {})["side"] = side
            self._save_session()
        return self.info(arm_name)

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
                attached = next(
                    (a for a, bid in self._ports.items() if bid == p.name
                     and self.bridges.get(a) and self.bridges[a].running),
                    None) or next((a for a, bid in self._ports.items()
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

    def attach(self, by_id: str, arm_name: str = "", calib: str | None = None) -> dict:
        import re

        # ⚠ **어댑터당 브리지는 하나다.** 케이블이 빠져 브리지가 죽은 채 남으면
        # 이름을 점유한 채 스캔 매핑까지 물고 있어, 재연결 클릭이 leader2, 3…을
        # 계속 만들며 한 포트를 두고 싸웠다 (실측: 11개, multiple access 예외).
        # 죽은 것은 여기서 치우고 **같은 이름으로 부활**시킨다 — 재연결은 새
        # 팔이 아니다.
        old = next((a for a, bid in self._ports.items() if bid == by_id), None)
        if old is not None:
            if self.bridges.get(old) and self.bridges[old].running:
                raise So101Error(f"{old} 이 이미 이 어댑터에 연결돼 있습니다")
            self.release(old)
            if not arm_name:
                arm_name = old
        if not arm_name:
            n = 1
            while f"so101_leader{n}" in self.bridges:
                n += 1
            arm_name = f"so101_leader{n}"
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
        bridge.side = str(self._session.get(by_id, {}).get("side", ""))
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
                "side": b.side,
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

    # ── 캘리브레이션 위저드 (feature/so101d.md §3 — lerobot-calibrate 의 UI 판) ──
    #
    # 단계: begin(토크 해제·공장 초기화) → center(중앙 자세 호밍 굽기) →
    # range(발행 루프가 min/max 추적) → save(리밋 굽기 + JSON — LeRobot 과 같은
    # 포맷·자리라 어느 쪽으로 만들었든 호환).

    #: 관절이 "움직였다"고 인정할 최소 스윕 폭 (틱). 4096 = 360° 기준 약 26°.
    #: 안 움직인 관절로 저장하면 범위가 퇴화해 그 팔의 정규화가 통째로 깨진다.
    CALIB_MIN_SPAN = 300

    def _calib_bridge(self, arm_name: str) -> So101Bridge:
        b = self.bridges.get(arm_name)
        if b is None or not b.running:
            raise So101Error(f"{arm_name} 이 연결돼 있지 않습니다")
        return b

    def calib_begin(self, arm_name: str) -> dict:
        """토크 해제 + EEPROM 잠금 해제 + 공장 초기화(호밍 0·리밋 전범위) →
        바로 범위 스윕. **중앙 자세를 사람이 잡지 않는다** — 양 끝까지 훑으면
        중앙은 산수(min+max)/2 다. 사람이 "중앙"을 눈대중으로 잡는 것보다
        정확하고, 단계도 하나 준다. 초기화하는 이유: 이전 호밍 위에 겹으로
        구우면 기준이 틀어진다 (lerobot reset_calibration 과 같은 순서)."""
        b = self._calib_bridge(arm_name)
        b._cal_backup = dict(b.cal)
        b.io_pause = True        # 읽기와 겹치면 EEPROM 쓰기가 응답을 놓친다
        try:
            for name in SO101_JOINTS:
                mid = MOTOR_IDS[name]
                b.bus.set_torque(mid, False)
                b.bus.unlock_eeprom(mid)
                if not (b.bus.write_homing(mid, 0)
                        and b.bus.write_limits(mid, 0, 4095)):
                    raise So101Error(f"{name} 초기화 쓰기 실패 — 전원·케이블을 보세요")
        finally:
            b.io_pause = False
        b.torque_on = False
        # 위저드 동안 화면·발행은 전범위 폴백으로 — 낡은 캘리브레이션으로
        # 정규화한 값이 "실시간 위치"로 보이면 사람이 헷갈린다
        b.cal = cal_mod.default_calibration()
        b.calibrated = False
        b._calib_min, b._calib_max = {}, {}
        b._uw, b._uw_last_raw = {}, {}
        b.calib_stage = "range"
        return self.calib_status(arm_name)

    def calib_status(self, arm_name: str) -> dict:
        b = self._calib_bridge(arm_name)
        spans = {n: (b._calib_max.get(n, 0) - b._calib_min.get(n, 0))
                 for n in SO101_JOINTS}
        return {
            "arm": arm_name, "stage": b.calib_stage,
            "raw": {n: b.last_raw.get(n) for n in SO101_JOINTS},
            "min": b._calib_min, "max": b._calib_max, "spans": spans,
            "min_span": self.CALIB_MIN_SPAN,
            "ok": {n: spans[n] >= self.CALIB_MIN_SPAN for n in SO101_JOINTS},
            "calibrated": b.calibrated,
        }

    def calib_save(self, arm_name: str) -> dict:
        b = self._calib_bridge(arm_name)
        if b.calib_stage != "range":
            raise So101Error("범위 기록 단계가 아닙니다")
        lacking = [n for n in SO101_JOINTS
                   if (b._calib_max.get(n, 0) - b._calib_min.get(n, 0))
                   < self.CALIB_MIN_SPAN]
        if lacking:
            raise So101Error("아직 안 움직인 관절이 있습니다: " + ", ".join(lacking)
                             + " — 양 끝까지 움직인 뒤 다시 저장하세요")
        # 중앙 = 스윕의 중점 (언랩 좌표). 호밍 = 중앙 − 2047 → 그 중앙이 2047
        # 로 읽히고, 범위는 2047±(폭/2) 로 옮겨 앉는다. 부호-크기 한도(±2047)
        # 는 중앙을 0..4095 로 접은 뒤라 넘을 수 없다 (2048 경계만 클램프).
        homing: dict[str, int] = {}
        range_min: dict[str, int] = {}
        range_max: dict[str, int] = {}
        b.io_pause = True        # 읽기와 겹치면 EEPROM 쓰기가 응답을 놓친다
        try:
            for name in SO101_JOINTS:
                mn, mx = b._calib_min[name], b._calib_max[name]
                span = mx - mn
                center = ((mn + mx) // 2) % 4096
                homing[name] = min(2047, max(-2047, center - 2047))
                range_min[name] = max(0, 2047 - span // 2)
                range_max[name] = min(4095, 2047 + (span - span // 2))
                if not b.bus.write_homing(MOTOR_IDS[name], homing[name]):
                    raise So101Error(f"{name} 호밍 쓰기 실패")
                if not b.bus.write_limits(MOTOR_IDS[name],
                                          range_min[name], range_max[name]):
                    raise So101Error(f"{name} 리밋 쓰기 실패")
        finally:
            b.io_pause = False
        path = cal_mod.save_calibration(arm_name, homing, range_min, range_max)
        b.cal = cal_mod.load_calibration(path)
        b.calibrated = True
        b.calib_stage = None
        b._cal_backup = None
        logger.info("%s: 캘리브레이션 저장 — %s", arm_name, path)
        return self.calib_status(arm_name)

    def calib_cancel(self, arm_name: str) -> dict:
        """중단 — 이전 캘리브레이션으로 되돌린다. ⚠ 호밍을 이미 구웠으면
        (center 이후) 서보 쪽은 새 호밍이고 파일은 옛 것이라 어긋난다 —
        그래서 취소해도 '처음부터 다시'가 안내다 (status 가 stage 로 말한다)."""
        b = self._calib_bridge(arm_name)
        if b._cal_backup is not None:
            b.cal = b._cal_backup
            b._cal_backup = None
            b.calibrated = cal_mod.find_calibration(arm_name) is not None
        b.calib_stage = None
        return self.calib_status(arm_name)

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
