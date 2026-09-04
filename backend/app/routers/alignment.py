"""정렬 검사 API (feature/alignment-check.md).

검사 정의와 기준은 공통 프리셋 스토어(`alignment` 도메인)에 둔다 — 이름으로
찾고 지우는 CRUD 가 이미 있고, 여기 필요한 건 **장치를 만져야 하는 것**뿐이다.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import presets
from app.services.alignment import (DOMAIN, POSE_DOMAIN, intrinsics_for,
                                    observe)

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
    #: 저장된 자세 이름. 비면 지금 자세를 찍는다.
    pose_name: str = ""


class PoseBody(BaseModel):
    """자세 하나. **지금 자세를 찍어서** 만든다 — 손으로 적게 하면 오타 하나가
    팔을 엉뚱한 곳으로 보낸다. 조그로 맞춘 뒤 저장하는 것이 그 화면이다."""

    name: str
    iface: str


def _load(name: str) -> dict:
    p = presets.get(DOMAIN, name)
    if p is None:
        raise HTTPException(404, f"'{name}' 검사가 없습니다")
    return dict(p.values)


def _values(domain: str, name: str) -> dict:
    """⚠ **`list_presets` 는 메타만 준다** — `values` 가 없다. 한때 여기서
    `row.get("values")` 를 읽어서 목록이 통째로 `null` 이었다: 화면이
    "null · null · 태그 null" 을 보여주고 기준·마지막 결과도 안 나왔다.
    값이 필요하면 이름으로 다시 읽어야 한다.
    """
    p = presets.get(domain, name)
    return dict(p.values) if p else {}


@router.get("")
async def list_checks():
    out = []
    for row in presets.list_presets(DOMAIN):
        name = row.get("name")
        v = _values(DOMAIN, name)
        out.append({"name": name, "iface": v.get("iface"),
                    "camera_id": v.get("camera_id"), "tag_id": v.get("tag_id"),
                    "tag_mm": v.get("tag_mm"), "family": v.get("family"),
                    "pose_name": v.get("pose_name"),
                    "baseline": v.get("baseline"), "last": v.get("last")})
    return {"checks": out}


def _read_pose(iface: str) -> dict[str, float]:
    """지금 관절 자세. 그리퍼는 뺀다 — 검사는 팔의 자세를 보는 것이고, 그리퍼가
    열려 있든 닫혀 있든 엔드이펙터 위치는 같아야 한다."""
    from app.services.robot_manager import robot_manager

    arm = robot_manager.arms.get(iface)
    if arm is None or not arm.connected:
        raise HTTPException(404, f"{iface} 가 연결되어 있지 않습니다")
    pose = arm.read_joints_normalized()
    if not pose:
        raise HTTPException(409, f"{iface} 의 관절값을 읽지 못했습니다")
    return {k: round(v, 2) for k, v in pose.items() if k != "gripper"}


@router.post("")
async def create_check(body: CheckBody):
    """검사를 만든다. 기준은 아직 없다 — 따로 잡는다.

    자세는 **저장된 자세**(`pose_name`)를 쓰거나, 없으면 지금 자세를 찍는다.
    """
    if body.pose_name:
        p = presets.get(POSE_DOMAIN, body.pose_name)
        if p is None:
            raise HTTPException(404, f"'{body.pose_name}' 자세가 없습니다")
        pose = dict(p.values).get("pose") or {}
        if not pose:
            raise HTTPException(409, f"'{body.pose_name}' 에 관절값이 없습니다")
    else:
        pose = _read_pose(body.iface)

    values = {**body.model_dump(exclude={"name"}), "pose": pose,
              "baseline": None, "last": None}
    presets.save(DOMAIN, body.name, values)
    return {"name": body.name, **values}


# ── 자세 프리셋 ──────────────────────────────────────────────────────────────

@router.get("/poses")
async def list_poses(iface: str = ""):
    """저장된 자세. `iface` 를 주면 그 팔의 것만.

    ⚠ 팔이 다르면 같은 관절값이 **다른 곳**을 가리킨다 — 캘리브레이션은 같아도
    팔이 놓인 위치가 다르다. 남의 팔 자세를 고르지 못하게 걸러 준다.
    """
    out = []
    for row in presets.list_presets(POSE_DOMAIN):
        name = row.get("name")
        v = _values(POSE_DOMAIN, name)
        if iface and v.get("iface") != iface:
            continue
        out.append({"name": name, "iface": v.get("iface"), "pose": v.get("pose")})
    return {"poses": out}


@router.post("/poses")
async def save_pose(body: PoseBody):
    """지금 자세를 이름 붙여 저장한다."""
    presets.save(POSE_DOMAIN, body.name,
                 {"iface": body.iface, "pose": _read_pose(body.iface)})
    return {"name": body.name, "iface": body.iface}


@router.delete("/poses/{name}")
async def delete_pose(name: str):
    if not presets.delete(POSE_DOMAIN, name):
        raise HTTPException(404, f"'{name}' 자세가 없습니다")
    return {"status": "deleted"}


@router.get("/tags/{cam_id:path}")
async def visible_tags(cam_id: str, tag_mm: float = 40.0, family: str = "36h11"):
    """지금 이 카메라에 보이는 태그 ID.

    ⚠ **자세를 만들 때 이게 보여야 한다.** 태그가 안 보이는 자세로 검사를 만들면
    실행할 때가 되어서야 "태그가 안 보입니다" 를 만난다 — 그때는 그 자세가 왜
    그렇게 정해졌는지도 잊은 뒤다.
    """
    from app.services.alignment import _frame

    intr = intrinsics_for(cam_id)
    if intr is None:
        return {"tags": [], "error": f"{cam_id} 의 내부 파라미터를 알 수 없습니다"}
    frame = _frame(cam_id)
    if frame is None:
        return {"tags": [], "error": f"{cam_id} 의 프레임을 읽지 못했습니다"}
    from piper_cam.tags import detect

    try:
        found = detect(frame, intr, float(tag_mm), family or "36h11")
    except Exception as exc:                                   # noqa: BLE001
        return {"tags": [], "error": str(exc)}
    return {"tags": sorted(p.tag_id for p in found)}


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
