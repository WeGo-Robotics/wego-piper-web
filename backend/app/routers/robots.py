import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import robot_manager as robot_manager_mod
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


@router.get("/joints/raw/{iface}")
async def read_joints_raw(iface: str):
    """관절 raw 값 (밀리도). 영점 창이 이걸 본다.

    ⚠ **정규화가 아니라 raw 다.** 정규화 값은 `JOINT_CALIBRATION` 을 거친 것이라
      하드웨어 영점을 옮기면 같이 흔들린다 — 무엇을 굽는지 보려면 팔이 직접
      말하는 숫자여야 한다.
    """
    arm = robot_manager.arms.get(iface)
    if not arm or not arm.connected:
        raise HTTPException(404, f"{iface} 가 연결되어 있지 않습니다")
    raw = robot_manager_mod._call("read_raw_all", iface)
    if not raw:
        raise HTTPException(503, "관절값을 읽지 못했습니다")
    return raw


# ── 하드웨어 영점 (모터 플래시) ──
#
# ⚠ **소프트웨어 캘리브레이션과 다른 물건이다.**
#   `/parking/save` 는 우리 파일에 자세를 적는다. 여기는 **모터 드라이버 플래시**에
#   지금 위치를 0 으로 굽는다 — 전원을 꺼도 남고 되돌리는 명령이 없다.


class ZeroRequest(BaseModel):
    iface: str
    #: joint1~6 또는 gripper
    joint: str


@router.post("/zero")
async def set_hardware_zero(body: ZeroRequest):
    """지금 위치를 그 관절의 하드웨어 영점으로 굽는다. **되돌릴 수 없다.**

    ⚠ 팔이 움직이는 중이면 **엉뚱한 자세가 영점이 된다.** 추론·녹화·조그가
      돌고 있으면 거절한다 — 되돌릴 수 없는 조작이라 나중에 알아차려도 늦다.
    """
    from app.services.exclusivity import LABELS, Activity, running

    # 팔을 움직이는 것들. `require_idle` 을 안 쓰는 이유는 영점이 활동이 아니라
    # 한 번의 조작이라서다 — 표에 항목을 늘리면 배타 규칙까지 따라 붙는다.
    movers = [a for a in (Activity.INFERENCE, Activity.RECORDING, Activity.TELEOP)
              if a in running()]
    if movers:
        names = " · ".join(LABELS[a] for a in movers)
        raise HTTPException(
            409, f"{names} 실행 중입니다 — 팔이 움직이는 동안 영점을 굽으면 "
                 f"엉뚱한 자세가 영점이 됩니다. 먼저 멈추세요.")
    arm = robot_manager.arms.get(body.iface)
    if not arm or not arm.connected:
        raise HTTPException(404, f"{body.iface} 가 연결되어 있지 않습니다")
    out = robot_manager_mod.set_hardware_zero(body.iface, body.joint)
    if out is None:
        raise HTTPException(503, "robotd 가 응답하지 않습니다 — 데몬이 떠 있나요?")
    if not out.get("ok"):
        raise HTTPException(409, out.get("error", "영점 설정에 실패했습니다"))
    return out


# ── 안전(바닥 필터) 설정 ──

class SafetyRequest(BaseModel):
    enabled: bool | None = None
    min_z_cm: float | None = None


@router.get("/safety")
async def get_safety():
    """바닥 필터 설정 + **무엇에 걸리는지.**

    ⚠ 필터가 걸리는 범위는 `robot_transport` 가 정한다. `shm` 이면 LeRobot
    녹화·추론이 robotd 를 지나므로 걸리고, `direct` 면 subprocess 가 CAN 을
    **직접 열어** 안 걸린다. 화면이 그 차이를 모르면 안 걸리는 상태에서도
    "전부에 걸립니다" 라고 말한다 — 안전 화면에서 그건 거짓말이다.
    """
    from app.core.config import settings
    return {
        "floor": robot_manager_mod.get_safety(),
        "transport": settings.robot_transport,
    }


@router.post("/safety")
async def set_safety(body: SafetyRequest):
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(400, "바꿀 값이 없습니다")
    out = robot_manager_mod.set_safety(patch)
    if out is None:
        raise HTTPException(503, "robotd 가 응답하지 않습니다 — 데몬이 떠 있나요?")
    return {"floor": out}


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
    # 팔의 키(iface)가 바뀌었다 — 세션은 그 이름으로 팔을 찾는다
    robot_manager.save_session()
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
    robot_manager.save_session()   # 슬롯은 세션이 저장하는 값이다
    return {"status": "assigned", "iface": body.iface, "slot": body.slot}


# ── 움직임 감지 ──

class MotionDetectRequest(BaseModel):
    slot: str


class IdentifyRequest(BaseModel):
    """`iface` 는 **버튼을 누른 그 팔**이다. `slot` 은 상태를 담아둘 키."""

    slot: str
    iface: str


@router.post("/identify")
async def start_identify(body: IdentifyRequest):
    """마스터/슬레이브를 **팔에 직접 물어서** 가린다.

    **부른 팔 하나에만** 작은 이동 명령을 넣고 반응을 본다 — 마스터는 외부 명령을 무시하고
    피드백도 안 보내므로 움직이지도, 관절값이 바뀌지도 않는다.

    ⚠ **팔이 실제로 움직인다.** 추론·녹화 중에는 막는다 — 돌고 있는 에피소드
    한가운데서 손목이 돌아가면 그 데이터는 못 쓴다.

    ⚠ 시작까지 10초를 기다린다. 부팅 중인 팔에 CAN 이 도착하면 부팅이 깨지는데,
    방금 전원을 넣었는지 여기서는 알 수 없다.
    """
    from app.services.exclusivity import Activity, require_idle

    require_idle(Activity.INFERENCE)
    require_idle(Activity.RECORDING)
    if not robot_manager.start_identify(body.slot, body.iface):
        raise HTTPException(400, f"{body.iface} 가 연결돼 있지 않습니다")
    return {"status": "started", "slot": body.slot}


@router.post("/find-by-motion")
async def start_motion_detect(body: MotionDetectRequest):
    if not robot_manager.start_motion_detect(body.slot):
        raise HTTPException(400, "No unassigned connected arms")
    return {"status": "started", "slot": body.slot}


@router.get("/find-by-motion/status")
async def motion_detect_status(slot: str):
    return robot_manager.get_motion_status(slot)


# ── 웹 조그 (feature/manual-control.md §2) ──


def _require_commandable(iface: str):
    """이 팔에 명령을 보내도 되는가. 아니면 **이유를 말하고 막는다.**

    ⚠ 마스터로 설정된 팔은 **외부 제어 명령을 무시한다** — 추정이 아니라 판별
    기능이 이용하는 측정된 성질이다. 보내면 에러도 안 나고 명령이 그냥 사라져서,
    사용자는 팔이 고장 났다고 생각하게 된다. 그래서 조용히 보내느니 막는다.
    """
    arm = robot_manager.arms.get(iface)
    if arm is None or not arm.connected:
        raise HTTPException(400, f"{iface} 가 연결돼 있지 않습니다")
    if arm.role == "leader":
        raise HTTPException(
            409, f"{iface} 는 마스터(리더)라 외부 명령을 무시합니다 — "
                 "슬레이브로 바꾸거나 팔로워 팔을 고르세요")
    if arm.role != "follower":
        raise HTTPException(
            409, f"{iface} 의 역할을 모릅니다 — 먼저 [찾기] 로 판별하세요")
    return arm


class JogStartRequest(BaseModel):
    iface: str


class JogGoalRequest(BaseModel):
    iface: str
    #: 정규화 목표. **부분 목표를 허용한다** — 세션이 직전 값과 병합한다.
    values: dict[str, float]


@router.post("/jog/start")
async def jog_start(body: JogStartRequest):
    """조그를 연다. 추론·녹화와 배타이고, 팔은 팔로워여야 한다."""
    from app.services.exclusivity import Activity, require_idle
    from app.services.jog import JogError, jog_session

    require_idle(Activity.TELEOP)
    arm = _require_commandable(body.iface)
    # 시작 목표는 **지금 자세**다 — 0 으로 채우면 첫 명령에 팔이 튄다
    current = arm.read_joints_normalized()
    if not current:
        raise HTTPException(400, f"{body.iface} 의 관절값을 읽지 못했습니다")
    try:
        jog_session.start(body.iface, current)
    except JogError as e:
        raise HTTPException(409, str(e))
    return {"status": "started", **jog_session.status()}


@router.post("/jog/goal")
async def jog_goal(body: JogGoalRequest):
    from app.services.jog import JogError, jog_session

    if jog_session.iface != body.iface:
        raise HTTPException(409, f"{body.iface} 는 조종 중이 아닙니다")
    try:
        return {"status": "ok", "goal": jog_session.set_goal(body.values)}
    except JogError as e:
        raise HTTPException(409, str(e))


@router.post("/jog/stop")
async def jog_stop():
    from app.services.jog import jog_session

    jog_session.stop()
    return {"status": "stopped"}


@router.get("/jog/status")
async def jog_status():
    from app.services.jog import jog_session

    return jog_session.status()


@router.get("/teleop/status")
async def teleop_status():
    """**누가 팔을 잡고 있나.** 조그·릴레이·말단 조그가 세션 하나를 공유한다.

    ⚠ 화면이 이걸 안 보면 버튼이 거짓말한다. 릴레이를 켜 둔 채 새로고침하면
    로컬 state 가 비어서 [조그 시작] 이 눌리는 것처럼 보이고, 누르면 409 만
    돌아온다 — "조그가 안 된다"로 보고된 게 이 경우다.
    """
    from app.services.teleop import teleop_session

    return teleop_session.to_dict()


# ── 말단 조그 (feature/teleoperation.md §3-C) ──


class EndPoseJogRequest(BaseModel):
    iface: str
    #: x|y|z (mm) 또는 rx|ry|rz (도)
    axis: str
    #: **상대** 이동량. 절대 좌표를 안 받는 이유는 오타 하나가 큰 이동이 되기 때문이다.
    delta: float


@router.post("/end-pose/jog")
async def end_pose_jog(body: EndPoseJogRequest):
    """말단을 한 걸음 움직인다 — **관절은 팔의 온보드 IK 가 정한다.**

    ⚠ 이 경로는 관절 안전 필터를 **타지 않는다.** 막는 것은 작업 공간 상자와
    걸음 상한뿐이라(`piper_robot.endpose`), 다른 모드보다 상자를 좁게 잡는다.
    """
    from app.services.exclusivity import Activity, require_idle

    import asyncio

    require_idle(Activity.TELEOP)
    _require_commandable(body.iface)
    # ⚠ 블로킹이다 — 명령을 보내고 도달을 2초 기다린다. 이벤트 루프에서 직접
    #   돌리면 그동안 heartbeat 를 포함한 모든 요청이 멈춘다.
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(
        None, lambda: robot_manager.jog_end_pose(body.iface, body.axis, body.delta))
    if not report.get("ok"):
        raise HTTPException(409, report.get("error", "말단 조그에 실패했습니다"))
    return report


@router.get("/end-pose/{iface}")
async def end_pose(iface: str):
    """지금 말단 자세 + 작업 공간 상자. 화면이 한계를 알아야 한다."""
    from piper_robot.endpose import WorkspaceBox

    return {"pose": robot_manager.read_end_pose(iface), "box": WorkspaceBox().to_dict()}


# ── 리더 릴레이 (feature/teleoperation.md §3-A) ──


class RelayStartRequest(BaseModel):
    # ⚠ **모르는 필드를 거절한다.** 기본값(무시)이라 옛 게이트웨이가 `mode` 를
    #   조용히 버리고 관절 복제로 돌았다 — 화면은 6D 라고 표시하는데. 실제로
    #   그렇게 보고됐다("6D 인데 왜 관절이 따라 돌지"). 400 이 나면 바로 안다.
    model_config = {"extra": "forbid"}

    leader: str
    follower: str
    #: `joint` = 관절 복제 (안전 필터를 탄다)
    #: `pose`  = 리더 말단 6D 를 FK 로 읽어 팔로워를 MoveP 로 (필터를 **안** 탄다)
    mode: str = "joint"


@router.post("/relay/start")
async def relay_start(body: RelayStartRequest):
    """리더 팔이 끄는 대로 팔로워를 따라 움직이게 한다.

    ⚠ 리더에게는 **아무것도 안 보낸다** — 읽기만 한다. 마스터는 외부 명령을
    무시하므로 보낼 이유도 없다.

    모드는 둘이다:

      `joint`  리더 관절을 그대로 복제한다. robotd 의 안전 필터를 탄다.
      `pose`   리더 말단 6D 를 **FK 로** 구해(마스터 팔은 자기 말단 자세를 안
               알려준다) 팔로워에 MoveP 로 준다. 관절을 팔의 온보드 IK 가
               정하므로 **관절 안전 필터가 안 걸린다** — 막는 것은 전부
               `relay._send_pose` 에 있다.
    """
    from app.services.exclusivity import Activity, require_idle
    from app.services.relay import RelayError, relay_session

    require_idle(Activity.TELEOP)
    _require_commandable(body.follower)
    leader = robot_manager.arms.get(body.leader)
    if leader is None or not leader.connected:
        raise HTTPException(400, f"{body.leader} 가 연결돼 있지 않습니다")
    if leader.role != "leader":
        raise HTTPException(
            409, f"{body.leader} 는 리더가 아닙니다 — [찾기] 로 판별하거나 "
                 "마스터로 설정하세요")
    try:
        relay_session.start(body.leader, body.follower, body.mode)
    except RelayError as e:
        raise HTTPException(409, str(e))
    return {"status": "started", **relay_session.status()}


@router.post("/relay/stop")
async def relay_stop():
    from app.services.relay import relay_session

    relay_session.stop()
    return {"status": "stopped"}


@router.get("/relay/status")
async def relay_status():
    from app.services.relay import relay_session

    return relay_session.status()


# ── 설정 저장/로드 ──

class SaveConfigRequest(BaseModel):
    config_name: str


@router.post("/save")
async def save_config(body: SaveConfigRequest):
    ok, msg = robot_manager.save_config(body.config_name)
    if ok:
        robot_manager.save_session()   # `config_name` 이 세션에 실린다
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
    robot_manager.save_session()
    arm = robot_manager.arms.get(body.iface)
    return arm.to_dict() if arm else {"status": "ok"}


# ── 좌/우 지정 (양팔) ──

class SideRequest(BaseModel):
    iface: str
    side: str | None  # "left" | "right" | null(해제)


@router.post("/side")
async def set_side(body: SideRequest):
    """좌/우는 사람의 해석이라 등록에 박제한다 — 세션 사이에 좌/우가 뒤바뀌면
    양팔 데이터셋이 거울상으로 오염된다 (feature/bimanual.md §3)."""
    if not robot_manager.set_side(body.iface, body.side):
        raise HTTPException(400, "Side change failed")
    robot_manager.save_session()
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
    robot_manager.save_session()   # 이 호출은 역할도 같이 바꾼다
    return arm.to_dict()


# ── 팔 설정값 업데이트 ──

class ArmConfigRequest(BaseModel):
    iface: str
    config: dict


@router.post("/arm-config")
async def update_arm_config(body: ArmConfigRequest):
    if not robot_manager.update_arm_config(body.iface, body.config):
        raise HTTPException(400, "Unknown arm")
    robot_manager.save_session()   # `config` 가 세션에 실린다
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
    # ⚠ **프리셋 로드가 곧 세션이 기억해야 할 상태다.** 이게 없어서, 팔을 등록
    #   해제했다가 프리셋으로 되살린 뒤 게이트웨이를 재시작하면 팔이 통째로
    #   사라졌다 — 화면에는 멀쩡히 있었으므로 재시작 전까지 아무도 몰랐다.
    robot_manager.save_session()
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
