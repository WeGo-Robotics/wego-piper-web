"""외부 제어 API — `/api/ext/v1` (feature/external-api.md).

외부 시스템이 이 로봇으로 **미션 수준** 프로젝트를 수행하는 창구다.
장치 수준을 열지 않는다 — 준비(정책 배포·장치)는 로컬 운영자 몫이고,
외부는 미션 제출·감시·중단만 한다. 내부 서비스를 직접 호출한다
(자기 HTTP 호출 금지 — 게이트웨이 안의 규칙).

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
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.routers import vision as vision_api
from app.services import exclusivity
from app.services.estop_bridge import estop_bridge
from app.services.plugins import registry

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


# ── 미션 = 플러그인 (feature/plugins.md §12-2) ──
#
# v1 표면은 그대로다. `type` 이 플러그인 이름이고 나머지 본문이 그 플러그인의 설정이다.
# 미션 타입 추가 = 플러그인 설치. 동시 1개 제한은 `mission: true` 플러그인에만 건다.


class MissionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = "recycling"
    cameras: dict[str, str] = {}


@router.post("/missions")
async def start_mission(body: MissionRequest):
    try:
        info = registry.get(body.type)
    except HTTPException:
        raise HTTPException(400, f"모르는 미션 타입: {body.type} — 설치된 플러그인 이름이어야 합니다")
    if info.manifest is None:
        raise HTTPException(400, f"플러그인 '{body.type}' 의 매니페스트가 깨져 있습니다: {info.error}")
    if not info.manifest.mission:
        raise HTTPException(400, f"'{body.type}' 은 미션 플러그인이 아닙니다 (plugin.yaml 의 mission: true)")
    live = registry.live_mission()
    if live:
        raise HTTPException(409, f"미션이 이미 돌고 있습니다: {live}")
    config = {k: v for k, v in body.model_dump().items() if k not in ("type", "cameras")}
    status = await registry.start(body.type, config, body.cameras)
    logger.info("외부 미션 시작: %s (%s)", status.get("run_id"), body.type)
    return {"id": status.get("run_id"), "type": body.type, "plugin": body.type,
            "max_episodes": config.get("max_episodes")}


@router.get("/missions")
async def list_missions():
    """미션 이력 — 저널 목록, 최신 먼저 (플러그인 전부)."""
    return {"missions": registry.runs()}


def _check_id(mission_id: str) -> None:
    if not _MISSION_ID.fullmatch(mission_id):
        raise HTTPException(400, "잘못된 미션 id")


@router.get("/missions/{mission_id}")
async def mission_status(mission_id: str):
    """진행 중이면 라이브 상태(플러그인 상태 KV), 끝났으면 저널의 회차 기록."""
    _check_id(mission_id)
    live = registry.find_live_run(mission_id)
    if live:
        return {"id": mission_id, "live": True, "plugin": live, **registry.status(live)}
    found = registry.run_events(mission_id)
    if found is None:
        raise HTTPException(404, "그런 미션이 없습니다")
    return {"id": mission_id, "live": False, "plugin": found[0], "events": found[1]}


@router.post("/missions/{mission_id}/cancel")
async def cancel_mission(mission_id: str):
    _check_id(mission_id)
    live = registry.find_live_run(mission_id)
    if not live:
        raise HTTPException(409, "그 미션은 실행 중이 아닙니다")
    await registry.stop(live)
    return {"id": mission_id, "status": "cancelled", "plugin": live}


# ── 플러그인 (feature/plugins.md §9 6단계) — 미션이 아닌 플러그인도 외부가 켠다 ──


class PluginStartRequest(BaseModel):
    config: dict = {}
    cameras: dict[str, str] = {}


@router.get("/plugins")
async def ext_list_plugins():
    out = []
    for info in registry.scan():
        d = registry.status(info.name)
        d["title"] = info.manifest.title if info.manifest else info.name
        d["error"] = info.error
        d["mission"] = bool(info.manifest and info.manifest.mission)
        out.append(d)
    return {"plugins": out}


@router.get("/plugins/{name}")
async def ext_plugin(name: str):
    registry.get(name)
    return registry.status(name)


@router.post("/plugins/{name}/start")
async def ext_plugin_start(name: str, body: PluginStartRequest):
    """플러그인 시작. 저장된 설정 위에 본문을 덮는다 — 비우면 화면에서 저장한 값으로 돈다."""
    saved = registry.saved_config(name)
    return await registry.start(name, {**saved["config"], **body.config}, {**saved["cameras"], **body.cameras})


@router.post("/plugins/{name}/stop")
async def ext_plugin_stop(name: str):
    registry.get(name)
    return await registry.stop(name)


# ── 상태 ──


@router.get("/status")
async def system_status():
    """종합 상태 — 외부 시스템이 폴링하는 한 장."""
    return {
        "activities": exclusivity.snapshot(),
        "estop": estop_bridge.status(),
        # 미션은 플러그인이다 (feature/plugins.md §12-2). `orchestrator` 키는 한 릴리스 동안 null 로 남긴다 —
        # 낡은 클라이언트가 키를 읽다 죽지 않게.
        "plugins": [registry.status(n) for n, _pid in registry.running().items()],
        "mission": registry.live_mission(),
        "orchestrator": None,
        "vision": await vision_api.detectord_status(),
    }


@router.get("/detections")
async def detections():
    return await vision_api.get_detections()


# ── 인식 제어 ──


@router.post("/vision/start")
async def vision_start(body: vision_api.StartRequest):
    return await vision_api.start_detectord(body)


@router.post("/vision/stop")
async def vision_stop():
    return await vision_api.stop_detectord()


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
