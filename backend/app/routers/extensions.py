"""장치 확장 — 웹에서 이미지를 더하고 그 화면을 연다 (feature/pika.md §2).

| 경로 | 하는 일 |
|---|---|
| `GET /api/system/extensions` | 등록된 확장 + 살아 있나(버스 생존 키) + 화면이 있나 + 로그인 상태(경고용) |
| `POST /api/system/extensions` | 이미지 추가 — 받기는 몇 분 걸려 즉시 돌아온다. 진행은 `…/status` |
| `DELETE /api/system/extensions/{name}` | 끄고 지운다. `purge=true` 면 데이터(캘리브레이션·배정)도 — 활동 중엔 막는다 |
| `/ext/{name}/ui/…` | 확장 자기 화면 — `<ext_dir>/<name>/run/ui.sock` 으로 HTTP·WebSocket 프록시 |

손은 호스트 unitd 다(`daemons/unitd_ext.py`) — 이 컨테이너엔 docker 도 systemd 도 없다. 플러그인 토큰은 `*` 로도
여기 못 닿는다(`plugin_scopes.NEVER_PREFIXES`) — 장치 권한을 주는 일은 사람이 한다. 로그인이 꺼져 있으면 막지 않고
화면이 경고한다(사용자 결정 2026-11-24).
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pathlib import Path

from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.core.config import settings
from app.routers.plugin_ui import proxy_http, proxy_ws
from app.services import units
from app.services.exclusivity import Activity, require_idle

router = APIRouter(tags=["extensions"])

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
# 새 창에 그대로 나가는 HTML 이라 화면 번역이 안 닿는다 — 플러그인 안내 페이지와 같은 이유로 옆 파일
_OFFLINE_HTML = (Path(__file__).with_name("ext_ui_offline.html")).read_text(encoding="utf-8")


def _offline(name: str) -> HTMLResponse:
    return HTMLResponse(_OFFLINE_HTML.format(title=name), status_code=503)


def _rpc(method: str, args: list | None = None, timeout: int = 20):
    from piper_bus import contract as C

    if not units.unitd_available():
        raise HTTPException(503, "서비스 관리 데몬(piper-unitd)이 없습니다 — 장치 확장은 unitd 가 켭니다")
    try:
        return units._bus().rpc_call(C.UNITD, method, args or [], timeout=timeout)
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


def _sock(name: str):
    return settings.ext_dir / name / "run" / "ui.sock"


@router.get("/api/system/extensions")
async def list_extensions():
    from app.services import auth

    got = await asyncio.to_thread(_rpc, "ext_list")
    rows = []
    for e in got.get("extensions", []):
        hub = e["manifest"]["rpc_hub"]
        try:
            info = units._bus().daemon_info(hub)
        except Exception:
            info = None
        rows.append({**e, "alive": info is not None, "started": (info or {}).get("started"),
                     "ui_ready": bool(e["manifest"].get("ui")) and _sock(e["name"]).exists()})
    return {"extensions": rows, "registries": got.get("registries", []),
            "login_enabled": auth.enabled()}


class AddRequest(BaseModel):
    image: str


@router.post("/api/system/extensions")
async def add_extension(body: AddRequest):
    return await asyncio.to_thread(_rpc, "ext_add", [body.image.strip()])


@router.get("/api/system/extensions/status")
async def add_status():
    return await asyncio.to_thread(_rpc, "ext_status")


@router.delete("/api/system/extensions/{name}")
async def remove_extension(name: str, purge: bool = False):
    if not _NAME.match(name):
        raise HTTPException(400, f"확장 이름 모양이 아닙니다: {name}")
    # 확장이 장치(Sense)를 쥐고 있다 — 텔레옵·녹화가 그걸 읽는 중이면 에피소드가 깨진다
    require_idle(Activity.CAMERA_ACCESS)
    return await asyncio.to_thread(_rpc, "ext_remove", [name, purge], 120)


# ── 확장 화면 ────────────────────────────────────────────────────────────────

def _checked(name: str) -> str:
    if not _NAME.match(name):
        raise HTTPException(404)
    return name


@router.get("/ext/{name}/ui", include_in_schema=False)
async def ui_root_redirect(name: str):
    return RedirectResponse(f"/ext/{_checked(name)}/ui/", status_code=307)


# 화면 프록시는 API 가 아니다 — 스키마(/docs)에서 뺀다 (여러 메서드라 작업 ID 도 겹친다)
@router.api_route("/ext/{name}/ui/{rel:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                  include_in_schema=False)
async def ui(request: Request, name: str, rel: str = ""):
    name = _checked(name)
    return await proxy_http(request, _sock(name), rel, prefix=f"/ext/{name}/ui",
                            extra={"x-piper-extension": name}, offline=lambda: _offline(name))


@router.websocket("/ext/{name}/ui/{rel:path}")
async def ui_ws(ws: WebSocket, name: str, rel: str = ""):
    if not _NAME.match(name):
        await ws.close(code=1008)
        return
    await proxy_ws(ws, _sock(name), rel, prefix=f"/ext/{name}/ui", extra={"x-piper-extension": name},
                   label=f"ext {name}")
