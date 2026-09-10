"""서비스 상태와 재시작 — "고쳤는데 왜 안 되지"를 없앤다.

유닛은 **기동 시점의 코드로 돈다.** 이 저장소는 그걸로 두 번 크게 헤맸다:
rsd 가 이틀 전 코드로 돌아 고친 버그가 재현됐고, 게이트웨이가 새 라우트를
모른 채로 404 만 돌려줬다. 화면이 그 사실을 말해주면 둘 다 몇 초짜리 일이다.
"""

import asyncio
import logging
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import units
from app.services.exclusivity import Activity, require_idle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/resources")
async def resources(since: float | None = None):
    """GPU·CPU·디스크 + 추이 견본. **대시보드 전용이라 실패해도 200 이다.**

    ⚠ `nvidia-smi` 는 드라이버가 걸리면 D-state 로 멈춘다 — D405 의 UVC 질의로
    똑같이 겪었고 그때 **이벤트 루프 전체가 먹통**이 됐다. `to_thread` 로 빼서
    루프를 막지 않고, 안에서 타임아웃을 건다. 자원 표시가 없는 것과 웹이 안 뜨는
    것은 비교할 일이 아니다.

    GPU 는 샘플러(trends)가 4초마다 뜬 마지막 견본을 재사용한다 — 같은 위험한
    호출을 폴링마다 또 하지 않기 위해서다. 샘플러가 아직 안 떴을 때만 직접 묻는다.

    `samples` 는 서버가 쌓아 둔 최근 15분 추이다. `since`(epoch 초) 없이 부르면
    창 전체가 온다 — **페이지 로딩 때 그래프가 처음부터 차 있는 이유다.**
    이후 폴링은 마지막 견본의 `t` 를 `since` 로 넘겨 새 것만 받는다.
    """
    from app.core.config import settings
    from app.services import resources as res
    from app.services import trends

    gpu_list = trends.latest_gpus() or await asyncio.to_thread(res.gpus)
    disks = [d for d in (await asyncio.to_thread(res.disk, str(settings.datasets_dir)),)
             if d]
    return {"gpus": gpu_list, "disks": disks,
            "cpu_pct": trends.latest_cpu(),
            "samples": trends.samples(since)}


@router.get("/services")
async def list_services():
    """유닛 목록 + 게이트웨이 자신. `stale` 이 이 응답의 요점이다."""
    return {"units": [u.to_dict() for u in units.list_units()],
            "gateway": units.gateway_status()}


@router.get("/version")
async def version_info():
    """지금 도는 버전 + 바깥 소프트웨어 (feature/version-update.md §2).
    정본은 코드 밖(이미지 매니페스트·git)이라 여기서 지어내지 않는다."""
    from app.services import version

    return await asyncio.to_thread(version.collect)


@router.get("/update/check")
async def update_check(force: bool = False):
    """새 버전이 있나 — 레지스트리(배포 기계) 또는 git 원격 태그(소스 기계). 배지만."""
    from app.services import version

    return await asyncio.to_thread(version.check_update, force)


class UpdateRequest(BaseModel):
    version: str


def _unitd_update(version_str: str, stage: str) -> dict:
    from app.services import units, version as V
    from piper_bus import contract as C

    if not V.VERSION_RE.match(version_str):
        raise HTTPException(400, f"버전 모양이 아닙니다: {version_str}")
    if not units.unitd_available():
        raise HTTPException(400, "서비스 관리 데몬(piper-unitd)이 없습니다 — 업데이트는 호스트의 unitd 가 실행합니다")
    try:
        return units._bus().rpc_call(C.UNITD, "update", [version_str, stage, V.update_mode()], timeout=30)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@router.post("/update/pull")
async def update_pull(body: UpdateRequest):
    """[받기] — 이미지를 받고 호스트 코드를 꺼낸다. **아무것도 실행하지 않는다.**"""
    return await asyncio.to_thread(_unitd_update, body.version, "pull")


@router.post("/update/apply")
async def update_apply(body: UpdateRequest):
    """[적용] / [이전 버전으로] — 받아 둔 버전의 apply.sh (소스 기계는 update-source.sh).

    ⚠ **활동 중이면 막는다** — 마지막 단계가 게이트웨이 컨테이너와 데몬을 갈아치운다.
    돌고 있는 녹화·추론·학습·수동 조작이 그대로 깨진다. 이 응답 뒤 곧 연결이 끊긴다.
    """
    from app.services import exclusivity as ex

    busy = ex.running()
    if busy:
        labels = ", ".join(ex.LABELS.get(a, a.value) for a in busy)
        raise HTTPException(409, f"{labels} 중에는 업데이트할 수 없습니다 — 끝내고 다시 누르세요")
    return await asyncio.to_thread(_unitd_update, body.version, "apply")


@router.get("/update/status")
async def update_status():
    from app.services import units
    from piper_bus import contract as C

    if not units.unitd_available():
        return {"active": False, "finished": False, "log": "", "need_sudo": [], "unitd": False}
    try:
        st = await asyncio.to_thread(units._bus().rpc_call, C.UNITD, "update_status", [], 10)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {**st, "unitd": True}


@router.get("/update/notes")
async def update_notes(version: str):
    """받아 둔 번들의 CHANGELOG 에서 그 버전의 절 — 받기 전엔 빈 문자열."""
    from app.services import units, version as V
    from piper_bus import contract as C

    if not V.VERSION_RE.match(version):
        raise HTTPException(400, f"버전 모양이 아닙니다: {version}")
    if not units.unitd_available():
        return {"version": version, "notes": ""}
    try:
        notes = await asyncio.to_thread(units._bus().rpc_call, C.UNITD, "notes", [version], 10)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"version": version, "notes": notes or ""}


@router.get("/log-units")
async def system_log_units():
    """로그 화면이 고를 수 있는 유닛. **목록은 계약이 갖는다** — 화면이 배열을
    손으로 들면 카탈로그에 데몬을 더했을 때 조용히 갈라진다."""
    from piper_bus import contract as C

    return {"units": C.log_unit_options()}


@router.get("/logs")
async def system_logs(unit: str = "all", lines: int = 300, level: str = "info",
                      since: str | None = None, format: str = "json"):
    """데몬 저널 (설정 → 로그). `level` 은 error|warning|info — 데몬은 stdout 이라 journald
    우선순위가 전부 info 여서 메시지의 `[ERROR]` 토큰으로 가른다. `format=text` 는 내려받기."""
    from fastapi.responses import PlainTextResponse

    from app.services import syslog

    if level not in ("error", "warning", "info", "debug"):
        raise HTTPException(400, f"모르는 레벨입니다: {level}")
    try:
        out = await asyncio.to_thread(syslog.fetch, unit, max(1, min(lines, 5000)), level, since or None)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    if format == "text":
        name = f"piper-{unit}-{level}.log"
        return PlainTextResponse(syslog.as_text(out["entries"]),
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return out


class RestartRequest(BaseModel):
    name: str


@router.post("/services/restart")
async def restart_service(body: RestartRequest):
    """유닛 재시작.

    ⚠ **추론·녹화 중에는 막는다.** rsd 를 재시작하면 카메라 스트림이 끊기고,
    robotd 면 팔 상태 발행이 멈춘다 — 돌고 있는 에피소드가 그대로 깨진다.
    """
    require_idle(Activity.CAMERA_ACCESS)
    ok, msg = units.restart_unit(body.name)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "restarted", "name": body.name}


class ControlRequest(BaseModel):
    name: str
    action: str     # start | stop | restart | enable | disable


@router.post("/services/control")
async def control_service(body: ControlRequest):
    """켜기/끄기/재시작/부팅 시 시작 (feature/services.md).

    ⚠ **끄기·재시작은 활동 중에 막는다** — robotd 를 끄면 팔 상태 발행이 멈추고
    rsd 를 끄면 카메라가 끊겨 돌고 있는 에피소드가 깨진다. 켜기와 "부팅 시 시작"은
    지금 도는 것에 영향이 없어 막지 않는다. estopd 는 unitd 가 거절한다.
    """
    if body.action not in ("start", "stop", "restart", "enable", "disable"):
        raise HTTPException(400, f"모르는 동작입니다: {body.action}")
    if body.action in ("stop", "restart"):
        require_idle(Activity.CAMERA_ACCESS)
    ok, msg = await asyncio.to_thread(units.control_unit, body.name, body.action)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": body.action, "name": body.name}


@router.post("/restart")
async def restart_gateway():
    """게이트웨이(이 프로세스)를 다시 띄운다.

    ⚠ **응답을 보낸 뒤에** 자신을 갈아탄다. 먼저 죽으면 브라우저는 "요청 실패"만
    보고, 재시작이 된 건지 터진 건지 알 수 없다.

    ⚠ 감독자가 없으면 되살려 줄 사람도 없다 — 그래서 `execv` 로 **같은 명령줄을
    그대로** 다시 실행한다. 새로 뜨는 것이 아니라 이 프로세스가 갈아입는 것이다.
    """
    require_idle(Activity.CAMERA_ACCESS)
    argv = units.respawn_argv()
    if not argv:
        raise HTTPException(400, "이 방식으로는 스스로 재시작할 수 없습니다")

    async def _respawn():
        # 응답이 소켓을 빠져나갈 틈을 준다. 짧게 — 사용자는 기다리고 있다.
        await asyncio.sleep(0.5)
        logger.warning("게이트웨이 재시작: execv %s", argv)
        try:
            os.execv(argv[0], argv)
        except Exception as exc:               # pragma: no cover - 되돌아올 수 없다
            logger.error("재시작 실패 — 이 프로세스는 그대로 돕니다: %s", exc)

    asyncio.create_task(_respawn())
    return {"status": "restarting", "pid": os.getpid()}
