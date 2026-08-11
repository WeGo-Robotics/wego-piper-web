"""라우터 등록 (refactor/07-router-registration.md).

이전에는 라우터를 추가할 때 `main.py` 의 import 줄과 `include_router()` 줄을
**둘 다** 고쳐야 했고, 등록을 빠뜨리면 라우트가 조용히 404 가 됐다.
"""

import pkgutil

from app import routers as routers_pkg
from app.main import ROUTERS, app


def test_every_router_module_is_registered():
    """`app/routers/` 에 파일을 추가하고 `ROUTERS` 에 안 넣으면 조용히 404 가 된다."""
    on_disk = {
        m.name for m in pkgutil.iter_modules(routers_pkg.__path__)
        if not m.name.startswith("_")
    }
    registered = {m.__name__.rsplit(".", 1)[-1] for m in ROUTERS}
    missing = on_disk - registered
    assert not missing, f"ROUTERS 에 없는 라우터 모듈: {missing} → 라우트가 404 가 된다"


def test_no_duplicate_registration():
    assert len(ROUTERS) == len({m.__name__ for m in ROUTERS})


def test_every_entry_has_a_router_attribute():
    for m in ROUTERS:
        assert hasattr(m, "router"), f"{m.__name__} 에 router 가 없다"


def test_prefixes_are_unique():
    """접두사가 겹치면 등록 순서가 경로 매칭에 영향을 준다."""
    prefixes = [m.router.prefix for m in ROUTERS if m.router.prefix]
    assert len(prefixes) == len(set(prefixes)), f"중복 접두사: {prefixes}"


def test_app_actually_serves_them():
    paths = {r.path for r in app.routes}
    for expected in ("/health", "/ws", "/api/activity", "/api/policies", "/api/presets/{domain}"):
        assert expected in paths, f"{expected} 가 등록되지 않았다"
