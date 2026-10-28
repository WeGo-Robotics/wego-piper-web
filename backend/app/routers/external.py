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
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.routers import vision as vision_api
from app.services import exclusivity
from app.services.estop_bridge import estop_bridge
from app.services.plugins import registry

logger = logging.getLogger(__name__)


# 마지막으로 통과한 외부 호출 — 설정 → 에이전트 탭이 "붙어 있나" 를 답하는 재료 (feature/agent-cli.md §4-7).
# 메모리뿐이라 게이트웨이가 재시작하면 비는데, 그것도 맞는 답이다.
_LAST_CALL: dict = {}


def last_call() -> dict | None:
    return dict(_LAST_CALL) or None


def _require_token(request: Request, authorization: str = Header(default="")) -> None:
    token = settings.api_token
    if not token:
        raise HTTPException(503, "외부 API 비활성 — PIPER_API_TOKEN 을 설정하세요")
    if not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "잘못된 토큰")
    _LAST_CALL.update(at=time.time(), method=request.method, path=request.url.path)


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
    arms: dict[str, str] = {}


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
    config = {k: v for k, v in body.model_dump().items() if k not in ("type", "cameras", "arms")}
    status = await registry.start(body.type, config, body.cameras, arms=body.arms)
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
    inputs: dict[str, str] = {}
    arms: dict[str, str] = {}


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
    return await registry.start(name, {**saved["config"], **body.config}, {**saved["cameras"], **body.cameras},
                                {**saved.get("inputs", {}), **body.inputs}, {**saved.get("arms", {}), **body.arms})


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


# ── 읽기 전용 (feature/agent-cli.md §4-4) ──
#
# MCP 리소스의 재료이자 외부 집계 경로다. 전부 GET 이고 장치 수준은 여전히 없다 —
# external-api.md §4 "안 여는 것" 표는 그대로다. 라우터 의존성이 인증을 걸고
# `test_auth_covers_every_route` 가 그걸 대조한다. 내부 서비스를 직접 부른다(자기 HTTP 호출 금지).


@router.get("/diag/context")
async def ext_diag_context(since: str | None = None, unit: str = "all", lines: int = 200, format: str = "markdown"):
    """에이전트용 컨텍스트 꾸러미 — `/api/system/agent-context` 와 같은 본문
    (버전·유닛·활동·E-stop·장치·경고 이상 저널·미션 저널·읽을 문서). 토큰·키는 지워진다."""
    import asyncio

    from fastapi.responses import PlainTextResponse

    from app.routers.system import check_agent_context_args
    from app.services import agent_context as ac

    check_agent_context_args(unit, format)
    data = await asyncio.to_thread(ac.collect, since=since, unit=unit, lines=lines)
    if format == "json":
        return ac.redact_data(data)
    return PlainTextResponse(ac.render_markdown(data), media_type="text/markdown; charset=utf-8")


def _cameras_of(features: dict) -> list[str]:
    prefix = "observation.images."
    return sorted(k[len(prefix):] for k in (features or {}) if k.startswith(prefix))


@router.get("/datasets")
async def ext_datasets():
    """로컬 데이터셋 목록 — 이름·에피소드·프레임·fps·카메라·구운 사본(stage/delta)·노트·장면.
    `features` 전체와 경로는 내지 않는다 — 외부가 알 일이 아니고 크다."""
    import asyncio

    from app.services.dataset_scanner import scan_datasets

    items = await asyncio.to_thread(scan_datasets)
    return {"datasets": [{
        "id": d.get("id"), "source": d.get("source"),
        "total_episodes": d.get("total_episodes"), "total_frames": d.get("total_frames"), "fps": d.get("fps"),
        "cameras": _cameras_of(d.get("features") or {}),
        "baked": d.get("baked"), "delta": d.get("delta"), "notes": d.get("notes"), "scene": d.get("scene"),
        "created": d.get("created"), "modified": d.get("modified"), "size_bytes": d.get("size_bytes"),
    } for d in items]}


@router.get("/datasets/{dataset_id:path}/episodes/{episode}")
async def ext_episode(dataset_id: str, episode: int):
    """에피소드 요약 — 길이·task·페이즈 구간(분석했으면)·노트·구운 사본 표식.
    프레임은 내지 않는다(그건 5단계의 묶음). `phases` 가 null 이면 아직 분석하지 않은 것이다."""
    import asyncio

    from piper_phase import labeler as PL

    from app.services.dataset_scanner import baked_info, delta_info, episode_meta
    from app.services.notes_sidecar import read_notes

    found = await asyncio.to_thread(episode_meta, dataset_id, episode)
    if not found:
        raise HTTPException(404, f"데이터셋 '{dataset_id}' 에 에피소드 {episode} 가 없습니다")
    ds_path, meta, rec = found
    labels = await asyncio.to_thread(PL.load, ds_path) or {}
    entry = (labels.get("episodes") or {}).get(str(episode))
    names = getattr(PL, "PHASE_NAMES", None)
    phases = None
    if entry is not None:
        segs = []
        for seg in entry.get("segments") or []:
            try:
                start, end, idx = seg
                segs.append({"start": start, "end": end,
                             "phase": names[idx] if names and 0 <= int(idx) < len(names) else idx})
            except (TypeError, ValueError):
                segs.append({"raw": seg})
        phases = {"segments": segs, "reviewed": bool(entry.get("reviewed")), "note": entry.get("note"),
                  "cycles": entry.get("cycles"), "version": labels.get("version")}
    return {
        "dataset": dataset_id, "episode": episode, "fps": meta.get("fps"),
        "length": rec.get("length"), "tasks": rec.get("tasks"),
        "phases": phases,
        "notes": read_notes(ds_path, kind="dataset"),
        "baked": baked_info(ds_path), "delta": delta_info(ds_path),
    }


@router.get("/eval/stats")
async def ext_eval_stats(last_n: int = 0):
    """평가 통계 — `/api/eval/stats` 그대로 (성공률·프리셋/체크포인트/장면별)."""
    from app.routers import eval_log

    return await eval_log.eval_stats(last_n)


@router.get("/training")
async def ext_training():
    """학습 상태·진행률·러너 + job 목록 — `/api/training/status`·`/jobs` 그대로."""
    from app.services.training import train_manager
    from app.services.training.jobs import job_registry

    return {"status": train_manager.get_status(), "jobs": [j.to_dict() for j in job_registry.list()]}


@router.get("/datasets/{dataset_id:path}/episodes/{episode}/bundle")
async def ext_episode_bundle(dataset_id: str, episode: int, cams: str | None = None, at: str = "phases"):
    """에피소드 묶음 zip — `/api/datasets/{id}/episodes/{ep}/bundle` 과 같다 (feature/agent-cli.md §4-5).
    키프레임·타임라인·상태/액션 표·README. `piper_episode_bundle` 도구가 받아서 로컬에 푼다."""
    import asyncio

    from fastapi.responses import Response

    from app.services import episode_bundle as EB

    try:
        data, _summary = await asyncio.to_thread(EB.build, dataset_id, episode, cams=EB.parse_cams(cams), at=at)
    except EB.BundleError as exc:
        raise HTTPException(exc.status, str(exc))
    name = f"{dataset_id.replace('/', '__')}-ep{episode:06d}.zip"
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
