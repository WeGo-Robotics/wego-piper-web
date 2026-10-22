"""플러그인 실행 토큰이 관문을 지나는 방식 (feature/plugins.md §5, core/authgate.py).

- 모르는 토큰은 401, 범위 밖은 403, 범위 안은 통과
- 로그인이 **꺼져 있어도** 범위는 건다 — 권한 화면이 로그인 설정에 따라 거짓말하면 안 된다
- 로그인이 켜져 있으면 토큰은 쿠키 없이 지나간다 (플러그인 프로세스에는 쿠키가 없다)
- `/api/estop/*` 는 토큰이 어떻든 열려 있다
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth
from app.services.plugins import Grant, registry

PW = "pin1234"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    auth._cache_clear()
    auth._fails.clear()
    registry._grants.clear()
    yield
    registry._grants.clear()
    auth._cache_clear()
    auth._fails.clear()


@pytest.fixture
def c():
    return TestClient(app)


def _grant(token: str, *scopes: str) -> None:
    registry._grants[token] = Grant(name="t", scopes=frozenset(scopes))


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_unknown_token_is_401(c):
    assert c.get("/api/activity", headers=_h("nope")).status_code == 401


def test_scope_is_enforced_even_when_login_is_off(c):
    _grant("tok", "activity.read")
    assert c.get("/api/activity", headers=_h("tok")).status_code == 200
    r = c.get("/api/models", headers=_h("tok"))
    assert r.status_code == 403 and "permissions" in r.json()["detail"]


def test_star_never_reaches_auth(c):
    _grant("root", "*")
    assert c.get("/api/activity", headers=_h("root")).status_code == 200
    assert c.get("/api/auth/status", headers=_h("root")).status_code == 403
    assert c.put("/api/auth/password", json={"current": "", "new": PW}, headers=_h("root")).status_code == 403


def test_token_passes_the_login_gate_without_a_cookie(c):
    assert c.put("/api/auth/password", json={"current": "", "new": PW}).status_code == 200
    c.cookies.clear()
    assert c.get("/api/activity").status_code == 401            # 쿠키 없음
    _grant("tok", "activity.read")
    assert c.get("/api/activity", headers=_h("tok")).status_code == 200
    assert c.get("/api/models", headers=_h("tok")).status_code == 403


def test_estop_is_open_regardless_of_token(c):
    assert c.get("/api/estop/status", headers=_h("garbage")).status_code == 200
