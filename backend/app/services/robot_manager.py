"""
로봇(Piper arm) 관리 서비스.
CAN 포트 스캔, 연결, 역할 감지, 움직임 감지, 설정값, 프리셋 저장/불러오기.
"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 상수 ──
FIND_THRESHOLD_RAW = 45_000
FIND_TIMEOUT_SEC = 30
DEFAULT_BITRATE = 1_000_000
CONFIG_PATH = Path.home() / "piper_config.json"
PRESETS_DIR = Path.home() / ".config" / "piper-web" / "presets"

CONFIGS: dict[str, list[str]] = {
    "1 Leader / 1 Follower": ["leader_1", "follower_1"],
    "2 Followers": ["follower_1", "follower_2"],
    "2 Leaders": ["leader_1", "leader_2"],
    "2 Leaders / 2 Followers": ["leader_1", "leader_2", "follower_1", "follower_2"],
}


def _run_cmd(cmd: list[str], sudo: bool = False) -> tuple[int, str, str]:
    if sudo:
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
        result.append({"iface": iface, "bus_info": bus_info, "state": state})
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


# ── Arm 데이터 ──

@dataclass
class ArmInfo:
    iface: str
    bus_info: str
    state: str = "DOWN"
    connected: bool = False
    role: str = "unknown"
    ctrl_mode: str = ""
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
            try:
                ctrl_mode = piper.GetArmStatus().arm_status.ctrl_mode
                mode_int = ctrl_mode.value if hasattr(ctrl_mode, "value") else int(ctrl_mode)
                mode_names = {
                    0x00: "Standby", 0x01: "CAN ctrl", 0x02: "Teaching",
                    0x03: "Ethernet", 0x04: "WiFi", 0x05: "Remote ctrl",
                    0x06: "Linkage teaching", 0x07: "Offline trajectory",
                }
                self.ctrl_mode = mode_names.get(mode_int, f"0x{mode_int:02X}")
                self.role = "leader" if mode_int == 0x06 else "follower"
            except Exception:
                self.ctrl_mode = "?"
                self.role = "follower"
            self.connected = True
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def disconnect(self) -> None:
        with self._lock:
            if self._piper:
                try:
                    self._piper.DisconnectPort()
                except Exception:
                    pass
                self._piper = None
            self.connected = False

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

    def list_presets(self) -> list[str]:
        if not PRESETS_DIR.exists():
            return []
        return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))

    def save_preset(self, name: str) -> None:
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "name": name,
            "robot_type": self.selected_type,
            "config_name": self.config_name,
            "arms": [],
        }
        for arm in self.arms.values():
            if arm.slot:
                data["arms"].append({
                    "slot": arm.slot,
                    "can_name": arm.iface,
                    "bus_info": arm.bus_info,
                    "role": arm.role,
                    "config": arm._config_dict(),
                })
        (PRESETS_DIR / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Preset saved: %s", name)

    def load_preset(self, name: str) -> dict | None:
        path = PRESETS_DIR / f"{name}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        # 상태 복원 (설정값만, CAN 재연결은 하지 않음)
        self.selected_type = data.get("robot_type")
        self.config_name = data.get("config_name")
        for arm_data in data.get("arms", []):
            iface = arm_data.get("can_name", "")
            if iface in self.arms:
                arm = self.arms[iface]
                arm.slot = arm_data.get("slot")
                arm.role = arm_data.get("role", "unknown")
                arm.update_config(arm_data.get("config", {}))
        return data

    def delete_preset(self, name: str) -> bool:
        path = PRESETS_DIR / f"{name}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    # ── 현재 상태 ──

    def get_current(self) -> dict:
        return {
            "selected_type": self.selected_type,
            "config_name": self.config_name,
            "config": self.load_config(),
            "arms": [a.to_dict() for a in self.arms.values()],
        }


robot_manager = RobotManager()
