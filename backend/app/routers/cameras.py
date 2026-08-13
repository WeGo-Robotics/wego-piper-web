import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.camera_manager import camera_manager
from app.services.exclusivity import Activity, blocked_reason
from app.services.realsense_manager import realsense_hub

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

_executor = ThreadPoolExecutor(max_workers=4)


def _camera_owner() -> str | None:
    """추론/녹화 subprocess가 실행 중이면 그 프로세스가 물리 카메라를 소유한다.

    이때 백엔드가 같은 RealSense 디바이스를 동시에 열거나 UVC 컨트롤을 질의하면
    (특히 D405) 커널 uvcvideo가 D-state로 물려 librealsense가 SIGABRT(-6)로 죽고
    카메라까지 먹통이 된다. 디바이스를 직접 건드리는 엔드포인트는 이 동안 막는다.

    누가 소유하는지는 exclusivity 의 CAMERA_ACCESS 규칙 한 곳에 정의돼 있다."""
    return blocked_reason(Activity.CAMERA_ACCESS)


def _guard_device_access() -> None:
    owner = _camera_owner()
    if owner:
        raise HTTPException(409, f"{owner} 실행 중에는 카메라 디바이스에 접근할 수 없습니다")


@router.get("/scan")
async def scan_cameras():
    """카메라 스캔 + 병렬 probe (타임아웃 3초)."""
    if _camera_owner():
        # 프로세스가 카메라를 소유 중 → 디바이스 재열거/probe 없이 캐시만 반환
        return [c.to_dict() for c in camera_manager.cameras.values()]
    camera_manager.scan()
    loop = asyncio.get_event_loop()
    tasks = []
    for cam in camera_manager.cameras.values():
        if not cam.connected and not cam.ready:
            tasks.append(loop.run_in_executor(_executor, cam.probe))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return [c.to_dict() for c in camera_manager.cameras.values()]


class CameraIdRequest(BaseModel):
    id: str


@router.post("/probe")
async def probe_camera(body: CameraIdRequest):
    """1프레임 캡처 (연결 유지 안 함)."""
    _guard_device_access()
    loop = asyncio.get_event_loop()
    cam = camera_manager.cameras.get(body.id)
    if not cam:
        raise HTTPException(404, f"Unknown camera: {body.id}")
    ok, msg = await loop.run_in_executor(_executor, cam.probe)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok"}


@router.post("/connect")
async def connect_camera(body: CameraIdRequest):
    _guard_device_access()
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(
        _executor, camera_manager.connect_camera, body.id
    )
    if not ok:
        raise HTTPException(400, msg)
    cam = camera_manager.cameras.get(body.id)
    return cam.to_dict() if cam else {"status": "connected"}


@router.post("/disconnect")
async def disconnect_camera(body: CameraIdRequest):
    camera_manager.disconnect_camera(body.id)
    return {"status": "disconnected"}


class CameraConfigRequest(BaseModel):
    id: str
    config: dict


@router.post("/config")
async def update_config(body: CameraConfigRequest):
    _guard_device_access()
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        _executor, camera_manager.update_config, body.id, body.config
    )
    if not ok:
        raise HTTPException(400, "Unknown camera")
    cam = camera_manager.cameras.get(body.id)
    return cam.to_dict() if cam else {"status": "ok"}


class CameraRegisterRequest(BaseModel):
    id: str
    # 사람이 붙이는 별칭 — "탑뷰", "손목". 화면에서 어느 카메라인지 알아보는 용도다.
    # LeRobot 카메라 키(`observation.images.<키>`)와는 별개다 — 아래 /label 참고.
    label: str | None = None


@router.post("/register")
async def register_camera(body: CameraRegisterRequest):
    if not camera_manager.register_camera(body.id, body.label):
        raise HTTPException(400, "등록 실패: 연결되지 않은 카메라입니다")
    camera_manager.save_session()
    cam = camera_manager.cameras.get(body.id)
    return cam.to_dict() if cam else {"status": "registered"}


class CameraLabelRequest(BaseModel):
    id: str
    label: str


@router.post("/label")
async def set_camera_label(body: CameraLabelRequest):
    """별칭만 변경. 등록 후에도 고칠 수 있어야 한다.

    ⚠ **데이터셋 피처 이름은 안 바뀐다.** `observation.images.<키>` 의 키는
    녹화·추론 페이지에서 정하고 학습된 정책이 그 키에 묶여 있다. 여기서 바꾸는 것은
    화면에 보이는 이름뿐이라, 이미 구운 데이터셋이나 정책에 영향을 주지 않는다.
    """
    if not camera_manager.set_label(body.id, body.label):
        raise HTTPException(404, "카메라를 찾을 수 없습니다")
    camera_manager.save_session()
    cam = camera_manager.cameras.get(body.id)
    return cam.to_dict() if cam else {"status": "ok"}


@router.post("/unregister")
async def unregister_camera(body: CameraIdRequest):
    if not camera_manager.unregister_camera(body.id):
        raise HTTPException(400, "Unknown camera")
    camera_manager.save_session()
    return {"status": "unregistered"}


@router.get("/ready")
async def get_ready_cameras():
    return camera_manager.get_ready_cameras()


@router.get("/current")
async def get_current():
    return camera_manager.get_current()


# ── 프로파일 ──
#
# CRUD 는 공통 프리셋 API(`/api/presets/camera`)를 그대로 쓴다. 여기에는
# **장치를 읽거나 만져야 하는 것**만 둔다 — 그게 공통 스토어로는 안 되는 부분이다.

class ProfileCaptureRequest(BaseModel):
    name: str
    note: str = ""
    # 비우면 등록된 카메라 전부
    camera_ids: list[str] = []


def _cams(camera_ids: list[str]) -> list:
    if camera_ids:
        return [c for cid in camera_ids if (c := camera_manager.cameras.get(cid))]
    return [c for c in camera_manager.cameras.values() if c.ready]


@router.post("/profiles/capture")
async def capture_profile(body: ProfileCaptureRequest):
    """현재 장치 값을 읽어 프로파일로 저장한다.

    값의 출처가 디스크가 아니라 **장치**라, 공통 프리셋 저장 API 로는 안 된다.
    """
    from app.services import camera_profiles, presets

    _guard_device_access()
    cams = _cams(body.camera_ids)
    if not cams:
        raise HTTPException(400, "저장할 카메라가 없습니다 (등록된 카메라가 없음)")
    loop = asyncio.get_event_loop()
    values = await loop.run_in_executor(_executor, camera_profiles.capture, cams)
    try:
        preset = presets.save(camera_profiles.DOMAIN, body.name, values,
                              scope="device", note=body.note)
    except presets.PresetError as e:
        raise HTTPException(400, str(e))
    camera_profiles.set_active(body.name)
    return preset.to_dict()


class ProfileApplyRequest(BaseModel):
    name: str = ""
    camera_ids: list[str] = []


@router.post("/profiles/apply")
async def apply_profile(body: ProfileApplyRequest):
    """수동 적용. 연결 시 자동 적용과 **같은 데몬 함수**를 탄다."""
    from app.services import camera_profiles

    _guard_device_access()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, camera_profiles.apply, _cams(body.camera_ids), body.name
    )


class ProfileActiveRequest(BaseModel):
    name: str = ""


@router.post("/profiles/active")
async def set_active_profile(body: ProfileActiveRequest):
    """연결 시 자동 적용할 프로파일. 빈 이름이면 자동 적용을 끈다."""
    from app.services import camera_profiles

    camera_profiles.set_active(body.name)
    return {"active": camera_profiles.active_name()}


@router.get("/profiles/report")
async def profile_report():
    """마지막 적용 결과. 연결 안에서 적용하므로 응답에 실을 수 없어 따로 묻는다."""
    from app.services import camera_profiles

    return camera_profiles.report(list(camera_manager.cameras.values()))


class CameraControlRequest(BaseModel):
    id: str
    name: str
    value: float


@router.post("/control")
async def set_control(body: CameraControlRequest):
    """카메라 v4l2 컨트롤 값 설정."""
    _guard_device_access()
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        _executor, camera_manager.set_control, body.id, body.name, body.value
    )
    if not ok:
        raise HTTPException(400, f"컨트롤 설정 실패: {body.name}")
    return {"status": "ok", "name": body.name, "value": body.value}


@router.post("/reset-device")
async def reset_device(body: CameraIdRequest):
    """RealSense 하드웨어 리셋 (librealsense hardware_reset, 펌웨어 파워사이클).

    카메라가 멈췄을 때 재부팅 없이 복구한다. reset 후 디바이스가 USB에서 잠시
    사라졌다 재열거되므로 프론트는 응답 후 재스캔해야 한다."""
    _guard_device_access()
    cam = camera_manager.cameras.get(body.id)
    if cam is not None and cam.cam_type != "realsense":
        raise HTTPException(400, "RealSense 카메라만 하드웨어 리셋을 지원합니다")
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(
        _executor, realsense_hub.hardware_reset, body.id
    )
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok"}


@router.post("/controls/reset")
async def reset_controls(body: CameraIdRequest):
    """모든 v4l2 컨트롤을 초기값으로 복원."""
    _guard_device_access()
    loop = asyncio.get_event_loop()
    controls = await loop.run_in_executor(_executor, camera_manager.get_controls, body.id)
    if not controls:
        raise HTTPException(400, "컨트롤이 없습니다")
    for ctrl in controls:
        await loop.run_in_executor(
            _executor, camera_manager.set_control, body.id, ctrl["name"], ctrl["default"]
        )
    return await loop.run_in_executor(_executor, camera_manager.get_controls, body.id)


# path 라우트는 반드시 고정 경로 뒤에 배치
@router.get("/{cam_id:path}/preview")
async def preview(cam_id: str):
    data = camera_manager.get_preview(cam_id)
    if data is None:
        raise HTTPException(404, "Preview unavailable")
    return Response(content=data, media_type="image/jpeg")


@router.get("/{cam_id:path}/controls")
async def get_controls(cam_id: str):
    """카메라가 지원하는 v4l2 컨트롤 목록 + 현재값."""
    # RealSense UVC 컨트롤 질의는 D405를 D-state로 물리게 하는 대표 원인이므로
    # 추론/녹화가 디바이스를 소유 중일 때는 건드리지 않는다.
    _guard_device_access()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, camera_manager.get_controls, cam_id)


class DepthEncodingRequest(BaseModel):
    near_mm: int
    far_mm: int


@router.post("/{cam_id:path}/depth-encoding")
async def set_depth_encoding(cam_id: str, body: DepthEncodingRequest):
    """깊이 인코딩 범위 변경.

    ⚠ **녹화 중에는 막는다.** 도중에 바꾸면 한 데이터셋 안에서 픽셀값의 뜻이
    달라지는데, 사이드카에는 정지 시점의 값 하나만 남아 거짓이 된다.
    """
    from app.services.exclusivity import Activity, require_idle
    from app.services.realsense_manager import realsense_hub

    require_idle(Activity.CAMERA_ACCESS)
    ok, msg = realsense_hub.set_depth_encoding(cam_id, body.near_mm, body.far_mm)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok", "encoding": realsense_hub.info(cam_id).get("depth_encoding")}
