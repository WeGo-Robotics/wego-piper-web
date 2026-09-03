"""정렬 검사 API (feature/alignment-check.md).

검사 정의와 기준은 공통 프리셋 스토어(`alignment` 도메인)에 둔다 — 이름으로
찾고 지우는 CRUD 가 이미 있고, 여기 필요한 건 **장치를 만져야 하는 것**뿐이다.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import presets
from app.services.alignment import DOMAIN, intrinsics_for, observe

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alignment", tags=["alignment"])


class CheckBody(BaseModel):
    """검사 하나. **자세는 현재 자세를 찍어서** 만든다 — 손으로 적게 하면
    오타 하나가 팔을 엉뚱한 곳으로 보낸다."""

    name: str
    iface: str
    camera_id: str
    tag_id: int = 0
    tag_mm: float = 40.0
    family: str = "36h11"


def _load(name: str) -> dict:
    p = presets.get(DOMAIN, name)
    if p is None:
        raise HTTPException(404, f"'{name}' 검사가 없습니다")
    return dict(p.values)


@router.get("")
async def list_checks():
    out = []
    for row in presets.list_presets(DOMAIN):
        v = row.get("values") or {}
        out.append({"name": row.get("name"), "iface": v.get("iface"),
                    "camera_id": v.get("camera_id"), "tag_id": v.get("tag_id"),
                    "tag_mm": v.get("tag_mm"), "family": v.get("family"),
                    "baseline": v.get("baseline"), "last": v.get("last")})
    return {"checks": out}


@router.post("")
async def create_check(body: CheckBody):
    """지금 자세를 찍어 검사를 만든다. 기준은 아직 없다 — 따로 잡는다."""
    from app.services.robot_manager import robot_manager

    arm = robot_manager.arms.get(body.iface)
    if arm is None or not arm.connected:
        raise HTTPException(404, f"{body.iface} 가 연결되어 있지 않습니다")
    pose = arm.read_joints_normalized()
    if not pose:
        raise HTTPException(409, f"{body.iface} 의 관절값을 읽지 못했습니다")
    # ⚠ 그리퍼는 뺀다 — 검사는 팔의 자세를 보는 것이고, 그리퍼가 열려 있든
    #   닫혀 있든 엔드이펙터 위치는 같아야 한다.
    pose = {k: round(v, 2) for k, v in pose.items() if k != "gripper"}

    values = {**body.model_dump(exclude={"name"}), "pose": pose,
              "baseline": None, "last": None}
    presets.save(DOMAIN, body.name, values)
    return {"name": body.name, **values}


@router.delete("/{name}")
async def delete_check(name: str):
    if not presets.delete(DOMAIN, name):
        raise HTTPException(404, f"'{name}' 검사가 없습니다")
    return {"status": "deleted"}


@router.post("/{name}/baseline")
async def capture_baseline(name: str):
    """⚠ **팔이 정상일 때** 잡아야 한다. 이 값이 이후 모든 판단의 0 점이 되므로,
    틀어진 상태에서 잡으면 그 틀어짐이 '정상' 이 된다."""
    check = _load(name)
    pose = observe(check).to_dict()
    check["baseline"] = {"at": __import__("time").time(), "pose": pose}
    check["last"] = None
    presets.save(DOMAIN, name, check)
    return {"name": name, "baseline": check["baseline"]}


@router.post("/{name}/run")
async def run_check(name: str):
    from piper_cam.tags import TagPose, deviation

    check = _load(name)
    base = check.get("baseline")
    if not base:
        raise HTTPException(409,
            f"'{name}' 에 기준이 없습니다 — 팔이 정상일 때 기준을 먼저 잡으세요. "
            f"기준이 없으면 '얼마나 틀어졌나' 를 잴 대상이 없습니다.")
    now = observe(check)
    result = {"at": __import__("time").time(), "pose": now.to_dict(),
              **deviation(TagPose.from_dict(base["pose"]), now)}
    check["last"] = result
    presets.save(DOMAIN, name, check)
    return {"name": name, "result": result}


@router.get("/intrinsics/{cam_id:path}")
async def get_intrinsics(cam_id: str):
    """이 카메라로 정렬 검사를 할 수 있나. 없으면 이유가 곧 답이다."""
    intr = intrinsics_for(cam_id)
    if intr is None:
        return {"available": False,
                "why": "내부 파라미터를 알 수 없습니다 — RealSense 를 연결하세요."}
    return {"available": True, "fx": intr.fx, "fy": intr.fy,
            "cx": intr.cx, "cy": intr.cy, "model": intr.model,
            "distorted": bool(intr.coeffs and any(intr.coeffs))}
