from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.process_manager import process_manager
from app.services.model_scanner import get_model
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager

router = APIRouter(prefix="/api/inference", tags=["inference"])

PIPER_JOINTS = 7  # Piper 팔 1대의 관절 수

# 마지막 선택값 (서버 메모리)
_last_selection: dict = {
    "follower_iface": "",
    "model_id": "",
    "camera_mapping": {},
}


class SelectionRequest(BaseModel):
    follower_iface: str = ""
    model_id: str = ""
    camera_mapping: dict[str, str] = {}


@router.get("/selection")
async def get_selection():
    return _last_selection


@router.post("/selection")
async def save_selection(body: SelectionRequest):
    _last_selection["follower_iface"] = body.follower_iface
    _last_selection["model_id"] = body.model_id
    _last_selection["camera_mapping"] = body.camera_mapping
    return {"status": "ok"}


@router.get("/status")
async def inference_status():
    # 실행 중인 추론의 카메라 프리뷰 파일 목록
    import glob
    cam_names = []
    for f in sorted(glob.glob("/tmp/piper_cam_*.jpg")):
        name = f.split("piper_cam_")[1].replace(".jpg", "")
        cam_names.append(name)
    return {
        "state": process_manager.state.value,
        "pid": process_manager.pid,
        "cameras": cam_names,
    }


class ValidateRequest(BaseModel):
    model_id: str
    follower_iface: str
    camera_mapping: dict[str, str]  # {"top": "/dev/video0", "hand": "/dev/video1"}


@router.post("/validate")
async def validate_config(body: ValidateRequest):
    """추론 시작 전 모델-로봇-카메라 매핑 검증."""
    errors: list[str] = []
    warnings: list[str] = []

    # 1) 모델 확인
    model = get_model(body.model_id)
    if not model:
        raise HTTPException(404, "모델을 찾을 수 없습니다")

    reqs = model.get("requirements", {})
    required_cameras = reqs.get("required_cameras", [])
    state_dim = reqs.get("state_dim", 0)
    action_dim = reqs.get("action_dim", 0)

    # 2) follower 확인
    arm = robot_manager.arms.get(body.follower_iface)
    if not arm:
        errors.append(f"로봇 '{body.follower_iface}'을(를) 찾을 수 없습니다")
    elif not arm.ready:
        errors.append(f"로봇 '{body.follower_iface}'이(가) 등록되지 않았습니다")
    elif arm.role != "follower":
        errors.append(f"'{body.follower_iface}'은(는) follower가 아닙니다 (현재: {arm.role})")

    # 3) 관절 수 검증
    # TODO: 로봇별 관절 수를 동적으로 가져오기. 현재는 Piper 기준 하드코딩
    robot_joints = PIPER_JOINTS
    # 프론트가 같은 상수를 따로 들고 있지 않도록 응답에 실어 보낸다
    if state_dim > 0 and robot_joints != state_dim:
        warnings.append(f"관절 수 불일치: 모델 state={state_dim}, 로봇={robot_joints} (모델이 자동 패딩/트림)")
    if action_dim > 0 and robot_joints != action_dim:
        errors.append(f"액션 차원 불일치: 모델={action_dim}, 로봇={robot_joints}")

    # 4) 카메라 매핑 검증
    required_names = {c["name"] for c in required_cameras}
    mapped_names = set(body.camera_mapping.keys())

    # 매핑되지 않은 카메라
    missing = required_names - mapped_names
    if missing:
        errors.append(f"매핑되지 않은 카메라: {', '.join(missing)}")

    # 불필요한 매핑
    extra = mapped_names - required_names
    if extra:
        warnings.append(f"모델에 없는 카메라 매핑 무시됨: {', '.join(extra)}")

    # 매핑된 카메라가 등록(ready)되어 있는지
    ready_cam_ids = {c["id"] for c in camera_manager.get_ready_cameras()}
    for cam_name, cam_id in body.camera_mapping.items():
        if cam_name not in required_names:
            continue
        if cam_id not in ready_cam_ids:
            errors.append(f"카메라 '{cam_id}'이(가) 등록되지 않았습니다 (매핑: {cam_name})")

    # 5) 해상도 경고
    for req_cam in required_cameras:
        cam_id = body.camera_mapping.get(req_cam["name"])
        if not cam_id:
            continue
        cam = camera_manager.cameras.get(cam_id)
        if cam and req_cam["width"] and req_cam["height"]:
            if cam.width and cam.height:
                if cam.width != req_cam["width"] or cam.height != req_cam["height"]:
                    warnings.append(
                        f"카메라 '{req_cam['name']}' 해상도 차이: "
                        f"모델={req_cam['width']}x{req_cam['height']}, "
                        f"카메라={cam.width}x{cam.height}"
                    )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "model_requirements": reqs,
        "robot_joints": robot_joints,
    }


@router.get("/camera/{cam_name}")
async def inference_camera_preview(cam_name: str):
    """추론 중 카메라 프리뷰 (wrapper가 /tmp에 저장한 JPEG)."""
    path = Path(f"/dev/shm/piper_cam_{cam_name}.jpg")
    if not path.exists():
        path = Path(f"/tmp/piper_cam_{cam_name}.jpg")
    if not path.exists():
        raise HTTPException(404, "Preview not available")
    return Response(content=path.read_bytes(), media_type="image/jpeg")
