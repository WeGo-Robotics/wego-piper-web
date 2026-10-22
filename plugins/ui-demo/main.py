#!/usr/bin/env python3
"""ui-demo — `ui: server` 의 실례. 자기 HTTP 서버(FastAPI)를 게이트웨이가 준 소켓에 띄운다.

- `GET /` : 상태 KV 를 그리는 페이지. 링크는 `X-Forwarded-Prefix` 를 앞에 붙인다
- `GET /api/state` : 플러그인 자신의 상태 JSON
- `WS  /ws` : 1초마다 상태를 밀어 준다 — 프록시가 WebSocket 을 넘기는지의 실측

    piper-plugin run plugins/ui-demo --api http://127.0.0.1:8000     → http://127.0.0.1:8765/
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from piper_plugin import Plugin
from piper_plugin.ui import forwarded_prefix

PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><title>ui-demo</title>
<body style="font-family:system-ui;background:#171717;color:#e5e5e5;margin:0">
<header style="display:flex;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid #333">
<h1 style="font-size:1.1rem;margin:0">자기 화면 데모 (서버)</h1><span id="s" style="color:#a3a3a3;font-size:.8rem">…</span>
<button onclick="fetch('/api/estop/trigger',{method:'POST'})" style="margin-left:auto;background:#b91c1c;color:#fff;border:0;border-radius:6px;padding:8px 14px;font-weight:700">E-STOP</button>
</header>
<pre id="kv" style="padding:16px;font-size:.9rem"></pre>
<script>
const prefix = {prefix!r}
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + prefix + '/ws')
ws.onmessage = (e) => { document.getElementById('kv').textContent = e.data; document.getElementById('s').textContent = 'WS 연결됨' }
ws.onclose = () => { document.getElementById('s').textContent = 'WS 끊김' }
</script></body></html>"""


class UiDemo(Plugin):
    def setup(self):
        self.ticks = 0
        app = FastAPI()
        plugin = self

        @app.get("/", response_class=HTMLResponse)
        async def index(request: Request):
            return PAGE.replace("{prefix!r}", json.dumps(forwarded_prefix(request.scope)))

        @app.get("/api/state")
        async def state():
            return {"ticks": plugin.ticks, "ts": time.time()}

        @app.websocket("/ws")
        async def ws(sock: WebSocket):
            await sock.accept()
            try:
                while True:
                    await sock.send_text(json.dumps({"ticks": plugin.ticks, "ts": time.time()}, ensure_ascii=False))
                    await asyncio.sleep(1)
            except WebSocketDisconnect:
                pass

        self.serve_ui(app)

    def loop(self):
        self.ticks += 1
        self.state(ticks=self.ticks)
        self.sleep(float(self.config["interval_s"]))


if __name__ == "__main__":
    raise SystemExit(UiDemo.main())
