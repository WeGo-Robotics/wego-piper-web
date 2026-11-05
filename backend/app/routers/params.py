from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.inference_params import bounds, defaults, foreign_to, realtime_params, spec_for_frontend
from app.routers.presets import register_domain
from app.services import inference_state
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
    # 돌고 있는 정책이 안 쓰는 정책별 파라미터는 버린다 — 시작 때와 같은 규칙
    # (inference_params.foreign_to). 화면은 그 카드를 감추지만 프리셋·외부 API 는 그대로 보낸다.
    params, foreign = dict(body.params), []
    running = inference_state.get()
    if running is not None and running.policy_type:
        foreign = sorted(set(params) & foreign_to(running.policy_type))
        for key in foreign:
            params.pop(key)
    safe, unsafe = param_bridge.validate_params(params)

    warnings = []
    if "target" in params and "target" not in safe:
        warnings.append("목표 위치는 [u, v] 두 값(0~1)이어야 합니다 — 무시했습니다")
    if foreign:
        warnings.append(f"이 정책이 쓰지 않는 파라미터라 무시했습니다: {', '.join(foreign)}")
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


# ── 여러 정책 (feature/multi-policy.md) ──

class PolicySwitch(BaseModel):
    name: str


@router.post("/policy")
async def switch_policy(body: PolicySwitch):
    """올려 둔 정책 중 하나로 바꾼다. 팔은 지금 자리에서 그 정책의 첫 청크로 이어진다(파킹 없음).

    바뀌었는지는 `GET /api/models/inference/policy` 의 `active` 로 본다 — wrapper 가 도는 추론을 끝낸 뒤
    갈아끼우고 텔레메트리로 알려야 바뀐다. 지금 이름을 다시 보내면 그 정책을 처음부터 다시 돈다.
    """
    running = inference_state.get()
    if running is None:
        raise HTTPException(409, "추론이 돌고 있지 않습니다")
    if body.name not in running.policies:
        raise HTTPException(400, f"올려 둔 정책이 아닙니다: {body.name} (올린 정책: {', '.join(running.policies) or '-'})")
    await param_bridge.send_params({"policy": body.name})
    return {"status": "requested", "policy": body.name}


class Hold(BaseModel):
    on: bool = True


@router.post("/hold")
async def hold_inference(body: Hold):
    """새 추론 없이 마지막 액션을 계속 보낸다 — 물건을 든 채 기다릴 때. 끄면 지금 정책을 처음부터 다시.

    ⚠ 일시정지(`/pause`)와 다르다. 일시정지는 명령을 멈춰 robotd 데드맨이 **측정한 자세**를 세우고, 물체에
    막혀 벌어진 그리퍼 폭이 목표가 되면 쥐는 힘이 빠질 수 있다. 유지는 쥐라는 마지막 지령을 그대로 보낸다.
    """
    if inference_state.get() is None:
        raise HTTPException(409, "추론이 돌고 있지 않습니다")
    await param_bridge.send_params({"hold": body.on})
    return {"status": "holding" if body.on else "released"}
