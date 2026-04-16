import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.camera_manager import camera_manager

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

_executor = ThreadPoolExecutor(max_workers=4)


@router.get("/scan")
async def scan_cameras():
    """카메라 스캔 + 병렬 probe (타임아웃 3초)."""
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
    ok, msg = camera_manager.connect_camera(body.id)
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
    if not camera_manager.update_config(body.id, body.config):
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
    ok = camera_manager.set_control(body.id, body.name, body.value)
    if not ok:
        raise HTTPException(400, f"컨트롤 설정 실패: {body.name}")
    return {"status": "ok", "name": body.name, "value": body.value}


@router.post("/controls/reset")
async def reset_controls(body: CameraIdRequest):
    """모든 v4l2 컨트롤을 초기값으로 복원."""
    controls = camera_manager.get_controls(body.id)
    if not controls:
        raise HTTPException(400, "컨트롤이 없습니다")
    for ctrl in controls:
        camera_manager.set_control(body.id, ctrl["name"], ctrl["default"])
    return camera_manager.get_controls(body.id)


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
    controls = camera_manager.get_controls(cam_id)
    return controls
