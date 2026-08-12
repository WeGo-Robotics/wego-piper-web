import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.robot_manager import robot_manager, CONFIGS, _save_custom_parking, _load_custom_parking

router = APIRouter(prefix="/api/robots", tags=["robots"])
logger = logging.getLogger(__name__)

# ── LeRobot 로봇 타입 목록 (기존 로직) ──

_LEROBOT_SEARCH_PATHS = [
    Path.home() / "lerobot_last" / "src" / "lerobot" / "robots",
    Path.home() / "lerobot" / "src" / "lerobot" / "robots",
]

FALLBACK_TYPES = [
    "so_follower", "bi_so_follower", "koch_follower", "lekiwi",
    "openarm_follower", "bi_openarm_follower", "omx_follower",
    "hope_jr", "reachy2", "unitree_g1", "earthrover_mini_plus",
]


def _discover_types() -> list[dict]:
    """
    로봇 타입 발견 순서:
    1) LeRobot 내장 로봇 (패키지 디렉토리 스캔)
    2) 외부 플러그인 (lerobot_robot_* 패키지 — importlib.metadata)
    3) fallback 하드코딩 목록
    """
    built_in = _scan_builtin_robots()
    plugins = _scan_plugin_robots()
    combined = built_in + [p for p in plugins if p["type"] not in {b["type"] for b in built_in}]
    return combined if combined else [{"type": t} for t in FALLBACK_TYPES]


def _scan_builtin_robots() -> list[dict]:
    """LeRobot 내장 로봇 (패키지 디렉토리 스캔)."""
    try:
        import lerobot.robots as pkg
        d = Path(pkg.__file__).parent
        if d.exists():
            found = [{"type": e.name, "source": "builtin"} for e in sorted(d.iterdir())
                     if e.is_dir() and not e.name.startswith("_") and any(e.glob("config*.py"))]
            if found:
                return found
    except Exception:
        pass
    for p in _LEROBOT_SEARCH_PATHS:
        if p.exists():
            found = [{"type": e.name, "source": "builtin"} for e in sorted(p.iterdir())
                     if e.is_dir() and not e.name.startswith("_") and any(e.glob("config*.py"))]
            if found:
                return found
    return []


def _scan_plugin_robots() -> list[dict]:
    """
    외부 플러그인 로봇 발견.
    lerobot_robot_* / lerobot_teleoperator_* 패키지를 importlib.metadata로 스캔.
    설치된 패키지뿐 아니라 알려진 로컬 경로도 탐색.
    """
    import importlib.metadata

    plugins: list[dict] = []
    seen: set[str] = set()

    # 1) importlib.metadata로 설치된 플러그인 스캔
    prefixes = ("lerobot_robot_", "lerobot_teleoperator_")
    for dist in importlib.metadata.distributions():
        dist_name = dist.metadata.get("Name", "")
        if not dist_name.startswith(prefixes):
            continue
        # 패키지 이름에서 타입 추출: lerobot_robot_piper → piper
        for prefix in prefixes:
            if dist_name.startswith(prefix):
                role = "robot" if "robot" in prefix else "teleoperator"
                robot_name = dist_name[len(prefix):]
                _add_plugin_types(dist_name, robot_name, role, plugins, seen)
                break

    # 2) 알려진 로컬 소스 경로 스캔 (미설치 플러그인)
    plugin_search_paths = [
        Path.home() / "lerobot_robot_piper",
    ]
    for plugin_path in plugin_search_paths:
        if not plugin_path.exists():
            continue
        pkg_name = plugin_path.name  # lerobot_robot_piper
        for prefix in prefixes:
            if pkg_name.startswith(prefix):
                robot_name = pkg_name[len(prefix):]
                _add_plugin_from_source(plugin_path, pkg_name, robot_name, plugins, seen)
                break

    return plugins


def _add_plugin_types(
    dist_name: str, robot_name: str, role: str,
    plugins: list[dict], seen: set[str],
) -> None:
    """설치된 플러그인에서 register_subclass 데코레이터로 등록된 타입을 추출."""
    try:
        import importlib
        mod = importlib.import_module(dist_name)
        # config 파일들을 import해서 등록된 이름 수집
        pkg_dir = Path(mod.__file__).parent if mod.__file__ else None
        if pkg_dir:
            for f in pkg_dir.glob("config*.py"):
                try:
                    importlib.import_module(f"{dist_name}.{f.stem}")
                except Exception:
                    pass
        # register_subclass로 등록된 이름을 찾기
        _extract_registered_types(dist_name, robot_name, role, plugins, seen)
    except Exception:
        # import 실패 시 이름 기반 추측
        for suffix in ("_follower", "_leader"):
            t = f"{robot_name}{suffix}"
            if t not in seen:
                plugins.append({"type": t, "source": f"plugin:{dist_name}"})
                seen.add(t)


def _add_plugin_from_source(
    plugin_path: Path, pkg_name: str, robot_name: str,
    plugins: list[dict], seen: set[str],
) -> None:
    """미설치 로컬 소스에서 config 파일을 파싱하여 타입 추출."""
    inner = plugin_path / pkg_name
    if not inner.exists():
        inner = plugin_path / "src" / pkg_name
    if not inner.exists():
        return

    import re
    for config_file in inner.glob("config*.py"):
        try:
            text = config_file.read_text()
            # @RobotConfig.register_subclass("piper_follower") 패턴 매칭
            for match in re.finditer(r'register_subclass\(["\']([^"\']+)["\']\)', text):
                type_name = match.group(1)
                if type_name not in seen:
                    plugins.append({"type": type_name, "source": f"plugin:{pkg_name}(local)"})
                    seen.add(type_name)
        except Exception:
            pass

    # config 파일에서 못 찾으면 이름 기반 추측
    for suffix in ("_follower", "_leader"):
        t = f"{robot_name}{suffix}"
        if t not in seen:
            plugins.append({"type": t, "source": f"plugin:{pkg_name}(local)"})
            seen.add(t)


def _extract_registered_types(
    dist_name: str, robot_name: str, role: str,
    plugins: list[dict], seen: set[str],
) -> None:
    """ChoiceRegistry에서 등록된 서브클래스 이름을 추출."""
    try:
        from lerobot.robots.config import RobotConfig
        for name in RobotConfig.get_known_choices():
            if robot_name in name and name not in seen:
                plugins.append({"type": name, "source": f"plugin:{dist_name}"})
                seen.add(name)
    except Exception:
        pass


# ── 로봇 타입 ──

@router.get("/types")
async def list_types():
    return _discover_types()


@router.get("/configurations")
async def list_configurations():
    """사용 가능한 팔 구성 목록."""
    return [{"name": k, "slots": v} for k, v in CONFIGS.items()]


# ── 로봇 타입 선택 ──

class SelectTypeRequest(BaseModel):
    robot_type: str


@router.post("/select")
async def select_type(body: SelectTypeRequest):
    """로봇 타입 선택. **세션에 남긴다.**

    예전에는 메모리에만 두어 서버가 리로드될 때마다 날아갔고, 그때마다
    추론 시작이 "로봇이 선택되지 않았습니다"로 막혔다 — 팔은 등록돼 있는데
    타입만 비어 있어서 원인을 찾기 어려웠다.
    """
    robot_manager.selected_type = body.robot_type
    robot_manager.save_session()
    return {"status": "ok", "selected_type": body.robot_type}


@router.get("/current")
async def get_current():
    # get_current는 연결된 팔마다 RX 감지로 잠깐 블로킹하므로 executor에서 실행
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, robot_manager.get_current)


# ── CAN 스캔 ──

@router.get("/can")
async def scan_can():
    return robot_manager.scan()


# ── USB 진단 / 복구 ──

class UsbRecoverRequest(BaseModel):
    pci_addrs: list[str] | None = None


@router.get("/usb/info")
async def usb_info():
    """lsusb 출력(목록 + 트리) 및 xHCI 컨트롤러 목록."""
    from app.services.robot_manager import get_usb_info
    return get_usb_info()


@router.post("/usb/recover")
async def usb_recover(body: UsbRecoverRequest):
    """xHCI 컨트롤러를 재바인딩하여 'HC died'로 사라진 USB 트리를 복구."""
    import asyncio
    from app.services.robot_manager import recover_usb_controllers, get_usb_info
    loop = asyncio.get_event_loop()
    ok, msg, done = await loop.run_in_executor(None, recover_usb_controllers, body.pci_addrs)
    await asyncio.sleep(2)  # 재열거 대기
    return {"ok": ok, "message": msg, "rebound": done, "usb": get_usb_info()}


# ── CAN 인터페이스 관리 ──

class CanUpRequest(BaseModel):
    iface: str
    bitrate: int = 1_000_000


@router.get("/can/check/{iface}")
async def can_check_active(iface: str):
    """CAN 포트에 로봇이 연결되어 데이터를 보내고 있는지 확인."""
    import asyncio
    from app.services.robot_manager import check_can_active
    loop = asyncio.get_event_loop()
    active = await loop.run_in_executor(None, check_can_active, iface)
    return {"iface": iface, "active": active}


@router.get("/can/sniff/{iface}")
async def can_sniff(iface: str, duration: float = 1.2):
    """CAN 버스를 잠깐 청취해 ID 그룹 분포와 마스터/슬레이브 정황을 반환."""
    import asyncio
    from app.services.robot_manager import sniff_can_ids
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, sniff_can_ids, iface, duration)


@router.post("/can/up")
async def can_up(body: CanUpRequest):
    """CAN 인터페이스 UP."""
    from app.services.robot_manager import init_can_interface
    ok, msg = init_can_interface(body.iface, body.bitrate)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok", "iface": body.iface}


class CanRenameRequest(BaseModel):
    old_name: str
    new_name: str


@router.post("/can/rename")
async def can_rename(body: CanRenameRequest):
    """CAN 인터페이스 이름 변경."""
    from app.services.robot_manager import rename_can_interface
    ok, msg = rename_can_interface(body.old_name, body.new_name)
    if not ok:
        raise HTTPException(400, msg)
    # robot_manager의 arms 딕셔너리도 갱신
    if body.old_name in robot_manager.arms:
        arm = robot_manager.arms.pop(body.old_name)
        arm.iface = body.new_name
        robot_manager.arms[body.new_name] = arm
    return {"status": "ok", "old_name": body.old_name, "new_name": body.new_name}


# ── 팔 연결/해제 ──

class ConnectRequest(BaseModel):
    iface: str


@router.post("/connect")
async def connect_arm(body: ConnectRequest):
    ok, msg = robot_manager.connect_arm(body.iface)
    if not ok:
        raise HTTPException(400, msg)
    arm = robot_manager.arms.get(body.iface)
    return arm.to_dict() if arm else {"status": "connected"}


@router.post("/disconnect")
async def disconnect_arm(body: ConnectRequest):
    robot_manager.disconnect_arm(body.iface)
    return {"status": "disconnected"}


# ── 슬롯 지정 ──

class AssignRequest(BaseModel):
    iface: str
    slot: str


@router.post("/assign")
async def assign_slot(body: AssignRequest):
    if not robot_manager.assign_slot(body.iface, body.slot):
        raise HTTPException(400, "Assignment failed")
    return {"status": "assigned", "iface": body.iface, "slot": body.slot}


# ── 움직임 감지 ──

class MotionDetectRequest(BaseModel):
    slot: str


@router.post("/find-by-motion")
async def start_motion_detect(body: MotionDetectRequest):
    if not robot_manager.start_motion_detect(body.slot):
        raise HTTPException(400, "No unassigned connected arms")
    return {"status": "started", "slot": body.slot}


@router.get("/find-by-motion/status")
async def motion_detect_status(slot: str):
    return robot_manager.get_motion_status(slot)


# ── 설정 저장/로드 ──

class SaveConfigRequest(BaseModel):
    config_name: str


@router.post("/save")
async def save_config(body: SaveConfigRequest):
    ok, msg = robot_manager.save_config(body.config_name)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "saved"}


@router.get("/config")
async def load_config():
    config = robot_manager.load_config()
    if not config:
        return {"status": "no_config"}
    return config


# ── 등록 (사용 가능 리스트) ──

class RegisterRequest(BaseModel):
    iface: str


@router.post("/register")
async def register_arm(body: RegisterRequest):
    if not robot_manager.register_arm(body.iface):
        raise HTTPException(400, "등록 실패: 연결되지 않았거나 역할이 미지정입니다")
    robot_manager.save_session()
    arm = robot_manager.arms.get(body.iface)
    return arm.to_dict() if arm else {"status": "registered"}


@router.post("/unregister")
async def unregister_arm(body: RegisterRequest):
    if not robot_manager.unregister_arm(body.iface):
        raise HTTPException(400, "Unknown arm")
    robot_manager.save_session()
    return {"status": "unregistered"}


@router.get("/ready")
async def get_ready_arms():
    return robot_manager.get_ready_arms()


# ── 역할 수동 변경 ──

class RoleRequest(BaseModel):
    iface: str
    role: str  # "leader" | "follower"


@router.post("/role")
async def set_role(body: RoleRequest):
    if not robot_manager.set_role(body.iface, body.role):
        raise HTTPException(400, "Role change failed")
    arm = robot_manager.arms.get(body.iface)
    return arm.to_dict() if arm else {"status": "ok"}


# ── 마스터/슬레이브 모드 설정 ──

class MasterSlaveRequest(BaseModel):
    iface: str
    master: bool


@router.post("/master-slave")
async def set_master_slave(body: MasterSlaveRequest):
    """팔을 마스터(示教输入) 또는 슬레이브(运动输出)로 설정."""
    import asyncio
    arm = robot_manager.arms.get(body.iface)
    if not arm:
        raise HTTPException(404, "Unknown arm")
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(None, arm.set_master_slave, body.master)
    if not ok:
        raise HTTPException(400, msg)
    return arm.to_dict()


# ── 팔 설정값 업데이트 ──

class ArmConfigRequest(BaseModel):
    iface: str
    config: dict


@router.post("/arm-config")
async def update_arm_config(body: ArmConfigRequest):
    if not robot_manager.update_arm_config(body.iface, body.config):
        raise HTTPException(400, "Unknown arm")
    arm = robot_manager.arms.get(body.iface)
    return arm.to_dict() if arm else {"status": "ok"}


# ── 프리셋 ──

@router.get("/presets")
async def list_presets():
    return robot_manager.list_presets()


class PresetSaveRequest(BaseModel):
    name: str


@router.post("/presets/save")
async def save_preset(body: PresetSaveRequest):
    try:
        robot_manager.save_preset(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "saved", "name": body.name}


class PresetLoadRequest(BaseModel):
    name: str


@router.post("/presets/load")
async def load_preset(body: PresetLoadRequest):
    # 프리셋 로드는 팔마다 CAN 을 여느라 초 단위로 블로킹한다 —
    # 이벤트 루프에서 돌리면 그동안 heartbeat 이 끊겨 E-stop 이 돈다.
    import asyncio

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, robot_manager.load_preset, body.name)
    if not data:
        raise HTTPException(404, "Preset not found")
    return data


@router.delete("/presets/{name}")
async def delete_preset(name: str):
    if not robot_manager.delete_preset(name):
        raise HTTPException(404, "Preset not found")
    return {"status": "deleted"}


# ── 파킹 위치 보정 ──

@router.post("/parking/go")
async def parking_go(body: ConnectRequest):
    """파킹 위치로 이동."""
    arm = robot_manager.arms.get(body.iface)
    if not arm or not arm.connected:
        raise HTTPException(404, "Arm not connected")
    if not arm.go_parking():
        raise HTTPException(500, "Parking failed")
    return {"status": "ok"}


@router.post("/parking/torque")
async def parking_torque(body: ConnectRequest, enable: bool = True):
    """토크 ON/OFF."""
    arm = robot_manager.arms.get(body.iface)
    if not arm or not arm.connected:
        raise HTTPException(404, "Arm not connected")
    if enable:
        arm.enable_torque()
    else:
        arm.disable_torque()
    return {"status": "torque_on" if enable else "torque_off"}


@router.get("/parking/joints/{iface}")
async def parking_read_joints(iface: str):
    """현재 관절 위치 읽기 (정규화값)."""
    arm = robot_manager.arms.get(iface)
    if not arm or not arm.connected:
        raise HTTPException(404, "Arm not connected")
    joints = arm.read_joints_normalized()
    if joints is None:
        raise HTTPException(500, "Failed to read joints")
    return joints


class ParkingSaveBody(BaseModel):
    iface: str
    positions: dict[str, float]


@router.post("/parking/save")
async def parking_save(body: ParkingSaveBody):
    """현재 관절 위치를 커스텀 파킹 위치로 저장."""
    arm = robot_manager.arms.get(body.iface)
    if not arm or not arm.connected:
        raise HTTPException(404, "Arm not connected")
    _save_custom_parking(body.iface, body.positions)
    return {"status": "saved", "positions": body.positions}


@router.get("/parking/saved/{iface}")
async def parking_get_saved(iface: str):
    """저장된 커스텀 파킹 위치 조회."""
    saved = _load_custom_parking(iface)
    return {"has_custom": saved is not None, "positions": saved}


# 하위 호환: 기존 GET /api/robots → types로 리다이렉트
@router.get("")
async def list_robots_compat():
    return _discover_types()
