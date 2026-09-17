"""가상환경 — 테이블 위 사물을 사람이 편집한다 (feature/sim-scene-editor.md §8).

편집기는 **독립 페이지**(`/scene`)다. 여기는 그 페이지가 쓰는 API 뿐이다:
목록·읽기·저장·지우기·적용, 그리고 **환경 불러오기/내보내기**(파일 하나).

⚠ 사람이 고칠 수 있는 실패는 **400 과 그 문장**으로 돌려준다. "저장 실패" 같은 말로
바꾸면 "id 'link3' 는 팔·테이블·카메라가 쓰는 이름입니다" 가 사라져서, 사람은 무엇을
고쳐야 할지 모른 채 같은 걸 다시 누른다. 이 저장소가 같은 실수를 여러 번 했다.
"""

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.services import sim_scenes

router = APIRouter(prefix="/api/sim/scenes", tags=["sim-scenes"])


class SceneBody(BaseModel):
    spec: dict


class ImportBody(BaseModel):
    text: str
    name: str = ""


class PlaceBody(BaseModel):
    id: str
    x: float
    y: float


class PlaceFromViewBody(BaseModel):
    id: str
    cam: str = "sim:top"
    u: float
    v: float
    aspect: float = 4.0 / 3.0


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except sim_scenes.SceneStoreError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:          # simd 가 거절했다 — 그 말을 그대로 전한다
        raise HTTPException(400, str(exc))


@router.get("")
async def list_scenes():
    """가상환경 목록. 깨진 파일도 `error` 를 달아 같이 낸다 — 숨기면 아무도 못 고친다."""
    return {"scenes": await asyncio.to_thread(sim_scenes.listing),
            "current": await asyncio.to_thread(sim_scenes.current_id)}


@router.get("/defaults")
async def defaults():
    """편집기가 폼을 그릴 재료 — 쓸 수 있는 모양·프리셋과 그 기본값.

    화면이 목록을 **따로 적지 않게** 한다. 따로 적으면 프리셋을 늘릴 때마다 두 곳을
    고쳐야 하고, 한 곳을 잊으면 화면에만 없거나 화면에만 있는 모양이 생긴다.
    """
    from piper_sim import scene_spec as S

    return {
        "primitives": {k: {"size_len": n} for k, (_, n) in S.PRIMITIVES.items()},
        "presets": {k: {"params": d} for k, (d, _) in S.PRESETS.items()},
        "reserved": sorted(S.RESERVED_IDS),
        "max_objects": S.MAX_OBJECTS,
        "movable_physics": S.MOVABLE_PHYSICS,
        "static_physics": S.STATIC_PHYSICS,
    }


@router.get("/{sid}")
async def get_scene(sid: str):
    return await asyncio.to_thread(_guard, sim_scenes.read, sid)


@router.get("/{sid}/export", response_class=PlainTextResponse)
async def export_scene(sid: str):
    """환경 내보내기 — 파일로 저장할 내용 그대로."""
    return await asyncio.to_thread(_guard, sim_scenes.export_text, sid)


@router.put("/{sid}")
async def save_scene(sid: str, body: SceneBody):
    return await asyncio.to_thread(_guard, sim_scenes.save, sid, body.spec)


@router.delete("/{sid}")
async def delete_scene(sid: str):
    return {"deleted": await asyncio.to_thread(_guard, sim_scenes.delete, sid)}


@router.post("/import")
async def import_scene(body: ImportBody):
    """환경 불러오기 — 파일 내용을 받아 새 가상환경으로. 겹치면 덮지 않고 이름을 늘린다."""
    return await asyncio.to_thread(_guard, sim_scenes.import_text, body.text, body.name)


@router.post("/{sid}/apply")
async def apply_scene(sid: str):
    """가상환경을 시뮬에 올린다 — 실패하면 적용 표시를 안 바꾼다."""
    return await asyncio.to_thread(_guard, sim_scenes.apply, sid)


@router.get("/live/objects")
async def live_objects():
    """지금 세계에 있는 물체와 **그 위치** — 배치 화면이 폴링한다."""
    from app.services import sim_robot_client as sim

    return {"objects": await asyncio.to_thread(sim.call, "objects", default=[]) or []}


@router.post("/live/place")
async def place_object(body: PlaceBody):
    """물체를 테이블 위 (x, y) 로. 고정물이면 데몬이 거절하고 그 이유가 그대로 온다."""
    from app.services import sim_robot_client as sim

    pos = await asyncio.to_thread(_guard, sim.call_strict, "place_object", body.id, body.x, body.y)
    return {"id": body.id, "pos": pos}


@router.post("/live/point-from-view")
async def point_from_view(body: PlaceFromViewBody):
    """클릭한 픽셀 → 테이블 좌표. **물체는 안 옮긴다** — 고정물을 끌 때 쓴다.

    고정물은 자유관절이 없어 qpos 로 못 움직인다(컴파일에 자리가 박혀 있다). 편집기가 이
    좌표를 명세에 적고 다시 올린다 — 그래서 통도 마우스로 옮겨진다.
    """
    from app.services import sim_robot_client as sim

    r = await asyncio.to_thread(_guard, sim.call_strict, "point_from_view",
                                body.cam, body.u, body.v, body.aspect, 0.0)
    if not r or not r.get("ok"):
        raise HTTPException(400, "클릭한 자리가 테이블이 아닙니다")
    return {"point": r.get("point")}


@router.post("/live/place-from-view")
async def place_from_view(body: PlaceFromViewBody):
    """탑뷰에서 클릭한 픽셀로 물체를 옮긴다 — 광선→테이블 계산은 **카메라 자세를 아는**
    데몬이 한다. 화면이 하면 fovy·카메라 회전을 다시 적어야 하고, 가상환경이 바뀌면 갈린다."""
    from app.services import sim_robot_client as sim

    r = await asyncio.to_thread(_guard, sim.call_strict, "object_from_view",
                                body.cam, body.u, body.v, body.aspect, body.id)
    if not r or not r.get("ok"):
        raise HTTPException(400, "클릭한 자리가 테이블이 아닙니다")
    return {"id": body.id, "pos": r.get("cube")}
