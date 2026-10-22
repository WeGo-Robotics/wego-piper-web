"""게이트웨이 REST 클라이언트 — 표준 라이브러리만 쓴다 (의존을 안 늘린다).

토큰은 게이트웨이가 준 **범위 토큰**이다(feature/plugins.md §5). 범위 밖은 403 으로
돌아오고, 그 본문의 `detail` 이 어느 범위를 더해야 하는지 말한다. 예외를 던지지 않고
`Response` 로 돌려준다 — 플러그인 루프가 상태 코드로 분기하는 쪽이 자연스럽다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class ApiUnavailable(RuntimeError):
    """게이트웨이 주소가 없다 — 터미널에서 `--api` 없이 돌리고 있다."""


@dataclass
class Response:
    status: int
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self, default=None):
        if not self.body:
            return default
        try:
            return json.loads(self.body)
        except ValueError:
            return default

    @property
    def detail(self) -> str:
        d = self.json() or {}
        return str(d.get("detail", "")) if isinstance(d, dict) else ""


class Api:
    def __init__(self, base: str, token: str = "", timeout_s: float = 10.0) -> None:
        self.base = (base or "").rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self.base)

    def request(self, method: str, path: str, json_body=None, timeout_s: float | None = None) -> Response:
        if not self.base:
            raise ApiUnavailable("게이트웨이 주소가 없다 — `python -m piper_plugin run --api http://...` 로 주거나 "
                                 "게이트웨이에서 시작하라")
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as r:
                return Response(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read() or b"")

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, json=None, **kw) -> Response:
        return self.request("POST", path, json_body=json, **kw)

    def put(self, path: str, json=None, **kw) -> Response:
        return self.request("PUT", path, json_body=json, **kw)

    def delete(self, path: str, **kw) -> Response:
        return self.request("DELETE", path, **kw)
