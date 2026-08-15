"""YOLO 검출 · LLM 판단 테스트 API — 분리수거 파이프라인의 앞 두 칸을 화면에서 굴린다.

yolod 는 정책 서버와 같은 "필요할 때 켜는 유닛"이다 — `make_process` 가 소유자를
정하므로 게이트웨이를 재시작해도 산다. 판단은 [llm_client](../services/llm_client.py)
를 **직접 호출**한다 (자기 HTTP 호출 금지 — episode-orchestrator §2 와 같은 규칙).
"""

import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.config import settings
from app.services import llm_client
from app.services.llm_client import LLMJudgeError
from app.services.systemd_process import make_process

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vision", tags=["vision"])

# yolod 소유자 — 유닛이면 게이트웨이 재시작에도 산다 (기동 재부착은 사소해서 생략:
# 테스트 도구라 화면에서 다시 켜면 그만이고, 유닛 자체는 계속 돈다)
_yolod_pm = make_process("piper-yolod")

_YOLOD_SCRIPT = Path(__file__).resolve().parents[3] / "daemons" / "yolod.py"

# 규칙·슬롯의 정본은 오케스트레이터 서비스 — 테스트 페이지와 루프가 같은 판단을 쓴다
from app.services.orchestrator import DEFAULT_RULES, JudgeSlots  # noqa: E402


# ── yolod 제어 ──


@router.get("/segments")
async def list_camera_segments():
    """살아 있는 카메라 세그먼트 — yolod 가 구독할 수 있는 것들."""
    from piper_shm import list_segments

    return {"segments": list_segments()}


class StartRequest(BaseModel):
    cams: dict[str, str]          # alias → 세그먼트 cam_id
    model: str = "yolo11n.pt"
    fps: float = 5.0
    conf: float = 0.25


@router.post("/start")
async def start_yolod(body: StartRequest):
    if not body.cams:
        raise HTTPException(400, "구독할 카메라가 없습니다")
    args = [settings.grpc_python, "-u", str(_YOLOD_SCRIPT)]
    for alias, cam_id in body.cams.items():
        args += ["--cam", f"{alias}={cam_id}"]
    args += ["--model", body.model, "--fps", str(body.fps), "--conf", str(body.conf)]
    await _yolod_pm.start(args)
    return {"status": "started", "cams": body.cams}


@router.post("/stop")
async def stop_yolod():
    await _yolod_pm.stop()
    return {"status": "stopped"}


@router.get("/status")
async def yolod_status():
    from piper_bus.client import Bus

    try:
        names = Bus().detection_names()
    except Exception:
        names = []
    return {"state": _yolod_pm.state.value, "pid": _yolod_pm.pid, "cams": names}


# ── 검출 조회 ──


@router.get("/detections")
async def get_detections():
    """살아 있는(TTL 안 지난) 검출 전부. `text` 필드가 곧 LLM 프롬프트 재료다."""
    from piper_bus.client import Bus

    bus = Bus()
    return {name: bus.get_detections(name) for name in bus.detection_names()}


@router.get("/preview/{name}")
async def get_annotated_preview(name: str):
    """yolod 의 어노테이트 프리뷰 (버스 `yolo_<name>` 키)."""
    from piper_bus.client import Bus

    data = Bus().get_preview(f"yolo_{name}")
    if data is None:
        raise HTTPException(404, "프리뷰 없음 — yolod 가 돌고 있습니까?")
    return Response(content=data, media_type="image/jpeg")


# ── LLM 판단 ──


@router.get("/judge/defaults")
async def judge_defaults():
    return {
        "rules": DEFAULT_RULES,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "stats": llm_client.stats,
    }


class JudgeRequest(BaseModel):
    user: str                      # 검출 텍스트 등 가변 입력
    system: str = DEFAULT_RULES
    provider: str | None = None    # 기본: 설정 (openai_compat/Ollama)
    model: str | None = None
    timeout_s: float = 60.0


@router.post("/judge")
async def run_judge(body: JudgeRequest):
    t0 = time.monotonic()
    try:
        slots = await llm_client.judge(
            body.system, body.user, JudgeSlots,
            timeout_s=body.timeout_s, provider=body.provider, model=body.model,
        )
    except LLMJudgeError as e:
        # reason 을 앞에 실어 넘긴다 — 화면이 "폴백을 정하는 호출자" 역할을 한다
        raise HTTPException(502, f"{e.reason}: {e.detail}" if e.detail else e.reason)
    return {
        "slots": slots.model_dump(),
        "ms": round((time.monotonic() - t0) * 1000),
        "provider": body.provider or settings.llm_provider,
        "model": body.model or settings.llm_model,
    }
