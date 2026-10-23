"""플러그인 API — 목록·시작·정지·설정·설치 (feature/plugins.md §8-1, §9 1단계).

화면은 아직 없다(4단계). 지금은 `curl` 과 외부 UI 가 부른다.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import plugin_scopes
from app.services.plugins import registry

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class StartRequest(BaseModel):
    config: dict = {}
    cameras: dict[str, str] = {}
    inputs: dict[str, str] = {}


class ConfigRequest(BaseModel):
    config: dict = {}
    cameras: dict[str, str] = {}
    inputs: dict[str, str] = {}


class InstallRequest(BaseModel):
    path: str


class CommandRequest(BaseModel):
    name: str
    args: dict = {}


@router.get("")
async def list_plugins():
    out = []
    for info in registry.scan():
        d = info.to_dict()
        d.update(registry.status(info.name))
        d["saved"] = registry.saved_config(info.name)
        out.append(d)
    return {"plugins": out, "dirs": [str(p) for p, _ in registry.dirs()]}


@router.get("/scopes")
async def list_scopes():
    """권한 범위 표 — 화면의 배지와 매니페스트 작성자가 본다."""
    return {"scopes": {k: [list(r) for r in v] for k, v in plugin_scopes.SCOPES.items()},
            "all": plugin_scopes.ALL, "never": list(plugin_scopes.NEVER_PREFIXES)}


@router.get("/topics")
async def list_sources():
    """살아 있는 검출 이름과 토픽 — 입력 슬롯 선택기가 고르는 것들 (feature/plugins.md §7-3)."""
    return registry.sources()


@router.get("/topics/{topic}")
async def get_topic(topic: str):
    from piper_bus.client import Bus

    tp = Bus().get_topic(topic)
    if tp is None:
        raise HTTPException(404, "그런 토픽이 없거나 낡았습니다")
    return tp


@router.get("/runs")
async def all_runs():
    """모든 플러그인의 저널 목록(최신 먼저) — 미션 이력."""
    return {"runs": registry.runs()}


@router.get("/{name}/runs")
async def plugin_runs(name: str):
    registry.get(name)
    return {"runs": registry.runs(name)}


@router.get("/{name}/runs/{run_id}")
async def plugin_run(name: str, run_id: str):
    registry.get(name)
    found = registry.run_events(run_id, name)
    if found is None:
        raise HTTPException(404, "그런 저널이 없습니다")
    return {"id": run_id, "plugin": name, "events": found[1]}


@router.get("/{name}")
async def get_plugin(name: str):
    info = registry.get(name)
    d = info.to_dict()
    d.update(registry.status(name))
    d["saved"] = registry.saved_config(name)
    return d


@router.post("/{name}/start")
async def start_plugin(name: str, body: StartRequest):
    return await registry.start(name, body.config, body.cameras, body.inputs)


@router.post("/{name}/stop")
async def stop_plugin(name: str):
    return await registry.stop(name)


@router.post("/{name}/command")
async def send_command(name: str, body: CommandRequest):
    """매니페스트 `commands` 의 버튼 — 플러그인 `on_command(name, args)` 로 간다."""
    registry.command(name, body.name, body.args)
    return {"status": "queued", "name": body.name}


@router.get("/{name}/deps")
async def plugin_deps(name: str):
    """의존 상태 (`python: venv | venv-isolated`) — venv 가 준비됐나, 설치 중인가, 로그 꼬리."""
    return registry.deps(name)


@router.post("/{name}/deps")
async def install_deps(name: str):
    """venv 를 만들고 requirements.txt 를 깐다 — 별도 프로세스, 로그는 플러그인 로그 채널(WS)로."""
    return await registry.install_deps(name)


@router.delete("/{name}/deps")
async def remove_deps(name: str):
    registry.remove_deps(name)
    return {"status": "removed"}


@router.put("/{name}/config")
async def save_config(name: str, body: ConfigRequest):
    registry.get(name)
    registry.save_config(name, body.config, body.cameras, body.inputs)
    return {"status": "ok"}


@router.post("/install")
async def install_plugin(body: InstallRequest):
    """로컬 폴더에서 설치 (zip 업로드는 4단계). git URL 은 받지 않는다 (§3-2)."""
    info = registry.install_from_path(Path(body.path))
    if info.manifest is None:
        raise HTTPException(400, info.error or "매니페스트 오류")
    return info.to_dict()


@router.post("/install/zip")
async def install_zip(request: Request):
    """zip 업로드(raw body) 설치 — 화면의 파일 선택이 여기로 온다. git URL 은 받지 않는다 (§3-2)."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "본문이 비어 있습니다")
    info = registry.install_from_zip(data)
    return info.to_dict()


@router.delete("/{name}")
async def remove_plugin(name: str):
    registry.remove(name)
    return {"status": "removed", "name": name}
