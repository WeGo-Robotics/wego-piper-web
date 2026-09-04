"""팔 소유 — robotd 가 돌리는 허브.

`RealSenseHub`/`V4l2Hub` 와 **같은 자리**다: 장치를 쥐고, 게이트웨이는 버스 RPC 로만
말한다. 이름도 그쪽과 맞춰 `scan`/`connect`/`disconnect` 를 쓴다.

## 게이트웨이에 남기는 것

역할(leader/follower)·슬롯·등록 여부·카메라 매핑·프리셋·세션. **사람이 정하는 것**이고
CAN 과 무관하다. 그래서 여기 메서드는 그 값들을 인자로 받지, 자기가 들고 있지 않다:

    clear_errors(ifaces)          ← "연결된 follower 전부"는 게이트웨이가 풀어서 준다
    detect_motion() -> iface      ← 슬롯 배정은 게이트웨이가 한다

이 경계를 흐리면 같은 사실이 두 프로세스에 생기고, 재시작할 때마다 어긋난다.
"""

import logging
import threading
import time

from piper_robot.joints import denormalize_joint
from piper_robot.arm import Arm
from piper_robot.can import scan_can_interfaces

logger = logging.getLogger(__name__)

# 움직임 감지: 이만큼 raw 가 움직이면 "이 팔이다"로 본다.
FIND_THRESHOLD_RAW = 45_000
FIND_TIMEOUT_SEC = 30


class RobotHub:
    def __init__(self) -> None:
        self.arms: dict[str, Arm] = {}
        # 말단 조그의 직전 명령. 도달 확인을 **다음 명령 때** 하려고 들고 있다.
        self._pending: dict[str, dict] = {}
        self._motion: dict = {}

    # ── 스캔/연결 ──

    def scan(self) -> list[dict]:
        for p in scan_can_interfaces():
            iface = p["iface"]
            arm = self.arms.get(iface)
            if arm is None:
                self.arms[iface] = Arm(iface=iface, bus_info=p["bus_info"], state=p["state"])
            else:
                arm.bus_info, arm.state = p["bus_info"], p["state"]
        return [a.to_dict() for a in self.arms.values()]

    def connect(self, iface: str) -> tuple[bool, str]:
        arm = self.arms.get(iface)
        return arm.connect() if arm else (False, f"Unknown interface: {iface}")

    def disconnect(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        if arm is None:
            return False
        arm.disconnect()
        return True

    def release_all(self) -> bool:
        released = False
        for arm in self.arms.values():
            if arm.connected:
                arm.disconnect()
                released = True
        return released

    def info(self, iface: str) -> dict:
        arm = self.arms.get(iface)
        return arm.to_dict() if arm else {}

    # ── 상태 ──

    def refresh_mode(self, iface: str, classify: bool = False) -> dict:
        """`classify=True` 는 **버스를 0.35초 듣는다** — 폴링에서 부르지 않는다."""
        arm = self.arms.get(iface)
        if arm is None:
            return {}
        arm.refresh_mode(classify=classify)
        return arm.to_dict()

    def set_master_slave(self, iface: str, master: bool) -> tuple[bool, str]:
        arm = self.arms.get(iface)
        return arm.set_master_slave(master) if arm else (False, f"Unknown interface: {iface}")

    def read_joints(self, iface: str) -> dict | None:
        arm = self.arms.get(iface)
        return arm.read_joints_normalized() if arm else None

    def bus_reset(self, iface: str) -> dict:
        """버스를 내렸다 올린다. **팔 연결이 끊기므로** 그 팔도 정리한다."""
        from piper_robot.bus_watch import bus_watch
        from piper_robot.can import bus_stats, reset_bus

        arm = self.arms.get(iface)
        if arm is not None and arm.connected:
            arm.disconnect()
        out = reset_bus(iface)
        if out.get("ok"):
            # 기준선과 이력을 **한 번에** 새로 잡는다 — 숫자만 초기화하고 선은
            # 옛 구간을 그대로 두면 둘이 다른 말을 한다
            bus_watch.rebase(iface)
        return out

    def bus_status(self, limit: int | None = None) -> list[dict]:
        from piper_robot.bus_watch import bus_watch

        """모든 CAN 버스의 상태. **호스트에서만 읽을 수 있다** — 게이트웨이는
        컨테이너라 `/sys/class/net` 자체가 안 보인다."""
        from piper_robot.can import bus_stats, scan_can_interfaces

        rows = []
        for c in scan_can_interfaces():
            row = bus_stats(c["iface"])
            base = bus_watch.baseline(c["iface"])
            if base and row.get("counters"):
                # ⚠ **항목별로도, 트래픽도** 낸다. 합계만 주면 화면은 항목 칸에
                #   누적값을 쓸 수밖에 없고, 트래픽을 빼먹으면 파생값이 섞인 기준을
                #   쓴다 — 둘 다 실제로 겪었다.
                cb = base.get("counters", {})
                row["counters_since_reset"] = {
                    k: max(0, v - cb.get(k, 0)) for k, v in row["counters"].items()}
                row["errors_since_reset"] = sum(row["counters_since_reset"].values())
                row["rx_since_reset"] = max(0, (row.get("rx_packets") or 0)
                                            - base.get("rx_packets", 0))
                row["tx_since_reset"] = max(0, (row.get("tx_packets") or 0)
                                            - base.get("tx_packets", 0))
            row["history"] = bus_watch.history(c["iface"], limit)
            rows.append(row)
        return rows

    # ── 관절 검사 ──
    #
    # ⚠ 한 번에 하나만 돈다. 두 팔을 동시에 흔들면 사람이 둘 다 못 지켜본다.
    _diag = None

    def diag_start(self, iface: str, joints: list[str],
                   intensity: str = "normal") -> dict:
        from piper_robot import diagnostics as D
        from piper_robot.diag_runner import DiagRun
        from piper_robot.publish import arm_bridge_manager

        if self._diag is not None and not self._diag.done:
            return {"ok": False, "error": "이미 검사가 돌고 있습니다"}
        arm = self.arms.get(iface)
        if arm is None or not arm.connected:
            return {"ok": False, "error": f"{iface} 가 연결되어 있지 않습니다"}
        if arm.is_master:
            return {"ok": False, "error": arm.MASTER_IGNORES}
        bridge = arm_bridge_manager.bridges.get(iface)
        if bridge is None:
            return {"ok": False, "error": f"{iface} 의 발행 브리지가 없습니다"}

        now = arm.read_joints_normalized() or {}
        if not now:
            return {"ok": False, "error": f"{iface} 의 관절값을 읽지 못했습니다"}
        centers = {j: denormalize_joint(j, now[j]) / 1000.0 for j in joints if j in now}
        limits, accels = {}, {}
        for row in (arm.versions().get("joints") or []):
            if row.get("angle_min_deg") is not None:
                limits[row["joint"]] = (row["angle_min_deg"], row["angle_max_deg"])
            # ⚠ 속도(`max_spd_rad_s`)는 **안 쓴다.** 그 값은 MOVE J 플래너의 설정이지
            #   위치 추종의 한계가 아니다 — 실제 수집은 그 네 배로 움직인다
            #   (`diagnostics.SPEED_DEG_S`). 계획을 무는 것은 가속도 쪽이다.
            if row.get("max_acc_rad_s2"):
                accels[row["joint"]] = row["max_acc_rad_s2"]
        plan = D.build_plan(centers, limits, joints, intensity, accels,
                            _up_directions(arm, now, joints))
        if not any(p.amplitude_deg > 0 for p in plan.joints):
            return {"ok": False, "plan": plan.to_dict(),
                    "error": "흔들 여유가 없습니다 — 관절이 한계 근처입니다. "
                             "팔을 가운데 자세로 옮기고 다시 하세요."}
        self._diag = DiagRun(arm, bridge, plan)
        self._diag.start()
        return {"ok": True, "plan": plan.to_dict()}

    def diag_status(self) -> dict:
        return self._diag.status() if self._diag else {"running": False, "samples": 0}

    def diag_stop(self) -> dict:
        if self._diag:
            self._diag.stop()
        return {"ok": True}

    def diag_result(self) -> dict:
        """행 전체와 요약. **행도 준다** — 요약만으로는 파형을 못 본다."""
        from piper_robot import diagnostics as D

        if self._diag is None:
            return {"rows": [], "summary": {}, "plan": {}}
        joints = [p.joint for p in self._diag.plan.joints]
        from piper_robot.can import adapter_serial

        arm = self._diag.arm
        return {"rows": self._diag.rows,
                "summary": D.summarize(self._diag.rows, joints),
                "plan": self._diag.plan.to_dict(),
                "iface": arm.iface, "error": self._diag.error,
                # ⚠ **팔의 시리얼이 아니다** — Piper 는 CAN 으로 시리얼을 안 준다.
                #   이건 "어느 케이블에 물려 있었나" 다. 팔 자체는 사람이 적는다.
                "adapter_serial": adapter_serial(arm.iface),
                "firmware": arm.firmware,
                "at": self._diag.started_at}

    def versions(self) -> list[dict]:
        """연결된 팔 전부의 버전·관절 정보."""
        return [a.versions() for a in self.arms.values() if a.connected]

    def motor_enabled(self, iface: str) -> dict:
        arm = self.arms.get(iface)
        return arm.motor_enabled() if arm else {}

    def read_joints_raw(self, iface: str) -> list[int] | None:
        arm = self.arms.get(iface)
        return arm.read_joints_raw() if arm else None

    # ── 안전·에러 ──

    def clear_errors(self, ifaces: list[str]) -> list[dict]:
        """지정한 팔의 에러를 조회 후 **무조건** 클리어. 조회 전 상태를 함께 돌려준다.

        ⚠ "연결된 follower 전부"는 여기서 풀지 않는다 — 역할은 게이트웨이가 안다.
        """
        report = []
        for iface in ifaces:
            arm = self.arms.get(iface)
            if arm is None or not arm.connected:
                continue
            before = arm.read_error()
            out = arm.clear_error()
            if before and before.get("err_code"):
                logger.warning("Arm %s had error 0x%04X before clear: %s",
                               iface, before["err_code"], before["flags"])
            # slip_raw: 리셋이 재동기화한 보고값 간극 = 쌓여 있던 슬립 (arm.clear_error)
            report.append({"iface": iface, "error": before,
                           "cleared": out.get("ok", False),
                           "slip_raw": out.get("slip_raw")})
        return report

    def read_error(self, iface: str) -> dict | None:
        arm = self.arms.get(iface)
        return arm.read_error() if arm else None

    def enable_torque(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        return bool(arm and arm.enable_torque())

    def disable_torque(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        return bool(arm and arm.disable_torque())

    # 직전 명령이 도달했는지 **다음 명령 때** 본다. 그만큼 지났어야 판정이 뜻이 있다.
    END_POSE_SETTLE_S = 2.0

    def jog_end_pose(self, iface: str, axis: str, delta: float,
                     box: dict | None = None) -> dict:
        """말단을 한 걸음 움직인다 (feature/teleoperation.md §3-C).

        ⚠ **관절 안전 필터가 안 걸리는 유일한 경로다.** 막는 것은 전부
        `endpose` 의 상자와 걸음 상한이고, 그게 통과한 뒤에야 명령이 나간다.
        """
        from piper_robot.endpose import WorkspaceBox, reached, step_target

        arm = self.arms.get(iface)
        if arm is None:
            return {"ok": False, "error": f"{iface} 를 모릅니다"}
        current = arm.read_end_pose()
        if current is None:
            return {"ok": False, "error": "말단 자세를 읽지 못했습니다"}

        wb = WorkspaceBox(**{k: tuple(v) for k, v in box.items()}) if box else WorkspaceBox()
        target, why = step_target(current, axis, delta, wb)
        if target is None:
            return {"ok": False, "error": why, "pose": current}

        # ⚠ **직전 명령의 도달을 여기서 본다 — 보내기 전에.**
        #
        #   원래는 보낸 뒤 2초를 기다려 확인했는데, 그러면 버튼 한 번에 UI 가
        #   2초 잠긴다. 조그는 연타하는 물건이라 그게 못 쓸 정도로 느렸다.
        #
        #   확인이 필요한 순간은 "못 가는 방향으로 **또** 미는" 때다. 그 순간이
        #   바로 다음 명령이므로, 여기서 보면 기다릴 필요가 없다.
        stuck = self._check_previous(iface, current, axis, delta)
        if stuck:
            return {"ok": False, "error": stuck, "pose": current}

        # ⚠ **SDK 는 전송 실패를 예외로 안 준다.** 팔이 꺼져 있어도 명령은 조용히
        #   돌아오고 로그에만 남는다 — 실기에서 5번을 "성공"으로 보고했다.
        #   버스 상태가 그걸 바로 말해 준다.
        from piper_robot.can import can_unhealthy_reason

        bad = can_unhealthy_reason(iface)
        if bad:
            return {"ok": False, "error": bad, "pose": current}

        ok, msg = arm.move_end_pose(target)
        if not ok:
            self._pending.pop(iface, None)
            return {"ok": False, "error": msg, "pose": current}

        self._pending[iface] = {"before": current, "target": target,
                                "axis": axis, "delta": delta, "at": time.time()}
        return {"ok": True, "pose": current, "target": target, "sent": True}

    def _check_previous(self, iface: str, now: dict, axis: str, delta: float) -> str | None:
        """직전 명령이 못 갔는데 **같은 방향으로 또** 미는가. 그러면 사유를 돌려준다.

        같은 방향만 막는다 — 못 가는 쪽으로 계속 밀면 팔이 떨거나 특이점에서
        튀지만, 빠져나오는 방향까지 막으면 갇힌다(작업 공간 상자와 같은 규율).
        """
        from piper_robot.endpose import reached

        prev = self._pending.get(iface)
        if not prev:
            return None
        if time.time() - prev["at"] < self.END_POSE_SETTLE_S:
            return None          # 아직 가는 중일 수 있다
        self._pending.pop(iface, None)
        if reached(prev["before"], prev["target"], now):
            return None
        same_way = prev["axis"] == axis and (prev["delta"] > 0) == (delta > 0)
        if not same_way:
            return None
        return "그 방향으로는 못 갑니다 (직전 명령이 도달하지 못했습니다)"

    # ── 하드웨어 영점 ──

    def read_motion_status(self, iface: str) -> dict | None:
        """팔이 보고하는 운동 상태 (`0x02 无解`, `0x03 奇异点` …).

        말단 명령이 안 먹을 때 **이유를 추측하지 않기 위한** 것이다.
        """
        arm = self.arms.get(iface)
        return arm.read_motion_status() if arm else None

    def stream_end_pose(self, iface: str, target: dict) -> dict:
        """POSE 모드 텔레오퍼레이션이 초당 수십 번 부른다.

        ⚠ 범위 판단은 **부르는 쪽**이 끝내고 온다 — 이 경로는 관절 안전 필터를
          안 탄다. 여기서 또 검사하면 두 곳이 되고, 둘이 어긋난다.
        """
        arm = self.arms.get(iface)
        if arm is None:
            return {"ok": False, "error": f"{iface} 를 모릅니다"}
        ok, why = arm.stream_end_pose(target)
        return {"ok": ok, "error": None if ok else why}

    def read_raw_all(self, iface: str) -> dict:
        """관절+그리퍼 raw. 영점 창이 폴링한다."""
        arm = self.arms.get(iface)
        return arm.read_raw_all() if arm else {}

    def set_hardware_zero(self, iface: str, joint: str) -> dict:
        """지금 위치를 그 관절의 하드웨어 영점으로 굽는다. **되돌릴 수 없다.**

        ⚠ 소프트웨어 캘리브레이션(`joints.JOINT_CALIBRATION`)이 아니다.
          모터 플래시에 쓰는 것이라 전원을 꺼도 남고, raw 값의 의미가 바뀐다.
        """
        arm = self.arms.get(iface)
        if arm is None:
            return {"ok": False, "error": f"{iface} 를 모릅니다"}
        return arm.set_hardware_zero(joint)

    # ── 안전 설정 ──
    #
    # 브리지가 아니라 **매니저**가 들고 있다 (팔을 뽑았다 꽂아도 유지돼야 한다).
    # 허브는 그 앞의 RPC 창구일 뿐이다.

    def get_safety(self) -> dict:
        from piper_robot import publish, safety_store
        return safety_store.as_dict(publish.arm_bridge_manager.floor_config())

    def set_safety(self, patch: dict) -> dict:
        """바닥 필터 설정 변경. `cm` 로 받아 `m` 로 저장한다."""
        from piper_robot import publish, safety_store

        out = dict(patch)
        # UI 는 cm 로 말한다 — 여기서 한 번만 바꾼다. 양쪽에서 바꾸면 언젠가
        # 100 배 틀린 값이 저장된다.
        if "min_z_cm" in out:
            out["min_z"] = float(out.pop("min_z_cm")) / 100.0
        return safety_store.as_dict(publish.arm_bridge_manager.set_floor(out))

    def read_end_pose(self, iface: str) -> dict | None:
        arm = self.arms.get(iface)
        return arm.read_end_pose() if arm else None

    def disable_all_torque(self) -> list[str]:
        """쥐고 있는 팔의 토크를 끊는다 — **마스터는 빼고.** 끊은 iface 목록을 돌려준다.

        E-stop 이 여기로 온다. 하나가 실패해도 나머지는 계속 — 부분 성공이라도
        해야 한다(estopd 가 PID 를 죽일 때와 같은 규율).

        ⚠ **팔이 중력으로 떨어진다.** 그게 대가고, E-stop 이 그걸 감수하는
        이유는 사람이 팔에 끼었을 때 손으로 빼낼 수 있어야 하기 때문이다.
        데드맨(연결 끊김)은 반대로 그 자리에 선다 — `safety.filter_goal` 참고.
        """
        done, skipped = [], []
        for iface, arm in list(self.arms.items()):
            # ⚠ **마스터 팔은 건드리지 않는다.**
            #
            # 마스터(示教输入臂)는 사람이 손으로 끄는 팔이라 **이미 토크가 없다** —
            # 여기서 얻는 안전이 0이다. 그런데 `DisablePiper()` 가 모터를 끄면서
            # 팔의 연동 설정까지 풀어버려서, E-stop 이 날 때마다 리더가 슬레이브로
            # 돌아갔다. 그러면 팔로워가 리더를 안 따라오고 아무 에러도 안 난다.
            # SDK 문서에 없는 동작이라 실기에서 재현해서 확인했다.
            #
            # 판단은 **측정한 값**(`is_master`)으로 한다. 라벨(`role`)로 하면,
            # leader 라고 적혀 있지만 실제로는 슬레이브인 — 즉 토크가 살아 있는 —
            # 팔을 건너뛰게 된다. 그 어긋난 상태는 실제로 있었다.
            # 모르면(`None`) 끊는다: 판정 불가는 안전이 아니다.
            if arm.is_master is True:
                skipped.append(iface)
                continue
            try:
                if arm.disable_torque():
                    done.append(iface)
            except Exception as exc:
                logger.error("%s 토크 차단 실패: %s", iface, exc)
        if done or skipped:
            logger.warning("E-STOP → 토크 차단: %s%s",
                           ", ".join(done) or "없음",
                           f" (마스터라 건너뜀: {', '.join(skipped)})" if skipped else "")
        return done

    def go_parking(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        return bool(arm and arm.go_parking())

    # ── 움직임 감지 ──

    # ⚠ **부팅 중인 팔에 CAN 을 보내면 부팅이 깨진다.** 그래서 무슨 일이 있어도
    #   이만큼 기다린 뒤에 시작한다 — 방금 전원을 넣었을 수 있고, 여기서는 그걸
    #   알 방법이 없다. 기다리는 편이 싸다.
    IDENTIFY_BOOT_WAIT_S = 10.0

    def start_identify(self, key: str, ifaces: list[str]) -> bool:
        """**명령에 반응하는가**로 팔마다 마스터/슬레이브를 가린다.

        마스터는 외부 제어 명령을 무시하고 피드백도 안 보낸다 — 움직이지도, 관절값이
        바뀌지도 않는다. 슬레이브는 둘 다 한다.

        CAN RX 카운터로 보던 기존 `_classify_master` 보다 확실하다: 그쪽은 "피드백이
        오는가"라는 **간접 증거**라 케이블이나 타이밍에 흔들리는데, 이건 팔에 직접
        물어보는 것이다.
        """
        candidates = [self.arms[i] for i in ifaces
                      if i in self.arms and self.arms[i].connected]
        if not candidates:
            return False
        self._motion[key] = {"status": "waiting",
                             "phase": "부팅 중인 팔을 깨뜨리지 않으려고 대기",
                             "remaining": self.IDENTIFY_BOOT_WAIT_S, "results": {}}
        threading.Thread(target=self._identify, args=(key, candidates),
                         daemon=True).start()
        return True

    def _identify(self, key: str, candidates: list[Arm]) -> None:
        # 1) 부팅 보호 대기. 남은 시간을 계속 알려 준다 — 10초 동안 아무 표시가
        #    없으면 사용자는 멈춘 줄 안다.
        start = time.monotonic()
        while True:
            left = self.IDENTIFY_BOOT_WAIT_S - (time.monotonic() - start)
            if left <= 0:
                break
            self._motion[key] = {
                "status": "waiting",
                # 왜 기다리는지 적는다 — "그냥 느린 것"으로 보이면 다음 사람이 지운다
                "phase": "부팅 중인 팔을 깨뜨리지 않으려고 대기",
                "remaining": round(left, 1), "results": {},
            }
            time.sleep(0.1)

        # 2) 한 팔씩 건드린다. 동시에 하면 어느 팔이 움직였는지 눈으로 못 가린다.
        results: dict[str, dict] = {}
        total = len(candidates)
        for n, arm in enumerate(candidates, 1):
            def on_step(text: str, remaining: float = 0.0, _a=arm, _n=n) -> None:
                # 단계마다 화면을 갱신한다 — 이 절차는 몇 초를 조용히 보내는데,
                # 아무 변화가 없으면 **멈춘 것과 구분이 안 된다.**
                self._motion[key] = {
                    "status": "probing", "phase": text, "iface": _a.iface,
                    "index": _n, "total": total,
                    "remaining": remaining, "results": dict(results),
                }

            on_step("시작하는 중")
            try:
                r = arm.probe_command_response(on_step=on_step)
            except Exception as exc:
                logger.warning("%s 판별 실패: %s", arm.iface, exc)
                r = {"ok": False, "error": str(exc)}
            if r.get("ok"):
                r["role"] = "master" if r["is_master"] else "slave"
            results[arm.iface] = r
            logger.info("판별 %s: %s", arm.iface, r)

        self._motion[key] = {"status": "done", "phase": "완료", "remaining": 0,
                             "results": results}

    def start_motion_detect(self, key: str, ifaces: list[str]) -> bool:
        """후보 팔 중 **어느 것이 움직였는지** 찾는다.

        슬롯 배정은 하지 않는다 — 게이트웨이가 `found_iface` 를 보고 정한다.
        """
        candidates = [self.arms[i] for i in ifaces if i in self.arms and self.arms[i].connected]
        if not candidates:
            return False
        self._motion[key] = {"status": "detecting", "remaining": FIND_TIMEOUT_SEC,
                             "max_delta": 0, "found_iface": None}
        threading.Thread(target=self._detect_motion, args=(key, candidates),
                         daemon=True).start()
        return True

    def _detect_motion(self, key: str, candidates: list[Arm]) -> None:
        baselines = {a.iface: a.read_joints_raw() for a in candidates}
        start = time.monotonic()
        best_iface, best_delta = None, 0
        while time.monotonic() - start < FIND_TIMEOUT_SEC:
            time.sleep(0.1)
            remaining = FIND_TIMEOUT_SEC - (time.monotonic() - start)
            for arm in candidates:
                baseline = baselines[arm.iface]
                if baseline is None:
                    baselines[arm.iface] = arm.read_joints_raw()
                    continue
                current = arm.read_joints_raw()
                if current is None:
                    continue
                delta = max(abs(c - b) for c, b in zip(current, baseline))
                if delta > best_delta:
                    best_delta, best_iface = delta, arm.iface
            self._motion[key] = {
                "status": "detecting", "remaining": max(0, round(remaining, 1)),
                "max_delta": best_delta, "threshold": FIND_THRESHOLD_RAW,
                "found_iface": None,
            }
            if best_delta >= FIND_THRESHOLD_RAW and best_iface:
                self._motion[key] = {
                    "status": "found", "remaining": 0, "max_delta": best_delta,
                    "threshold": FIND_THRESHOLD_RAW, "found_iface": best_iface,
                }
                return
        self._motion[key] = {
            "status": "timeout", "remaining": 0, "max_delta": best_delta,
            "threshold": FIND_THRESHOLD_RAW, "found_iface": None,
        }

    def motion_status(self, key: str) -> dict:
        return self._motion.get(key, {"status": "idle"})

    # ── CAN 인터페이스 관리 ──

    def init_interface(self, iface: str, bitrate: int) -> tuple[bool, str]:
        from piper_robot.can import init_can_interface

        return init_can_interface(iface, bitrate)

    def check_active(self, iface: str, interval: float = 0.3) -> bool:
        """게이트웨이 컨테이너는 브리지 네트워크라 `can0`/`can1` 자체가 안 보인다
        (network_mode: host 를 뺐다) — sysfs rx 카운터를 읽으려면 여기(호스트)를 거쳐야 한다."""
        from piper_robot.can import check_can_active

        return check_can_active(iface, interval)

    def sniff_ids(self, iface: str, duration: float = 1.2) -> dict:
        """raw CAN 소켓도 마찬가지로 호스트 네트워크 네임스페이스가 있어야 열린다."""
        from piper_robot.can import sniff_can_ids

        return sniff_can_ids(iface, duration)

    def rename_interface(self, old: str, new: str) -> tuple[bool, str]:
        from piper_robot.can import rename_can_interface

        ok, msg = rename_can_interface(old, new)
        if ok:
            self.arms.pop(old, None)
            self.scan()
        return ok, msg

    def recover_usb(self, pci_addrs: list[str] | None = None) -> tuple[bool, str, list[str]]:
        """xHCI 컨트롤러 리바인딩. **CAN 어댑터와 카메라가 함께 사라졌을 때** 쓴다."""
        from piper_robot.can import recover_usb_controllers

        return recover_usb_controllers(pci_addrs)

    def usb_info(self) -> dict:
        from piper_robot.can import get_usb_info

        return get_usb_info()


def _up_directions(arm, now_norm: dict, joints: list[str]) -> dict[str, int]:
    """손목 관절마다 **어느 부호가 팔을 들어올리나**.

    ⚠ 관절 부호 규약은 팔마다 다르고 지금 자세에 따라서도 달라진다 — 추측하면
    반대로 내리찍는다. 기구학으로 양쪽을 실제로 재서 **최저점이 높아지는 쪽**을
    고른다. 말단에 달린 것이 걸리는 곳은 아래이므로 최저점이 기준이다.
    """
    import numpy as np
    from piper_robot import diagnostics as D
    from piper_robot import kinematics as K
    from piper_robot.joints import denormalize_joint, normalize_joint

    out: dict[str, int] = {}
    if not K.available():
        return out
    try:
        base = [now_norm[j] for j in K.ARM_JOINTS]
    except KeyError:
        return out                      # 관절값이 다 없으면 판정하지 않는다
    for name in joints:
        if name not in D.WRIST_JOINTS or name not in K.ARM_JOINTS:
            continue
        idx = K.ARM_JOINTS.index(name)
        zs = {}
        for sign in (1, -1):
            q = list(base)
            deg = denormalize_joint(name, q[idx]) / 1000.0 + sign * 10.0
            q[idx] = normalize_joint(name, deg * 1000.0)
            try:
                zs[sign] = float(K.lowest_z(K.norm_to_rad(np.array([q])))[0])
            except Exception:
                return out
        if zs:
            out[name] = 1 if zs[1] >= zs[-1] else -1
    return out
