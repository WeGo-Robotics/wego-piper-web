"""로봇 — **robotd 데몬의 얇은 클라이언트** (daemon-inventory.md #2).

장치 구현은 `robot/piper_robot/` 로 옮겨졌고 `daemons/robotd.py` 가 그걸 돌린다.
여기는 공개 인터페이스를 그대로 두고 내부만 버스 RPC 로 바꾼 껍데기다 —
rsd·camerad 때와 같은 방식이라 **라우터는 한 줄도 안 바뀐다.**

## 게이트웨이에 남는 것 — 사람이 정하는 값

역할(leader/follower)·슬롯·등록 여부·카메라 매핑·프리셋·세션. CAN 과 무관하고
팔에 물어봐서 알 수 있는 것도 아니다. 이걸 데몬에 두면 같은 사실이 두 프로세스에
생기고, 재시작할 때마다 어긋난다 (카메라에서 `CameraInfo` 가 게이트웨이 기록으로
남은 것과 같은 경계다).

## 무엇이 RPC 로 가는가

**piper_sdk 를 여는 것만.** `/proc`·`/sys` 읽기(CAN 활성 확인, 프레임 스니핑)는
장치를 열지 않으므로 그대로 부른다 — RPC 로 감싸면 데몬이 죽었을 때 진단 도구까지
같이 죽는다. 반대로 xHCI 리바인딩은 robotd 가 쥔 핸들을 무효화하므로 RPC 로 보낸다.
"""

import json
import logging
import threading
from dataclasses import dataclass, field

from app.core.config import settings
from app.services import presets
from piper_bus import contract as C
from piper_bus.client import Bus

# 순수 문자열 계산이라 장치·네트워크 네임스페이스가 필요 없는 것만 그대로 쓴다.
from piper_robot.can import slot_to_can_name  # noqa: F401
# ⚠ init_can_interface·check_can_active·sniff_can_ids·scan_can_interfaces 는 여기서
# 직접 재노출하지 않는다. `ip link set ... up`은 NET_ADMIN이 필요하고, sysfs rx
# 카운터·raw CAN 소켓·`ip link show`는 `can0`/`can1`이 보이는 네트워크 네임스페이스가
# 있어야 하는데 backend 컨테이너는 `network_mode: host`를 뺀 브리지 네트워크라 둘 다 없다
# (docker-compose.yml). rename_interface·recover_usb 와 같은 이유로 robotd RPC 를 거친다.
from piper_robot.joints import denormalize_all  # noqa: F401

logger = logging.getLogger(__name__)

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

_bus_singleton: Bus | None = None


def _bus() -> Bus:
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = Bus()
    return _bus_singleton


def robotd_available() -> bool:
    try:
        return _bus().is_alive(C.ROBOTD)
    except Exception:
        return False


def _call(method: str, *args, default=None, timeout: int = C.RPC_TIMEOUT_S):
    """RPC 한 번. **실패해도 게이트웨이를 죽이지 않는다.**

    robotd 가 없거나 죽어 있어도 웹은 떠 있어야 한다 — 팔만 안 보이는 것으로
    격리되는 게 프로세스를 나눈 이유다.
    """
    # ⚠ **죽은 데몬을 기다리지 않는다.** 생존 표시가 없으면 즉시 포기한다 —
    # 안 그러면 호출마다 RPC 타임아웃(20초)을 통째로 기다려, robotd 가 내려간
    # 동안 웹이 통째로 굼떠진다. 격리하려고 프로세스를 나눈 건데 그러면 의미가 없다.
    try:
        if not _bus().is_alive(C.ROBOTD):
            return default
    except Exception:
        return default
    try:
        return _bus().rpc_call(C.ROBOTD, method, list(args), timeout=timeout)
    except TimeoutError:
        logger.warning("robotd 응답 없음 (%s) — 데몬이 떠 있나요?", method)
        return default
    except Exception as exc:
        logger.warning("robotd.%s 실패: %s", method, exc)
        return default


def _pair(result, fallback: str) -> tuple[bool, str]:
    """RPC 결과를 `(ok, msg)` 로. JSON 왕복에서 tuple 이 list 가 되므로 복원한다."""
    if isinstance(result, (list, tuple)) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return False, fallback


# ── 라우터가 직접 부르는 하드웨어 조작 (RPC) ──

def lost_arms() -> list[dict]:
    """**robotd 가 판정한** 사라진 팔. 게이트웨이가 세그먼트로 추론하지 않는다.

    ⚠ 게이트웨이는 컨테이너에서 돌고 브리지 네트워크라 `/sys/class/net` 에
    `can0` 이 안 보인다 — 인터페이스 소멸을 **볼 수가 없다.** 판정이 데몬 쪽에
    있어야 하는 이유가 하나 더 있는 셈이다.
    """
    return _call("lost", default=[]) or []


def get_usb_info() -> dict:
    return _call("usb_info", default={}) or {}


def recover_usb_controllers(pci_addrs: list[str] | None = None) -> tuple[bool, str, list[str]]:
    """xHCI 리바인딩. **robotd 를 거친다** — 데몬이 쥔 핸들이 무효가 되므로
    데몬이 스스로 정리할 수 있어야 한다."""
    r = _call("recover_usb", pci_addrs, default=None, timeout=60)
    if isinstance(r, (list, tuple)) and len(r) == 3:
        return bool(r[0]), str(r[1]), list(r[2])
    return False, "robotd 응답 없음", []


def init_can_interface(iface: str, bitrate: int = 1_000_000) -> tuple[bool, str]:
    return _pair(_call("init_interface", iface, bitrate, timeout=30), "CAN 인터페이스 초기화 실패")


def check_can_active(iface: str, interval: float = 0.3) -> bool:
    return bool(_call("check_active", iface, interval, default=False, timeout=int(interval) + 5))


def sniff_can_ids(iface: str, duration: float = 1.2) -> dict:
    empty = {"iface": iface, "total": 0, "has_traffic": False, "master_detected": False,
              "groups": {}, "ids": [], "error": "robotd 응답 없음"}
    return _call("sniff_ids", iface, duration, default=empty, timeout=int(duration) + 5) or empty


def rename_can_interface(old: str, new: str) -> tuple[bool, str]:
    return _pair(_call("rename_interface", old, new, timeout=30), "이름 변경 실패")


# ── 세션/파킹 저장 경로 ──
# ⚠ **경로를 바꾸면 사용자가 저장한 파킹 위치를 잃는다.** robotd 도 같은 파일을
# 읽는다(`PIPER_CONFIG_DIR`) — 파일 하나를 양쪽이 보는 것은 같은 사실을 두 번
# 적는 것과 다르다. 정본은 여전히 하나다.
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



@dataclass
class ArmInfo:
    """팔 하나 — **설정은 여기, 장치 상태는 robotd.**

    필드 이름·메서드 시그니처는 옛것 그대로다. 라우터가 그대로 쓴다.
    """

    iface: str
    bus_info: str = ""
    state: str = "DOWN"
    connected: bool = False
    role: str = "unknown"
    ctrl_mode: str = ""
    is_master: bool | None = None
    firmware: str = ""
    slot: str | None = None
    ready: bool = False
    # ── 설정값 (게이트웨이 소유) ──
    disable_torque_on_disconnect: bool = True
    max_relative_target: float | None = None
    cameras: dict = field(default_factory=dict)
    gripper_open_pos: float = 50.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── 데몬이 준 장치 상태를 흡수한다 ──

    def absorb(self, info: dict) -> None:
        """robotd 의 `to_dict()` 를 받아 장치 필드만 갱신한다.

        ⚠ **설정 필드는 건드리지 않는다.** 데몬은 역할·슬롯을 모르고, 알아서도 안 된다.
        """
        if not info:
            return
        self.bus_info = info.get("bus_info", self.bus_info)
        self.state = info.get("state", self.state)
        self.connected = bool(info.get("connected", self.connected))
        self.ctrl_mode = info.get("ctrl_mode", self.ctrl_mode)
        ms = info.get("master_slave")
        if ms is not None:
            self.is_master = ms == "master"
        self.firmware = info.get("firmware", self.firmware)

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

    # ── 장치 조작은 전부 RPC ──

    def connect(self, bitrate: int = 1_000_000) -> tuple[bool, str]:
        ok, msg = _pair(_call("connect", self.iface, timeout=30), "robotd 연결 실패")
        if ok:
            # 연결 직후 마스터/슬레이브가 정해지므로 바로 흡수해야 한다 —
            # 안 그러면 등록 화면이 판별 전 값을 보여준다
            self.absorb(_call("refresh_mode", self.iface, default={}) or {})
            # `is_master` 는 팔에 물어본 사실이고, 역할은 그 **해석**이다.
            # 사용자가 `set_role()` 로 언제든 뒤집을 수 있다.
            if self.role == "unknown" and self.is_master is not None:
                self.role = "leader" if self.is_master else "follower"
        return ok, msg

    def disconnect(self) -> None:
        _call("disconnect", self.iface)
        self.connected = False

    def mark_absent(self) -> None:
        """robotd 가 이 팔을 모른다 — **장치 사실만** 지운다.

        역할·슬롯·등록 여부는 사람이 정한 것이라 남긴다. 하지만 `connected` 는
        장치 사실이고, 데몬이 죽은 동안 살아남으면 **화면은 "연결됨"인데
        `/dev/shm/piper.arm.<iface>.state` 가 없어 추론이 죽는다.** 실제로 겪었다 —
        에러는 "세그먼트가 없습니다"라고 하는데 로봇 페이지는 초록불이라
        무엇이 잘못됐는지 알 길이 없었다.

        데몬이 **다시 떠도** False 다. robotd 는 재시작하면 아무 팔도 안 쥐고 있으므로,
        살아 있다는 것만으로 연결됐다고 하면 같은 거짓말을 한 번 더 하게 된다.
        """
        self.connected = False

    def refresh_mode(self) -> None:
        info = _call("refresh_mode", self.iface, default={}) or {}
        # 빈 응답 = robotd 가 죽었거나 이 팔을 모른다. `absorb` 는 빈 dict 를
        # 그냥 무시하므로(실패한 RPC 가 값을 지우면 안 되니까) 여기서 갈라야 한다.
        if info:
            self.absorb(info)
        else:
            self.mark_absent()

    def refresh_ctrl_mode(self) -> int | None:
        self.refresh_mode()
        return None

    def set_master_slave(self, master: bool) -> tuple[bool, str]:
        r = _pair(_call("set_master_slave", self.iface, master, timeout=30), "설정 실패")
        if r[0]:
            self.role = "leader" if master else "follower"
            self.refresh_mode()
        return r

    def read_joints_raw(self) -> list[int] | None:
        return _call("read_joints_raw", self.iface)

    def read_joints_normalized(self) -> dict[str, float] | None:
        return _call("read_joints", self.iface)

    def read_error(self) -> dict | None:
        return _call("read_error", self.iface)

    def clear_error(self) -> bool:
        report = _call("clear_errors", [self.iface], default=[]) or []
        return bool(report and report[0].get("cleared"))

    def enable_torque(self) -> bool:
        return bool(_call("enable_torque", self.iface, default=False))

    def disable_torque(self) -> bool:
        return bool(_call("disable_torque", self.iface, default=False))

    def go_parking(self) -> bool:
        return bool(_call("go_parking", self.iface, default=False, timeout=30))


class RobotManager:
    def __init__(self) -> None:
        self.arms: dict[str, ArmInfo] = {}
        self.selected_type: str | None = None
        self.config_name: str | None = None

    # ── 스캔/연결 ──

    def scan(self) -> list[dict]:
        """robotd 가 본 팔 + 게이트웨이가 아는 설정.

        데몬이 죽어 있으면 빈 목록이 온다 — 그래도 **등록된 설정은 지우지 않는다.**
        데몬 재시작 사이에 사용자가 맞춰둔 역할·슬롯이 사라지면 안 된다.

        ⚠ 다만 **`connected` 는 설정이 아니라 장치 사실이다.** 데몬이 안 말한 팔은
        연결 안 된 것으로 내린다 — 안 그러면 robotd 가 죽은 뒤에도 화면이
        "연결됨"인 채로 남고, 추론만 "세그먼트가 없습니다"로 죽는다.
        """
        seen: set[str] = set()
        for info in _call("scan", default=[], timeout=30) or []:
            iface = info["iface"]
            seen.add(iface)
            arm = self.arms.get(iface)
            if arm is None:
                arm = self.arms[iface] = ArmInfo(iface=iface)
            arm.absorb(info)
        for iface, arm in self.arms.items():
            if iface not in seen:
                arm.mark_absent()
        return [a.to_dict() for a in self.arms.values()]

    def connect_arm(self, iface: str) -> tuple[bool, str]:
        arm = self.arms.get(iface)
        return arm.connect() if arm else (False, f"Unknown interface: {iface}")

    def disconnect_arm(self, iface: str) -> bool:
        arm = self.arms.get(iface)
        if not arm:
            return False
        arm.disconnect()
        return True

    # ── 역할/슬롯 (게이트웨이 소유) ──

    def set_role(self, iface: str, role: str) -> bool:
        arm = self.arms.get(iface)
        if not arm or role not in ("leader", "follower"):
            return False
        arm.role = role
        arm.slot = None          # 역할이 바뀌면 슬롯은 무효다
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
        return [a.to_dict() for a in self.arms.values() if a.ready]

    def update_arm_config(self, iface: str, cfg: dict) -> bool:
        arm = self.arms.get(iface)
        if not arm:
            return False
        arm.update_config(cfg)
        return True

    # ── 에러 클리어 ──

    def clear_arm_errors(self, ifaces: list[str] | None = None) -> list[dict]:
        """팔 에러를 조회 후 무조건 클리어. 조회 전 상태를 함께 돌려준다.

        ⚠ **"연결된 follower 전부"를 여기서 푼다.** 역할은 게이트웨이만 알므로
        데몬에 넘길 때는 iface 목록으로 바꿔서 준다.
        """
        if ifaces is None:
            ifaces = [a.iface for a in self.arms.values()
                      if a.connected and a.role == "follower"]
        report = _call("clear_errors", list(ifaces), default=[], timeout=30) or []
        for r in report:
            err = r.get("error") or {}
            if err.get("err_code"):
                logger.warning("Arm %s had error 0x%04X before clear: %s",
                               r["iface"], err["err_code"], err.get("flags"))
        return report

    # ── 움직임 감지 ──

    def start_motion_detect(self, slot: str) -> bool:
        """슬롯에 배정할 팔을 움직임으로 찾는다.

        ⚠ 후보를 **게이트웨이가 고른다** — 데몬은 슬롯을 모른다.
        """
        candidates = [a.iface for a in self.arms.values() if a.connected and a.slot is None]
        if not candidates:
            return False
        return bool(_call("start_motion_detect", slot, candidates, default=False))

    def get_motion_status(self, slot: str) -> dict:
        st = _call("motion_status", slot, default={"status": "idle"}) or {"status": "idle"}
        # 데몬은 "어느 iface 가 움직였는지"까지만 안다. 배정은 여기서 한다.
        if st.get("status") == "found" and st.get("found_iface"):
            self.assign_slot(st["found_iface"], slot)
        return st


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
        """프리셋을 현재 하드웨어에 적용한다.

        **무엇이 실제로 적용됐는지 돌려준다** (`applied`/`missing`/`failed`).
        예전에는 결과와 무관하게 프리셋 내용만 돌려줘서, 팔을 하나도 못 붙였는데도
        화면에는 "적용됨"이라고 떴다.
        """
        preset = presets.get(PRESET_DOMAIN, name)
        if preset is None:
            return None
        data = preset.values

        # ⚠ **먼저 스캔한다** (`restore_session` 과 같은 순서).
        # 안 하면 `self.arms` 가 비어 있어 프리셋의 팔을 전부 건너뛴다 —
        # 사용자가 "스캔 먼저"를 알고 있어야 하는 것은 UI 의 잘못이다.
        self.scan()

        # ⚠ **빈 값으로 덮어쓰지 않는다.** 프리셋을 저장할 때 타입이 비어 있었으면
        # null 이 저장되는데, 그걸 그대로 대입하면 프리셋이 상태를 복원하는 게 아니라
        # **지운다.** 실제로 프리셋을 부른 뒤 추론 시작이 막혔다.
        if data.get("robot_type"):
            self.selected_type = data["robot_type"]
        if data.get("config_name"):
            self.config_name = data["config_name"]
        applied: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        for arm_data in data.get("arms", []):
            iface = arm_data.get("can_name", "")
            if iface not in self.arms:
                logger.warning("프리셋의 팔 %s 를 스캔 결과에서 못 찾음 — 건너뜀", iface)
                missing.append(iface)
                continue
            arm = self.arms[iface]
            arm.slot = arm_data.get("slot")
            arm.role = arm_data.get("role", "unknown")
            arm.update_config(arm_data.get("config", {}))

            # ⚠ **연결까지 해야 한다** (`restore_session` 과 같은 이유).
            #
            # 예전에는 `ready` 만 세웠는데, 그러면 팔이 "사용 가능"에 올라가면서도
            # CAN 은 안 열린 상태로 남는다. 화면에서는 1·2단계 목록에서 빠지므로
            # **연결 버튼에 닿을 방법이 없어져** 등록을 끝낼 수가 없다.
            if not arm.connected:
                ok, msg = arm.connect()
                if not ok:
                    logger.warning("프리셋 로드 중 %s 연결 실패: %s", iface, msg)
                    # 연결 실패한 팔을 ready 로 올리지 않는다 — 쓸 수 없는데
                    # 쓸 수 있다고 말하는 셈이다. 1단계에 남아 다시 시도할 수 있다.
                    arm.ready = False
                    failed.append(iface)
                    continue
            # 등록 상태 복원. 없으면(옛 프리셋) 슬롯 유무로 추정한다.
            arm.ready = arm_data.get("ready", bool(arm_data.get("slot")))
            applied.append(iface)
        # 프론트 호환: 예전 응답이 name 을 포함했다
        return {"name": name, **data,
                "applied": applied, "missing": missing, "failed": failed}

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
