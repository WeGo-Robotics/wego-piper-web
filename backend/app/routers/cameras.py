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


@router.post("/register")
async def register_camera(body: CameraIdRequest):
    if not camera_manager.register_camera(body.id):
        raise HTTPException(400, "등록 실패: 연결되지 않은 카메라입니다")
    camera_manager.save_session()
    cam = camera_manager.cameras.get(body.id)
    return cam.to_dict() if cam else {"status": "registered"}


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
