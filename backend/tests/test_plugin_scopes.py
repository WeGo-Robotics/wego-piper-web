"""권한 범위 표가 **실제 라우트**를 가리키는가 (feature/plugins.md §5).

경로가 바뀌면 표가 조용히 빈 범위가 된다 — 플러그인이 403 을 받는데 이유를 모른다.
"""

from __future__ import annotations

import pytest

from app.core import plugin_scopes as S
from app.main import app


def _routes() -> set[tuple[str, str]]:
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        for m in methods:
            out.add((m, path))
    return out


@pytest.mark.parametrize("scope", sorted(S.SCOPES))
def test_every_rule_points_at_a_real_route(scope):
    routes = _routes()
    for method, path in S.SCOPES[scope]:
        if path.endswith("/"):
            ok = any(m == method and (p == path[:-1] or p.startswith(path)) for m, p in routes)
        else:
            ok = (method, path) in routes
        assert ok, f"{scope}: {method} {path} 라우트가 없다"


def test_star_allows_everything_but_auth():
    assert S.allows({"*"}, "POST", "/api/models/inference/start")
    assert S.allows({"*"}, "DELETE", "/api/plugins/x")
    assert not S.allows({"*"}, "PUT", "/api/auth/password")
    assert not S.allows({"*"}, "POST", "/api/auth/logout")


def test_prefix_and_exact_matching():
    s = {"vision.read", "inference.stop"}
    assert S.allows(s, "GET", "/api/vision/detections")
    assert S.allows(s, "GET", "/api/vision")             # 접두사는 자기 자신도 포함
    assert not S.allows(s, "POST", "/api/vision/start")   # 메서드가 다르다
    assert S.allows(s, "POST", "/api/models/inference/stop")
    assert not S.allows(s, "POST", "/api/models/inference/stop-all")   # 정확히만
    assert not S.allows(set(), "GET", "/api/activity")


def test_task_change_is_a_declared_scope():
    """오케스트레이터 이관(§12)의 필수 범위 — 판단을 언어 정책에 전달하는 길."""
    assert S.allows({"params.write"}, "POST", "/api/params")
    assert S.allows({"vision.judge"}, "POST", "/api/vision/judge")
    assert S.allows({"eval.write"}, "POST", "/api/eval/log")
