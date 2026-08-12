"""
로봇(Piper arm) 관리 서비스.
CAN 포트 스캔, 연결, 역할 감지, 움직임 감지, 설정값, 프리셋 저장/불러오기.
"""

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.core.joints import denormalize_all, normalize_all
from app.services import presets

logger = logging.getLogger(__name__)

# ── 상수 ──
FIND_THRESHOLD_RAW = 45_000
FIND_TIMEOUT_SEC = 30
DEFAULT_BITRATE = 1_000_000
CONFIG_PATH = settings.robot_config_path
PRESET_DOMAIN = "robot"
# 이관 전 경로 — 기존 프리셋을 새 스토어(presets/robot/)로 한 번 옮긴다
_LEGACY_PRESETS_DIR = settings.config_dir / "presets"

CONFIGS: dict[str, list[str]] = {
    "1 Leader / 1 Follower": ["leader_1", "follower_1"],
    "2 Followers": ["follower_1", "follower_2"],
    "2 Leaders": ["leader_1", "leader_2"],
    "2 Leaders / 2 Followers": ["leader_1", "leader_2", "follower_1", "follower_2"],
}


def _is_root() -> bool:
    """이미 root면 sudo가 불필요하다 (컨테이너 실행 시 sudo 자체가 미설치)."""
    return os.geteuid() == 0


def _run_cmd(cmd: list[str], sudo: bool = False) -> tuple[int, str, str]:
    if sudo and not _is_root():
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


def slot_to_can_name(slot: str) -> str:
    role, num = slot.rsplit("_", 1)
    return f"can_{role}{num}"


# ── CAN 포트 스캔 ──

def _read_can_rx(iface: str) -> int:
    """sysfs에서 CAN RX 패킷 수 읽기 (non-blocking)."""
    try:
        return int(Path(f"/sys/class/net/{iface}/statistics/rx_packets").read_text().strip())
    except Exception:
        return 0


def sniff_can_ids(iface: str, duration: float = 1.2) -> dict:
    """raw CAN 소켓으로 버스를 잠깐 청취해 CAN ID 그룹별 빈도와 마스터/슬레이브 정황을 반환.

    Piper CAN ID 규약:
      - 0x2A1~0x2A8 : 슬레이브/standby 팔의 주기 피드백 (기본)
      - 0x2B1~0x2C8 : 마스터(示教输入臂)의 오프셋 피드백 (MasterSlaveConfig feedback_offset)
      - 0x150~0x15F : 마스터가 송신하는 관절 제어지령 (위치 변화 시)
      - 0x251~0x266 : 드라이버 고속/저속 피드백
    마스터 정황 = 0x2Bx/0x2Cx 피드백 또는 0x15x 제어지령 관측.
    """
    import socket
    import struct
    groups = {"slave_fb": 0, "master_fb": 0, "master_ctrl": 0, "driver": 0, "other": 0}
    ids: dict[int, int] = {}
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((iface,))
        s.settimeout(0.2)
        end = time.time() + duration
        while time.time() < end:
            try:
                frame = s.recv(16)
            except socket.timeout:
                continue
            cid = struct.unpack("=I", frame[:4])[0] & 0x1FFFFFFF
            ids[cid] = ids.get(cid, 0) + 1
            if 0x2A1 <= cid <= 0x2A8:
                groups["slave_fb"] += 1
            elif 0x2B1 <= cid <= 0x2C8:
                groups["master_fb"] += 1
            elif 0x150 <= cid <= 0x15F:
                groups["master_ctrl"] += 1
            elif 0x251 <= cid <= 0x266:
                groups["driver"] += 1
            else:
                groups["other"] += 1
        s.close()
    except Exception as exc:
        return {"iface": iface, "error": str(exc), "total": 0,
                "groups": groups, "ids": [], "master_detected": False, "has_traffic": False}
    master = groups["master_fb"] > 0 or groups["master_ctrl"] > 0
    return {
        "iface": iface,
        "total": sum(ids.values()),
        "has_traffic": bool(ids),
        "master_detected": master,
        "groups": groups,
        "ids": [f"{x:03X}" for x in sorted(ids)],
    }


def check_can_active(iface: str, interval: float = 0.3) -> bool:
    """CAN 포트에 실제 데이터가 오고 있는지 확인. (interval 초 간격으로 rx 증가 여부)"""
    rx1 = _read_can_rx(iface)
    import time
    time.sleep(interval)
    rx2 = _read_can_rx(iface)
    return rx2 > rx1


def scan_can_interfaces() -> list[dict]:
    rc, out, _ = _run_cmd(["ip", "-br", "link", "show", "type", "can"])
    if rc != 0 or not out.strip():
        return []
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        iface, state = parts[0], parts[1]
        bus_info = ""
        rc2, out2, _ = _run_cmd(["ethtool", "-i", iface], sudo=True)
        if rc2 == 0:
            for ln in out2.splitlines():
                if ln.strip().startswith("bus-info:"):
                    bus_info = ln.split(":", 1)[1].strip()
                    break
        rx = _read_can_rx(iface) if state == "UP" else 0
        result.append({"iface": iface, "bus_info": bus_info, "state": state, "rx_packets": rx})
    return result


def init_can_interface(iface: str, bitrate: int = DEFAULT_BITRATE) -> tuple[bool, str]:
    _run_cmd(["modprobe", "gs_usb"], sudo=True)
    _run_cmd(["ip", "link", "set", iface, "down"], sudo=True)
    rc, _, err = _run_cmd(
        ["ip", "link", "set", iface, "type", "can", "bitrate", str(bitrate)], sudo=True
    )
    if rc != 0:
        return False, f"set bitrate failed: {err}"
    rc, _, err = _run_cmd(["ip", "link", "set", iface, "up"], sudo=True)
    if rc != 0:
        return False, f"bring-up failed: {err}"
    return True, "OK"


def rename_can_interface(old_name: str, new_name: str) -> tuple[bool, str]:
    _run_cmd(["ip", "link", "set", old_name, "down"], sudo=True)
    rc, _, err = _run_cmd(["ip", "link", "set", old_name, "name", new_name], sudo=True)
    if rc != 0:
        return False, f"rename failed: {err}"
    rc, _, err = _run_cmd(["ip", "link", "set", new_name, "up"], sudo=True)
    if rc != 0:
        return False, f"bring-up failed: {err}"
    return True, "OK"


# ── USB 진단 / 복구 ──

XHCI_DRIVER_DIR = Path("/sys/bus/pci/drivers/xhci_hcd")


def get_usb_info() -> dict:
    """lsusb 출력(목록 + 트리)을 반환. root 권한 불필요."""
    rc1, flat, err1 = _run_cmd(["lsusb"])
    rc2, tree, err2 = _run_cmd(["lsusb", "-t"])
    return {
        "flat": flat if rc1 == 0 else f"(lsusb 실패: {err1})",
        "tree": tree if rc2 == 0 else f"(lsusb -t 실패: {err2})",
        "controllers": list_xhci_controllers(),
    }


def list_xhci_controllers() -> list[str]:
    """xhci_hcd 드라이버에 바인딩된 PCI 컨트롤러 주소 목록."""
    try:
        return sorted(
            n for n in (p.name for p in XHCI_DRIVER_DIR.iterdir())
            if n[:4].isalnum() and ":" in n and "." in n
        )
    except Exception:
        return []


def _sysfs_write(path: str, value: str) -> tuple[int, str, str]:
    """sudo tee로 sysfs에 기록 (값은 stdin으로 전달). root면 직접 쓴다."""
    if _is_root():
        try:
            Path(path).write_text(value)
            return 0, "", ""
        except Exception as exc:
            return -1, "", str(exc)
    cmd = ["sudo", "-n", "/usr/bin/tee", path]
    try:
        r = subprocess.run(cmd, input=value, capture_output=True, text=True, timeout=8)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


def recover_usb_controllers(pci_addrs: list[str] | None = None) -> tuple[bool, str, list[str]]:
    """xHCI 컨트롤러를 unbind→bind 하여 강제 재열거.

    'HC died'로 USB 트리가 통째로 사라졌을 때 재부팅 없이 복구한다.
    pci_addrs 미지정 시 바인딩된 모든 xhci_hcd 컨트롤러 대상.
    반환: (성공여부, 메시지, 처리된 주소 목록)
    """
    addrs = pci_addrs or list_xhci_controllers()
    if not addrs:
        return False, "xhci_hcd 컨트롤러를 찾을 수 없습니다", []
    done, errors = [], []
    for addr in addrs:
        rc, _, err = _sysfs_write(str(XHCI_DRIVER_DIR / "unbind"), addr)
        if rc != 0:
            errors.append(f"{addr} unbind: {err or 'failed'}")
            continue
        time.sleep(1.0)
        rc, _, err = _sysfs_write(str(XHCI_DRIVER_DIR / "bind"), addr)
        if rc != 0:
            errors.append(f"{addr} bind: {err or 'failed'}")
            continue
        done.append(addr)
        logger.info("USB controller rebound: %s", addr)
    if errors:
        return bool(done), "; ".join(errors), done
    return True, "OK", done


# ── Arm 데이터 ──

@dataclass
class ArmInfo:
    iface: str
    bus_info: str
    state: str = "DOWN"
    connected: bool = False
    role: str = "unknown"
    ctrl_mode: str = ""
    is_master: bool | None = None  # 하드웨어 마스터(示教输入)/슬레이브(运动输出) 모드. None=미확인
    firmware: str = ""
    slot: str | None = None
    ready: bool = False  # 등록 완료 → 사용 가능 리스트에 올라감
    # ── 설정값 ──
    # follower
    disable_torque_on_disconnect: bool = True
    max_relative_target: float | None = None
    cameras: dict = field(default_factory=dict)
    # leader
    gripper_open_pos: float = 50.0
    # 내부
    _piper: object = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict:
        return {
            "iface": self.iface,
            "bus_info": self.bus_info,
            "state": self.state,
            "connected": self.connected,
            "role": self.role,
            "ctrl_mode": self.ctrl_mode,
            "master_slave": (None if self.is_master is None
                             else "master" if self.is_master else "slave"),
            "firmware": self.firmware,
            "slot": self.slot,
            "ready": self.ready,
            "config": self._config_dict(),
        }

    def _config_dict(self) -> dict:
        """역할에 따른 설정값."""
        if self.role == "leader":
            return {"gripper_open_pos": self.gripper_open_pos}
        return {
            "disable_torque_on_disconnect": self.disable_torque_on_disconnect,
            "max_relative_target": self.max_relative_target,
            "cameras": self.cameras,
        }

    def update_config(self, cfg: dict) -> None:
        if "disable_torque_on_disconnect" in cfg:
            self.disable_torque_on_disconnect = cfg["disable_torque_on_disconnect"]
        if "max_relative_target" in cfg:
            self.max_relative_target = cfg["max_relative_target"]
        if "cameras" in cfg:
            self.cameras = cfg["cameras"]
        if "gripper_open_pos" in cfg:
            self.gripper_open_pos = cfg["gripper_open_pos"]

    def connect(self, bitrate: int = DEFAULT_BITRATE) -> tuple[bool, str]:
        ok, msg = init_can_interface(self.iface, bitrate)
        if not ok:
            return False, msg
        try:
            from piper_sdk import C_PiperInterface_V2
        except ImportError:
            return False, "piper_sdk not installed"
        try:
            piper = C_PiperInterface_V2(self.iface, judge_flag=False, can_auto_init=False)
            piper.CreateCanBus(self.iface)
            piper.ConnectPort(piper_init=False, start_thread=True)
            try:
                piper.SearchPiperFirmwareVersion()
            except Exception:
                pass
            for _ in range(5):
                time.sleep(0.2)
                fw = piper.GetPiperFirmwareVersion()
                if isinstance(fw, str):
                    self.firmware = fw
                    break
            with self._lock:
                self._piper = piper
            mode_int = self.refresh_ctrl_mode()
            if mode_int is None:
                self.ctrl_mode = "?"
            self._classify_master(mode_int)  # RX 유무로 마스터/슬레이브 판별
            # 역할 기본값은 **마스터/슬레이브 판별 결과를 따른다.**
            #
            # 예전에는 `ctrl_mode == 0x06` 만 보고 role 을 정했는데, 마스터가 되는 길은
            # 둘이다 — 0x06 을 보고하거나, RX 가 없거나(피드백을 송신하지 않는 팔).
            # 전원을 껐다 켜면 마스터 설정이 풀려 Standby(0x00)를 보고하므로
            # **RX 규칙으로만 마스터로 잡히고 role 은 follower 로 남았다.**
            # 사용자가 원하면 `set_role()` 로 언제든 바꿀 수 있다.
            self.role = "leader" if self.is_master else "follower"
            self.connected = True
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def refresh_ctrl_mode(self) -> int | None:
        """GetArmStatus로 현재 제어 모드를 다시 읽어 ctrl_mode 텍스트 갱신.

        반환: mode_int (읽기 실패 시 None — 이 경우 기존 값 유지)
        """
        with self._lock:
            if not self._piper:
                return None
            try:
                ctrl_mode = self._piper.GetArmStatus().arm_status.ctrl_mode
                mode_int = ctrl_mode.value if hasattr(ctrl_mode, "value") else int(ctrl_mode)
            except Exception:
                return None
        mode_names = {
            0x00: "Standby", 0x01: "CAN ctrl", 0x02: "Teaching",
            0x03: "Ethernet", 0x04: "WiFi", 0x05: "Remote ctrl",
            0x06: "Linkage teaching", 0x07: "Offline trajectory",
        }
        self.ctrl_mode = mode_names.get(mode_int, f"0x{mode_int:02X}")
        return mode_int

    def _classify_master(self, mode_int: int | None = None, rx_interval: float = 0.3) -> None:
        """마스터/슬레이브 판별 (사용자 지정 규칙).

        - RX 데이터가 있으면 슬레이브: 슬레이브 팔은 주기 피드백(0x2Ax)을 계속 송신한다.
        - RX 데이터가 없으면 마스터: 마스터(示教输入臂)는 피드백을 송신하지 않아 RX가 비어있다.
        - ctrl_mode 0x06(연동 示教입력)이면 명시적 마스터로 본다.
        """
        rx1 = _read_can_rx(self.iface)
        time.sleep(rx_interval)
        rx2 = _read_can_rx(self.iface)
        self.is_master = (mode_int == 0x06) or (rx2 == rx1)

    def refresh_mode(self) -> None:
        """ctrl_mode 텍스트 + 마스터/슬레이브를 라이브 갱신.

        ⚠ **`role` 은 건드리지 않는다.** 이건 `/robots/current` 폴링마다 불리므로,
        여기서 role 을 덮으면 사용자가 `set_role()` 로 고른 값이 조용히 되돌아간다.
        자동 판별은 연결 시점(`connect`)과 마스터/슬레이브를 직접 바꿀 때만 한다.
        """
        mode_int = self.refresh_ctrl_mode()
        self._classify_master(mode_int)

    def set_master_slave(self, master: bool) -> tuple[bool, str]:
        """팔을 마스터(示教输入臂, 0xFA) 또는 슬레이브(运动输出臂, 0xFC)로 설정.

        설정 후 마스터는 ctrl_mode 0x06(Linkage teaching)을 보고하며 제어지령(0x15x)을
        송신한다. 전원 재투입 시 풀릴 수 있으므로 재설정이 필요할 수 있다.
        """
        with self._lock:
            if not self._piper:
                return False, "연결되지 않음"
            try:
                self._piper.MasterSlaveConfig(0xFA if master else 0xFC, 0, 0, 0)
            except Exception as e:
                return False, str(e)
        time.sleep(0.4)  # 모드 전환 반영 대기
        self.refresh_mode()
        # 사용자가 명시적으로 바꿨으므로 역할도 따라간다 — 마스터로 바꿔놓고
        # 화면에 follower 로 남아 있으면 슬롯 배정에서 그대로 어긋난다.
        self.role = "leader" if master else "follower"
        return True, "OK"

    def disconnect(self) -> None:
        with self._lock:
            if self._piper:
                try:
                    self._piper.DisconnectPort()
                except Exception:
                    pass
                self._piper = None
            self.connected = False
            self.is_master = None
            self.ctrl_mode = ""

    def read_joints_raw(self) -> list[int] | None:
        with self._lock:
            if not self._piper:
                return None
            try:
                j = self._piper.GetArmJointCtrl().joint_ctrl
                ctrl = [j.joint_1, j.joint_2, j.joint_3, j.joint_4, j.joint_5, j.joint_6]
                if any(v != 0 for v in ctrl):
                    return ctrl
            except Exception:
                pass
            try:
                j = self._piper.GetArmJointMsgs().joint_state
                return [j.joint_1, j.joint_2, j.joint_3, j.joint_4, j.joint_5, j.joint_6]
            except Exception:
                return None

    def read_joints_normalized(self) -> dict[str, float] | None:
        """정규화된 관절 위치 읽기 (joint1~6 + gripper)."""
        with self._lock:
            if not self._piper:
                return None
            try:
                j = self._piper.GetArmJointMsgs().joint_state
                g = self._piper.GetArmGripperMsgs().gripper_state
                raw = {
                    "joint1": float(j.joint_1), "joint2": float(j.joint_2),
                    "joint3": float(j.joint_3), "joint4": float(j.joint_4),
                    "joint5": float(j.joint_5), "joint6": float(j.joint_6),
                    "gripper": float(g.grippers_angle),
                }
                return normalize_all(raw)
            except Exception as e:
                logger.debug("read_joints_normalized error: %s", e)
                return None

    def go_parking(self) -> bool:
        """파킹 위치로 이동."""
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.EnablePiper()
                time.sleep(0.3)
                # 커스텀 파킹 위치 또는 기본값
                parking_pos = _load_custom_parking(self.iface)
                from lerobot_robot_piper.motors.tables import INITIALIZE_POSITION
                target = parking_pos or INITIALIZE_POSITION
                # set_action 직접 호출 (정규화 값 → raw 변환)
                raw = denormalize_all(target)

                self._piper.ModeCtrl(0x01, 0x01, 30, 0x00)
                self._piper.JointCtrl(
                    raw["joint1"], raw["joint2"], raw["joint3"],
                    raw["joint4"], raw["joint5"], raw["joint6"],
                )
                self._piper.GripperCtrl(abs(raw["gripper"]), 1000, 0x03, 0)
                return True
            except Exception as e:
                logger.error("go_parking error: %s", e)
                return False

    def enable_torque(self) -> bool:
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.EnablePiper()
                return True
            except Exception:
                return False

    def disable_torque(self) -> bool:
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.DisablePiper()
                return True
            except Exception:
                return False

    # ── 에러 플래그 ──

    # err_code 비트 → 의미 (arm_feedback_status.ErrStatus 참조)
    _ERR_BITS = {
        0: "joint1_comm", 1: "joint2_comm", 2: "joint3_comm",
        3: "joint4_comm", 4: "joint5_comm", 5: "joint6_comm",
        8: "joint1_angle_limit", 9: "joint2_angle_limit", 10: "joint3_angle_limit",
        11: "joint4_angle_limit", 12: "joint5_angle_limit", 13: "joint6_angle_limit",
    }

    def read_error(self) -> dict | None:
        """현재 팔의 에러 코드/플래그를 읽는다. 연결 안 됐거나 실패 시 None."""
        with self._lock:
            if not self._piper:
                return None
            try:
                err_code = int(self._piper.GetArmStatus().arm_status.err_code)
            except Exception as e:
                logger.debug("read_error failed on %s: %s", self.iface, e)
                return None
        flags = [name for bit, name in self._ERR_BITS.items() if err_code & (1 << bit)]
        return {"err_code": err_code, "flags": flags}

    def clear_error(self) -> bool:
        """팔의 에러를 무조건 클리어한다 (급정지/일시정지 해제 + 전 관절 에러코드 클리어).

        공식 reset 데모(piper_ctrl_reset.py)의 MotionCtrl_1(0x02) '恢复'와
        JointConfig clear_err=0xAE(전 관절 에러코드 클리어)를 함께 보낸다.
        """
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.MotionCtrl_1(0x02, 0, 0)        # 급정지/일시정지 해제(恢复)
                time.sleep(0.01)
                self._piper.JointConfig(joint_num=7, clear_err=0xAE)  # 전 관절 에러코드 클리어
                return True
            except Exception as e:
                logger.error("clear_error failed on %s: %s", self.iface, e)
                return False


# ── 세션/파킹 저장 경로 ──
SESSION_DIR = settings.config_dir
PARKING_DIR = SESSION_DIR / "parking"
ROBOT_SESSION_PATH = SESSION_DIR / "robot_session.json"


def _load_custom_parking(iface: str) -> dict | None:
    path = PARKING_DIR / f"{iface}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _save_custom_parking(iface: str, positions: dict) -> None:
    PARKING_DIR.mkdir(parents=True, exist_ok=True)
    (PARKING_DIR / f"{iface}.json").write_text(json.dumps(positions, indent=2))
    logger.info("Saved custom parking for %s: %s", iface, positions)


# ── Robot Manager ──

class RobotManager:
    def __init__(self) -> None:
        self.arms: dict[str, ArmInfo] = {}
        self.selected_type: str | None = None
        self.config_name: str | None = None
        self._motion_task: dict = {}

    # ── 스캔/연결 ──

    def scan(self) -> list[dict]:
        ports = scan_can_interfaces()
        for p in ports:
            iface = p["iface"]
            if iface not in self.arms:
                self.arms[iface] = ArmInfo(iface=iface, bus_info=p["bus_info"], state=p["state"])
            else:
                self.arms[iface].bus_info = p["bus_info"]
                self.arms[iface].state = p["state"]
        return [a.to_dict() for a in self.arms.values()]

    def connect_arm(self, iface: str) -> tuple[bool, str]:
        arm = self.arms.get(iface)
        if not arm:
            return False, f"Unknown interface: {iface}"
        return arm.connect()

    def disconnect_arm(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        if not arm:
            return False
        arm.disconnect()
        return True

    # ── 역할/슬롯 ──

    def set_role(self, iface: str, role: str) -> bool:
        arm = self.arms.get(iface)
        if not arm or role not in ("leader", "follower"):
            return False
        arm.role = role
        # 역할 변경 시 슬롯 해제
        arm.slot = None
        return True

    def assign_slot(self, iface: str, slot: str) -> bool:
        arm = self.arms.get(iface)
        if not arm:
            return False
        for a in self.arms.values():
            if a.slot == slot:
                a.slot = None
        arm.slot = slot
        arm.role = "leader" if "leader" in slot else "follower"
        return True

    # ── 등록 (사용 가능 리스트) ──

    def register_arm(self, iface: str) -> bool:
        """팔을 사용 가능 상태로 등록. 연결 + 역할이 지정되어 있어야 함."""
        arm = self.arms.get(iface)
        if not arm or not arm.connected or arm.role == "unknown":
            return False
        arm.ready = True
        return True

    def unregister_arm(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        if not arm:
            return False
        arm.ready = False
        return True

    def get_ready_arms(self) -> list[dict]:
        """등록 완료된 팔 목록."""
        return [a.to_dict() for a in self.arms.values() if a.ready]

    # ── 설정값 ──

    def update_arm_config(self, iface: str, cfg: dict) -> bool:
        arm = self.arms.get(iface)
        if not arm:
            return False
        arm.update_config(cfg)
        return True

    # ── 에러 클리어 ──

    def clear_arm_errors(self, ifaces: list[str] | None = None) -> list[dict]:
        """팔 에러를 조회 후 무조건 클리어한다.

        ifaces 미지정 시 연결된 모든 follower 대상. 추론 시작/종료 시 호출.
        반환: 팔별 {iface, error(클리어 전 상태), cleared} 리포트.
        """
        report = []
        for arm in self.arms.values():
            if not arm.connected:
                continue
            if ifaces is not None:
                if arm.iface not in ifaces:
                    continue
            elif arm.role != "follower":
                continue
            before = arm.read_error()
            cleared = arm.clear_error()
            if before and before.get("err_code"):
                logger.warning("Arm %s had error 0x%04X before clear: %s",
                               arm.iface, before["err_code"], before["flags"])
            report.append({"iface": arm.iface, "error": before, "cleared": cleared})
        return report

    # ── 움직임 감지 ──

    def start_motion_detect(self, slot: str) -> bool:
        unassigned = [a for a in self.arms.values() if a.connected and a.slot is None]
        if not unassigned:
            return False
        self._motion_task[slot] = {
            "status": "detecting", "remaining": FIND_TIMEOUT_SEC,
            "max_delta": 0, "found_iface": None,
        }
        threading.Thread(target=self._detect_motion, args=(slot, unassigned), daemon=True).start()
        return True

    def _detect_motion(self, slot: str, candidates: list[ArmInfo]) -> None:
        baselines: dict[str, list[int] | None] = {}
        for arm in candidates:
            baselines[arm.iface] = arm.read_joints_raw()
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
                    best_delta = delta
                    best_iface = arm.iface
            self._motion_task[slot] = {
                "status": "detecting", "remaining": max(0, round(remaining, 1)),
                "max_delta": best_delta, "threshold": FIND_THRESHOLD_RAW, "found_iface": None,
            }
            if best_delta >= FIND_THRESHOLD_RAW and best_iface:
                self.assign_slot(best_iface, slot)
                self._motion_task[slot] = {
                    "status": "found", "remaining": 0,
                    "max_delta": best_delta, "threshold": FIND_THRESHOLD_RAW, "found_iface": best_iface,
                }
                return
        self._motion_task[slot] = {
            "status": "timeout", "remaining": 0,
            "max_delta": best_delta, "threshold": FIND_THRESHOLD_RAW, "found_iface": None,
        }

    def get_motion_status(self, slot: str) -> dict:
        return self._motion_task.get(slot, {"status": "idle"})

    # ── 설정 저장 (piper_config.json + CAN 이름 변경) ──

    def save_config(self, config_name: str) -> tuple[bool, str]:
        slots = CONFIGS.get(config_name)
        if not slots:
            return False, f"Unknown config: {config_name}"
        assigned = {a.slot: a for a in self.arms.values() if a.slot in slots}
        if len(assigned) != len(slots):
            missing = set(slots) - set(assigned.keys())
            return False, f"Unassigned slots: {missing}"
        for arm in assigned.values():
            arm.disconnect()
        arms_data = []
        for slot in slots:
            arm = assigned[slot]
            new_name = slot_to_can_name(slot)
            if arm.iface != new_name:
                ok, msg = rename_can_interface(arm.iface, new_name)
                if not ok:
                    return False, f"Rename {arm.iface} → {new_name} failed: {msg}"
                old_iface = arm.iface
                arm.iface = new_name
                if old_iface in self.arms:
                    del self.arms[old_iface]
                self.arms[new_name] = arm
            arms_data.append({
                "slot": slot, "can_name": new_name, "original_iface": arm.iface,
                "bus_info": arm.bus_info, "role": arm.role, "firmware": arm.firmware,
                "config": arm._config_dict(),
            })
        config_data = {"configuration": config_name, "arms": arms_data}
        CONFIG_PATH.write_text(json.dumps(config_data, indent=2))
        self.config_name = config_name
        logger.info("Robot config saved: %s", CONFIG_PATH)
        return True, "OK"

    def load_config(self) -> dict | None:
        if not CONFIG_PATH.exists():
            return None
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return None

    # ── 프리셋 ──

    @staticmethod
    def migrate_legacy_presets() -> int:
        """`config_dir/presets/*.json` → `config_dir/presets/robot/`.

        새 도메인만 새 스토어에 넣고 로봇은 그대로 두면 **프리셋 시스템이 둘이 된다.**
        서버 기동 시 한 번 돌린다. 이미 옮겼으면 아무것도 안 한다.
        """
        if not _LEGACY_PRESETS_DIR.exists():
            return 0
        moved = 0
        for f in _LEGACY_PRESETS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                name = data.pop("name", f.stem)
                if presets.get(PRESET_DOMAIN, name) is not None:
                    continue  # 이미 있음
                presets.save(PRESET_DOMAIN, name, data, scope="device",
                             note="이전 형식에서 이관됨")
                f.unlink()
                moved += 1
            except Exception as e:
                logger.warning("프리셋 이관 실패, 원본 유지: %s (%s)", f, e)
        if moved:
            logger.info("로봇 프리셋 %d개를 presets/robot/ 으로 이관했습니다", moved)
        return moved

    def list_presets(self) -> list[str]:
        return [p["name"] for p in presets.list_presets(PRESET_DOMAIN)]

    def save_preset(self, name: str) -> None:
        """현재 로봇 구성을 프리셋으로.

        ## 무엇을 "설정된 팔"로 볼 것인가

        예전에는 `slot` 이 있는 팔만 담았다. 그런데 실제 사용 흐름은
        **스캔 → 연결 → (역할 자동 판별) → 등록**이고, 이 경로는 `ready` 만 세우고
        `slot` 은 건드리지 않는다 — 슬롯 배정은 별도 구성 단계다.
        그래서 등록을 다 끝낸 사용자도 `arms: []` 인 빈 프리셋을 받았다.

        `slot` 이든 `ready` 든 **사용자가 손댄 팔**이면 담는다.

        ⚠ 하나도 없으면 거부한다. 예전에는 빈 프리셋을 조용히 저장해서,
        저장은 성공했다고 나오는데 불러오면 아무 일도 일어나지 않았다.
        """
        configured = [arm for arm in self.arms.values() if arm.slot or arm.ready]
        if not configured:
            raise ValueError(
                "저장할 구성이 없습니다 — 사용 가능 목록에 등록된 팔이 없습니다. "
                "스캔 → 연결 → 등록을 먼저 하세요."
            )
        values = {
            "robot_type": self.selected_type,
            "config_name": self.config_name,
            "arms": [
                {
                    "slot": arm.slot,
                    "can_name": arm.iface,
                    "bus_info": arm.bus_info,
                    "role": arm.role,
                    # 등록 상태도 담는다 — 안 담으면 불러와도 사용 가능 목록이 빈 채라
                    # "불러왔는데 아무것도 안 바뀐다"가 된다
                    "ready": arm.ready,
                    "config": arm._config_dict(),
                }
                for arm in configured
            ],
        }
        # CAN 이름·bus_info 는 이 기기의 것이므로 device scope
        presets.save(PRESET_DOMAIN, name, values, scope="device")

    def load_preset(self, name: str) -> dict | None:
        preset = presets.get(PRESET_DOMAIN, name)
        if preset is None:
            return None
        data = preset.values
        # 상태 복원 (설정값만, CAN 재연결은 하지 않음)
        self.selected_type = data.get("robot_type")
        self.config_name = data.get("config_name")
        for arm_data in data.get("arms", []):
            iface = arm_data.get("can_name", "")
            if iface in self.arms:
                arm = self.arms[iface]
                arm.slot = arm_data.get("slot")
                arm.role = arm_data.get("role", "unknown")
                # 등록 상태 복원. 없으면(옛 프리셋) 슬롯 유무로 추정한다.
                arm.ready = arm_data.get("ready", bool(arm_data.get("slot")))
                arm.update_config(arm_data.get("config", {}))
        # 프론트 호환: 예전 응답이 name 을 포함했다
        return {"name": name, **data}

    def delete_preset(self, name: str) -> bool:
        return presets.delete(PRESET_DOMAIN, name)

    # ── 현재 상태 ──

    def get_current(self) -> dict:
        # 연결된 팔은 ctrl_mode와 마스터/슬레이브(RX 유무)를 라이브로 다시 읽어 반영
        for arm in self.arms.values():
            if arm.connected:
                arm.refresh_mode()
        return {
            "selected_type": self.selected_type,
            "config_name": self.config_name,
            "config": self.load_config(),
            "arms": [a.to_dict() for a in self.arms.values()],
        }


    # ── 세션 저장/복원 ──

    def save_session(self) -> None:
        """현재 등록된 로봇 상태를 세션 파일에 저장."""
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "robot_type": self.selected_type,
            "config_name": self.config_name,
            "arms": [],
        }
        for arm in self.arms.values():
            if arm.ready or arm.slot:
                data["arms"].append({
                    "iface": arm.iface,
                    "bus_info": arm.bus_info,
                    "role": arm.role,
                    "slot": arm.slot,
                    "ready": arm.ready,
                    "config": arm._config_dict(),
                })
        ROBOT_SESSION_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Robot session saved (%d arms)", len(data["arms"]))

    def restore_session(self) -> bool:
        """세션 파일에서 로봇 상태 복원. 스캔 → 연결 → 설정 복원."""
        if not ROBOT_SESSION_PATH.exists():
            return False
        try:
            data = json.loads(ROBOT_SESSION_PATH.read_text())
        except Exception:
            return False

        self.selected_type = data.get("robot_type")
        self.config_name = data.get("config_name")
        saved_arms = data.get("arms", [])
        if not saved_arms:
            return False

        logger.info("Restoring robot session (%d arms)...", len(saved_arms))

        # 1. CAN 스캔
        self.scan()

        # 2. 저장된 각 팔에 대해 연결 + 설정 복원
        restored = 0
        for arm_data in saved_arms:
            iface = arm_data.get("iface", "")
            if iface not in self.arms:
                logger.warning("  Session arm %s not found in scan, skipping", iface)
                continue
            arm = self.arms[iface]
            if not arm.connected:
                ok, msg = arm.connect()
                if not ok:
                    logger.warning("  Failed to reconnect %s: %s", iface, msg)
                    continue
            arm.role = arm_data.get("role", "unknown")
            arm.slot = arm_data.get("slot")
            arm.update_config(arm_data.get("config", {}))
            if arm_data.get("ready", False):
                arm.ready = True
            restored += 1
            logger.info("  Restored %s (role=%s, ready=%s)", iface, arm.role, arm.ready)

        logger.info("Robot session restored: %d/%d arms", restored, len(saved_arms))
        return restored > 0


robot_manager = RobotManager()
