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

from piper_robot.arm import Arm
from piper_robot.can import scan_can_interfaces

logger = logging.getLogger(__name__)

# 움직임 감지: 이만큼 raw 가 움직이면 "이 팔이다"로 본다.
FIND_THRESHOLD_RAW = 45_000
FIND_TIMEOUT_SEC = 30


class RobotHub:
    def __init__(self) -> None:
        self.arms: dict[str, Arm] = {}
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

    def refresh_mode(self, iface: str) -> dict:
        arm = self.arms.get(iface)
        if arm is None:
            return {}
        arm.refresh_mode()
        return arm.to_dict()

    def set_master_slave(self, iface: str, master: bool) -> tuple[bool, str]:
        arm = self.arms.get(iface)
        return arm.set_master_slave(master) if arm else (False, f"Unknown interface: {iface}")

    def read_joints(self, iface: str) -> dict | None:
        arm = self.arms.get(iface)
        return arm.read_joints_normalized() if arm else None

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
            cleared = arm.clear_error()
            if before and before.get("err_code"):
                logger.warning("Arm %s had error 0x%04X before clear: %s",
                               iface, before["err_code"], before["flags"])
            report.append({"iface": iface, "error": before, "cleared": cleared})
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
        self._motion[key] = {"status": "waiting", "phase": "부팅 대기",
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
            self._motion[key] = {"status": "waiting", "phase": "부팅 대기",
                                 "remaining": round(left, 1), "results": {}}
            time.sleep(0.1)

        # 2) 한 팔씩 건드린다. 동시에 하면 어느 팔이 움직였는지 눈으로 못 가린다.
        results: dict[str, dict] = {}
        for arm in candidates:
            self._motion[key] = {"status": "probing", "phase": f"{arm.iface} 확인 중",
                                 "remaining": 0, "results": dict(results)}
            try:
                r = arm.probe_command_response()
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
