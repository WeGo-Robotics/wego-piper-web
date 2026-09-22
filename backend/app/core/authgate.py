"""로그인 관문 — 세션 쿠키가 없으면 통과시키지 않는다.

## ⚠ `BaseHTTPMiddleware` 를 쓰지 않는다

두 가지가 안 된다:

1. **WebSocket 을 아예 못 본다.** `/ws` 는 학습 로그와 프로세스 상태를 흘리는데, 그게
   인증 밖에 남으면 막은 의미가 반쯤 사라진다.
2. **끝나지 않는 스트림과 안 맞는다.** MJPEG 프리뷰(`/api/cameras/*/preview`)는 응답이
   끝나지 않는다 — 응답을 한 번 감싸는 미들웨어는 이런 것에서 탈이 난다.

그래서 순수 ASGI 로 짠다. `scope` 만 보고 결정하고, 통과시킬 때는 **아무것도 감싸지
않는다** — 통과가 공짜여야 프리뷰가 느려지지 않는다.

## ⚠ 열어 두는 것들

| 경로 | 왜 |
|---|---|
| `/api/estop/*` | **멈추는 것을 막을 이유가 없다.** 그리고 heartbeat 가 401 이면 estopd 가 "브라우저가 죽었다" 로 읽어 추론을 SIGKILL 한다 — 인증을 붙이려다 추론을 끊는 셈이다 |
| `/api/auth/*` | 로그인 자체 |
| `/health` | 헬스체크는 컴포즈·감시가 본다 |
| `/api/ext/v1/*` | 자기 Bearer 토큰이 이미 있다(`external.py`) |
| `OPTIONS` | CORS 프리플라이트는 본 요청이 아니다 |
"""

from __future__ import annotations

import json

from app.services.auth import COOKIE

#: ⚠ 접두사 비교라 **끝의 `/` 가 중요하다.** `/api/estop` 으로 두면 `/api/estopX` 같은
#: 것이 같이 열린다. 지금 그런 라우터는 없지만, 여는 규칙은 좁을수록 좋다.
OPEN_PREFIXES = (
    "/health",
    "/api/estop/",
    "/api/ext/v1/",
    "/api/auth/",
)

DENY = {"detail": "로그인이 필요합니다"}


def _open(scope) -> bool:
    if scope.get("method") == "OPTIONS":
        return True
    path = scope.get("path", "")
    return any(path.startswith(p) for p in OPEN_PREFIXES)


def _cookie(scope) -> str | None:
    """세션 쿠키. 헤더를 직접 뒤진다 — `Request` 를 만들면 본문 수신까지 얽힌다."""
    for k, v in scope.get("headers", []):
        if k != b"cookie":
            continue
        for part in v.decode("latin-1").split(";"):
            name, _, val = part.strip().partition("=")
            if name == COOKIE:
                return val
    return None


async def _deny(scope, send) -> None:
    if scope["type"] == "websocket":
        # ⚠ accept 하지 않고 닫는다. 1008 = policy violation.
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps(DENY).encode()
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class AuthGate:
    """세션이 없으면 401(HTTP) · 1008(WS). 비밀번호가 없으면 **아무것도 안 한다.**"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or _open(scope):
            await self.app(scope, receive, send)
            return
        from app.services import auth

        if not auth.enabled() or auth.check(_cookie(scope)):
            await self.app(scope, receive, send)
            return
        await _deny(scope, send)
