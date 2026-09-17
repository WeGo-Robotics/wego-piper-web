"""메시 자산 — 사람이 올린 물건 (feature/sim-scene-editor.md 3단계).

⚠ **raw 바디로 받는다.** multipart 는 `python-multipart` 의존성을 끌고 오는데, 이 저장소는
이미 같은 이유로 raw 바디를 쓴다(YOLO 이미지·가중치 업로드). 창구를 둘로 만들 이유가 없다.

⚠ 사람이 고칠 수 있는 실패는 **400 과 그 문장**이다. "ASCII STL 입니다 — 바이너리로
내보내세요" 가 "업로드 실패" 로 바뀌면 그 사람은 영영 못 고친다. ASCII STL 과 GLB 는
흔한 내보내기 기본값이라 반드시 온다.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import sim_assets

router = APIRouter(prefix="/api/sim/assets", tags=["sim-assets"])

#: 스캔 원본은 수십 MB 가 예사다 — 받기 전에 끊는다(저장소 자체 상한과 같은 값).
LIMIT_BYTES = 32 << 20


class ScaleBody(BaseModel):
    unit_scale: float


class NameBody(BaseModel):
    name: str


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except sim_assets.AssetError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:               # MeshError 도 여기로 온다 (같은 뿌리)
        raise HTTPException(400, str(exc))


@router.get("")
async def list_assets():
    return {"assets": await asyncio.to_thread(sim_assets.listing)}


@router.post("")
async def upload(request: Request, filename: str = "mesh.obj", name: str = ""):
    """메시 파일 하나. 받으면서 재고, **못 쓰는 파일은 디스크에 안 남긴다.**"""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > LIMIT_BYTES:
            raise HTTPException(413, f"{LIMIT_BYTES >> 20}MB 를 넘습니다 — "
                                     "Blender 의 데시메이트로 면을 줄여 오세요")
    if not data:
        raise HTTPException(400, "빈 파일입니다")
    return await asyncio.to_thread(_guard, sim_assets.add, bytes(data), filename, name)


@router.get("/{asset_id}")
async def get_asset(asset_id: str):
    return await asyncio.to_thread(_guard, sim_assets.meta, asset_id)


@router.put("/{asset_id}/scale")
async def set_scale(asset_id: str, body: ScaleBody):
    """단위 확인 — **사람만 알 수 있다.** OBJ·STL 에 단위가 없어서 추측은 추측일 뿐이다."""
    return await asyncio.to_thread(_guard, sim_assets.set_unit_scale, asset_id, body.unit_scale)


@router.put("/{asset_id}/name")
async def set_name(asset_id: str, body: NameBody):
    return await asyncio.to_thread(_guard, sim_assets.rename, asset_id, body.name)


@router.delete("/{asset_id}")
async def delete_asset(asset_id: str):
    return {"deleted": await asyncio.to_thread(_guard, sim_assets.delete, asset_id)}
