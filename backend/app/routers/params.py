from fastapi import APIRouter
from pydantic import BaseModel

from app.core.inference_params import bounds, defaults, realtime_params, spec_for_frontend
from app.routers.presets import register_domain
from app.services.param_bridge import param_bridge

router = APIRouter(prefix="/api/params", tags=["params"])

# 추론 프리셋 도메인 — 키와 범위 **모두 PARAM_SPEC 에서 파생한다.**
# 프리셋 코드가 파라미터 목록을 자체로 알면 그게 다섯 번째 사본이 된다
# (feature/parameter-presets.md "선행 의존").
#
# `task` 는 담지 않는다 — 태스크 텍스트는 "실행 대상"이지 튜닝이 아니다.
PRESET_DOMAIN = "inference"
register_domain(PRESET_DOMAIN, set(realtime_params()) , bounds())


@router.get("/spec")
async def param_spec():
    """추론 파라미터 스펙 — 프론트가 슬라이더를 여기서 생성한다.

    이전에는 기본값·범위·라벨이 프론트에 손으로 적혀 있어서 백엔드 클램프 범위와
    어긋났다 (`max_velocity` 가 프론트 500 / 백엔드 1000).
    """
    return {"params": spec_for_frontend(), "defaults": defaults()}


class ParamUpdate(BaseModel):
    params: dict


@router.post("")
async def update_params(body: ParamUpdate):
    safe, unsafe = param_bridge.validate_params(body.params)

    warnings = []
    if unsafe:
        warnings.append(
            f"Unsafe params ignored (restart required): {', '.join(unsafe)}"
        )

    if safe:
        await param_bridge.send_params(safe)

    return {
        "applied": safe,
        "warnings": warnings,
    }


@router.post("/pause")
async def pause_inference():
    await param_bridge.send_params({"pause": True})
    return {"status": "paused"}


@router.post("/resume")
async def resume_inference():
    await param_bridge.send_params({"pause": False})
    return {"status": "resumed"}


@router.post("/reset")
async def reset_inference():
    """로봇을 원위치로 복귀시키고 액션 버퍼/필터 상태를 초기화한 뒤 새로 시작."""
    await param_bridge.send_params({"reset": True})
    return {"status": "reset"}


class ManualAction(BaseModel):
    action: dict  # {"joint1.pos": 0.0, "joint2.pos": -50.0, ...}


@router.post("/manual-action")
async def send_manual_action(body: ManualAction):
    await param_bridge.send_params({"manual_action": body.action})
    return {"status": "sent"}
