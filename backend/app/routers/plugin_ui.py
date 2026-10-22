"""플러그인 자기 화면 — `/plugins/{name}/ui/…` (feature/plugins.md §8-2).

| 매니페스트 `ui` | 여기서 하는 일 |
|---|---|
| `ui/` (폴더) | 폴더의 정적 파일을 서빙한다. 없는 경로는 `index.html` 로 폴백(SPA). 경로 조작은 404 |
| `server` | 플러그인이 띄운 유닉스 소켓으로 HTTP·WebSocket 을 프록시한다. 꺼져 있으면 안내 페이지 |
| `/detector` (저장소 경로) | 그 라우트로 보낸다 — 화면은 저장소 페이지다 |

로그인 관문은 `/api/` 와 똑같이 걸린다 — `authgate` 는 열어 둔 접두사가 아니면 전부 막는다.
밖에서 닿는 포트는 게이트웨이 하나다; 플러그인은 포트를 고르지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.services.plugins import registry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["plugins"])

# 프록시가 넘기지 않는 홉 단위 헤더
_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te", "trailer",
        "content-length"}

# 꺼진 플러그인의 안내 페이지 — 새 창에 **그대로** 나가는 HTML 이라 화면 번역(i18n)이 닿지 않는다.
# 그래서 파이썬 문자열이 아니라 옆 파일에 둔다(번역 도구가 키로 집지 않게).
_OFFLINE_HTML = (Path(__file__).with_name("plugin_ui_offline.html")).read_text(encoding="utf-8")


def _kind(name: str):
    info = registry.get(name)
    if info.manifest is None:
        raise HTTPException(400, f"매니페스트가 깨져 있습니다: {info.error}")
    ui = info.manifest.ui
    if not ui:
        raise HTTPException(404, "이 플러그인은 자기 화면이 없습니다 (plugin.yaml 의 ui)")
    if ui == "server":
        return "server", registry.ui_socket(name), info
    if isinstance(ui, str) and ui.startswith("/"):
        return "route", ui, info
    folder = (info.dir / str(ui)).resolve()
    if not folder.is_dir() or info.dir.resolve() not in folder.parents and folder != info.dir.resolve():
        raise HTTPException(404, f"정적 UI 폴더가 없습니다: {ui}")
    return "static", folder, info


def _static(folder: Path, rel: str) -> Response:
    target = (folder / rel).resolve() if rel else folder / "index.html"
    # ⚠ 경로 조작 — 폴더 밖이면 404. 사용자가 올린 zip 의 폴더다
    if folder not in target.parents and target != folder:
        raise HTTPException(404)
    if target.is_dir():
        target = target / "index.html"
    if target.is_file():
        return FileResponse(target)
    index = folder / "index.html"
    if index.is_file():
        return FileResponse(index)        # SPA 폴백
    raise HTTPException(404)


def _client(sock: Path) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sock)), base_url="http://plugin",
                             timeout=httpx.Timeout(30.0, read=None))


async def _proxy(request: Request, name: str, sock: Path, rel: str) -> Response:
    if not sock.exists():
        title = registry.get(name).manifest.title if registry.get(name).manifest else name
        return HTMLResponse(_OFFLINE_HTML.format(title=title, name=name), status_code=503)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers["x-forwarded-prefix"] = f"/plugins/{name}/ui"
    headers["x-piper-plugin"] = name
    headers["host"] = "plugin"
    url = "/" + rel
    if request.url.query:
        url += "?" + request.url.query
    # 본문은 통째로 읽어 넘긴다 — 스트리밍으로 넘기면 chunked 가 되어 Content-Length 만 읽는 단순한
    # 서버가 빈 본문을 받는다(테스트에서 났다). 플러그인 UI 의 업로드는 작다
    body = await request.body()
    client = _client(sock)
    try:
        req = client.build_request(request.method, url, headers=headers, content=body)
        resp = await client.send(req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        await client.aclose()
        title = registry.get(name).manifest.title if registry.get(name).manifest else name
        return HTMLResponse(_OFFLINE_HTML.format(title=title, name=name), status_code=503)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers,
                             media_type=resp.headers.get("content-type"))


@router.get("/plugins/{name}/ui")
async def ui_root_redirect(name: str):
    return RedirectResponse(f"/plugins/{name}/ui/", status_code=307)


@router.api_route("/plugins/{name}/ui/{rel:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def ui(request: Request, name: str, rel: str = ""):
    kind, target, _info = _kind(name)
    if kind == "route":
        return RedirectResponse(str(target), status_code=307)
    if kind == "static":
        return _static(target, rel)
    return await _proxy(request, name, target, rel)


@router.websocket("/plugins/{name}/ui/{rel:path}")
async def ui_ws(ws: WebSocket, name: str, rel: str = ""):
    """WebSocket 프록시 — Streamlit·Gradio 처럼 서버가 곧 UI 인 것들은 WS 가 필수다."""
    try:
        kind, target, _info = _kind(name)
    except HTTPException:
        await ws.close(code=1008)
        return
    if kind != "server" or not Path(target).exists():
        await ws.close(code=1013)        # try again later — 플러그인이 꺼져 있다
        return
    from websockets.asyncio.client import unix_connect

    uri = "ws://plugin/" + rel + (f"?{ws.url.query}" if ws.url.query else "")
    extra = [(k, v) for k, v in ws.headers.items()
             if k.lower() not in _HOP and not k.lower().startswith("sec-websocket")]
    extra += [("x-forwarded-prefix", f"/plugins/{name}/ui"), ("x-piper-plugin", name)]
    subprotocols = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    try:
        upstream = await unix_connect(str(target), uri=uri, additional_headers=extra,
                                      subprotocols=subprotocols or None, open_timeout=10)
    except Exception as e:
        logger.warning("플러그인 %s WS 연결 실패: %s", name, e)
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=upstream.subprotocol)

    async def down() -> None:
        async for msg in upstream:
            if isinstance(msg, (bytes, bytearray)):
                await ws.send_bytes(bytes(msg))
            else:
                await ws.send_text(msg)

    async def up() -> None:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                await upstream.send(m["bytes"])
            elif m.get("text") is not None:
                await upstream.send(m["text"])

    tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        try:
            await ws.close()
        except Exception:
            pass
