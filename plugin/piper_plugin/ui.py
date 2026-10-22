"""플러그인 자기 화면 — `ui: server` (feature/plugins.md §8-2).

게이트웨이가 `PIPER_PLUGIN_UI_SOCKET` 으로 유닉스 소켓 경로를 준다. 플러그인은 거기에 HTTP 서버를
띄우고, 게이트웨이가 `/plugins/<name>/ui/` 로 프록시한다(HTTP·WebSocket). **포트를 고르지 않는다** —
밖에서 닿는 포트는 게이트웨이 하나다. 터미널(`piper-plugin run`)에서는 소켓이 없으므로
`127.0.0.1:8765` 에 띄워 바로 본다.

ASGI 앱(FastAPI·Starlette 등)을 받는다. 다른 프레임워크(Streamlit 등)는 소켓 경로를 자기 방식으로
받아 띄우면 된다 — 프록시는 뒤에 무엇이 있는지 가리지 않는다.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEV_HOST, DEV_PORT = "127.0.0.1", 8765


def serve_ui(app, socket_path: str | None = None, *, block: bool = False) -> threading.Thread | None:
    """ASGI 앱을 소켓(또는 개발용 루프백 포트)에 띄운다. 기본은 **데몬 스레드** — 플러그인 루프는 계속 돈다."""
    import uvicorn

    socket_path = socket_path or os.environ.get("PIPER_PLUGIN_UI_SOCKET") or None
    if socket_path:
        p = Path(socket_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()                       # 지난 실행이 남긴 소켓 파일
        config = uvicorn.Config(app, uds=socket_path, log_level="warning", proxy_headers=True)
        where = socket_path
    else:
        config = uvicorn.Config(app, host=DEV_HOST, port=DEV_PORT, log_level="warning")
        where = f"http://{DEV_HOST}:{DEV_PORT}/"
    server = uvicorn.Server(config)
    logger.info("UI 서버: %s", where)
    if block:
        server.run()
        return None
    t = threading.Thread(target=server.run, name="plugin-ui", daemon=True)
    t.start()
    return t


def forwarded_prefix(scope_or_headers) -> str:
    """프록시가 붙인 `X-Forwarded-Prefix` — 앱이 링크를 만들 때 앞에 붙인다. 없으면 빈 문자열(개발 실행)."""
    headers = scope_or_headers.get("headers", []) if isinstance(scope_or_headers, dict) else scope_or_headers
    for k, v in headers:
        if (k.lower() if isinstance(k, str) else k.decode().lower()) == "x-forwarded-prefix":
            return v if isinstance(v, str) else v.decode()
    return ""
