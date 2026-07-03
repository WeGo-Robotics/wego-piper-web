from fastapi import APIRouter
from pydantic import BaseModel

from app.services.zmq_bridge import zmq_bridge

router = APIRouter(prefix="/api/params", tags=["params"])


class ParamUpdate(BaseModel):
    params: dict


@router.post("")
async def update_params(body: ParamUpdate):
    safe, unsafe = zmq_bridge.validate_params(body.params)

    warnings = []
    if unsafe:
        warnings.append(
            f"Unsafe params ignored (restart required): {', '.join(unsafe)}"
        )

    if safe:
        await zmq_bridge.send_params(safe)

    return {
        "applied": safe,
        "warnings": warnings,
    }


@router.post("/pause")
async def pause_inference():
    await zmq_bridge.send_params({"pause": True})
    return {"status": "paused"}


@router.post("/resume")
async def resume_inference():
    await zmq_bridge.send_params({"pause": False})
    return {"status": "resumed"}


@router.post("/reset")
async def reset_inference():
    """로봇을 원위치로 복귀시키고 액션 버퍼/필터 상태를 초기화한 뒤 새로 시작."""
    await zmq_bridge.send_params({"reset": True})
    return {"status": "reset"}


class ManualAction(BaseModel):
    action: dict  # {"joint1.pos": 0.0, "joint2.pos": -50.0, ...}


@router.post("/manual-action")
async def send_manual_action(body: ManualAction):
    await zmq_bridge.send_params({"manual_action": body.action})
    return {"status": "sent"}
