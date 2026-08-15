"""외부 제어 API — `/api/ext/v1` (feature/external-api.md).

외부 시스템이 이 로봇으로 **미션 수준** 프로젝트를 수행하는 창구다.
장치 수준을 열지 않는다 — 준비(정책 배포·장치)는 로컬 운영자 몫이고,
외부는 미션 제출·감시·중단만 한다. 내부 서비스를 직접 호출한다
(자기 HTTP 호출 금지 — episode-orchestrator §2 와 같은 규칙).

안전 계약: **외부 호출자가 곧 운영자다.** 브라우저가 하던 heartbeat 를
외부 클라이언트가 보내야 하고, 안 보내면 estopd 가 2.5초 안에 세운다.
그래서 heartbeat·E-stop 이 이 표면에 반드시 들어 있다.

인증: `PIPER_API_TOKEN` Bearer. 미설정이면 전체 503 — 기본 잠김.
"""

import json
import logging
import re
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException

from app.core.config import settings
from app.routers import vision as vision_api
from app.services import exclusivity
from app.services.estop_bridge import estop_bridge
from app.services.exclusivity import Activity, require_idle
from app.services.orchestrator import OrchestratorConfig, orchestrator

logger = logging.getLogger(__name__)


def _require_token(authorization: str = Header(default="")) -> None:
    token = settings.api_token
    if not token:
        raise HTTPException(503, "외부 API 비활성 — PIPER_API_TOKEN 을 설정하세요")
    if not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "잘못된 토큰")


router = APIRouter(
    prefix="/api/ext/v1", tags=["external"], dependencies=[Depends(_require_token)]
)

# 미션 id = 저널 run id. 경로 조작 방지 — 이 모양만 파일 이름으로 인정한다
_MISSION_ID = re.compile(r"^run_\d{8}_\d{6}$")


# ── 미션 ──


class MissionRequest(OrchestratorConfig):
    """v1 미션 = 분리수거 루프 설정 그대로. 타입 필드만 얹는다 —
    오케스트레이터 2단계(YAML 시나리오 스펙)가 오면 타입이 스펙 종류가 된다."""

    type: str = "recycling"


@router.post("/missions")
async def start_mission(body: MissionRequest):
    if body.type != "recycling":
        raise HTTPException(400, f"모르는 미션 타입: {body.type} (v1 은 recycling 뿐)")
    require_idle(Activity.ORCHESTRATOR)
    try:
        await orchestrator.start(OrchestratorConfig(**body.model_dump(exclude={"type"})))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    assert orchestrator.journal_path is not None
    mid = orchestrator.journal_path.stem
    logger.info("외부 미션 시작: %s (%d회)", mid, body.max_episodes)
    return {"id": mid, "type": body.type, "max_episodes": body.max_episodes}


@router.get("/missions")
async def list_missions():
    """미션 이력 — 저널 파일 목록, 최신 먼저."""
    journal_dir = settings.log_dir / "orchestrator"
    live = orchestrator.journal_path.stem if orchestrator.is_running and orchestrator.journal_path else None
    items = []
    if journal_dir.is_dir():
        for p in sorted(journal_dir.glob("run_*.jsonl"), reverse=True):
            items.append({
                "id": p.stem,
                "live": p.stem == live,
                "bytes": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
    return {"missions": items}


def _journal_file(mission_id: str):
    if not _MISSION_ID.fullmatch(mission_id):
        raise HTTPException(400, "잘못된 미션 id")
    return settings.log_dir / "orchestrator" / f"{mission_id}.jsonl"


@router.get("/missions/{mission_id}")
async def mission_status(mission_id: str):
    """진행 중이면 라이브 상태, 끝났으면 저널의 회차 기록."""
    path = _journal_file(mission_id)
    if (orchestrator.is_running and orchestrator.journal_path
            and orchestrator.journal_path.stem == mission_id):
        return {"id": mission_id, "live": True, **orchestrator.status()}
    if not path.is_file():
        raise HTTPException(404, "그런 미션이 없습니다")
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:      # 강제 종료로 잘린 마지막 줄
            pass
    return {"id": mission_id, "live": False, "events": events}


@router.post("/missions/{mission_id}/cancel")
async def cancel_mission(mission_id: str):
    _journal_file(mission_id)  # id 형식 검증
    if not (orchestrator.is_running and orchestrator.journal_path
            and orchestrator.journal_path.stem == mission_id):
        raise HTTPException(409, "그 미션은 실행 중이 아닙니다")
    await orchestrator.stop()
    return {"id": mission_id, "status": "cancelled"}


# ── 상태 ──


@router.get("/status")
async def system_status():
    """종합 상태 — 외부 시스템이 폴링하는 한 장."""
    return {
        "activities": exclusivity.snapshot(),
        "estop": estop_bridge.status(),
        "orchestrator": orchestrator.status(),
        "vision": await vision_api.yolod_status(),
    }


@router.get("/detections")
async def detections():
    return await vision_api.get_detections()


# ── 인식 제어 ──


@router.post("/vision/start")
async def vision_start(body: vision_api.StartRequest):
    return await vision_api.start_yolod(body)


@router.post("/vision/stop")
async def vision_stop():
    return await vision_api.stop_yolod()


# ── 안전 — 미션 중 heartbeat 는 의무다 ──


@router.post("/heartbeat")
async def heartbeat():
    """데드맨 생존 신호. 외부 클라이언트가 500ms~1s 주기로 보내야 한다."""
    estop_bridge.heartbeat()
    return {"status": "ok"}


@router.post("/estop")
async def estop():
    stopped = await estop_bridge.trigger_manual()
    return {"status": "stopped", "stopped": stopped}
