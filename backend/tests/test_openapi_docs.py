"""Swagger UI 도달성 (`/docs`).

백엔드는 FastAPI 기본값대로 `/docs` 와 `/openapi.json` 을 서빙한다. 문제는 **앞단**이다:

- nginx 는 `location /` 의 SPA 폴백이 모든 미등록 경로를 index.html 로 돌린다.
  `/docs` 를 명시적으로 프록시하지 않으면 배포본에서 **빈 화면**이 나온다 —
  백엔드는 멀쩡하고 앞단만 가로챈 모양이라 원인이 안 보인다.
- vite 개발 서버도 같다. 프록시 목록에 없으면 :5173 에서 404.

그래서 앱 쪽 서빙과 **앞단 두 곳의 설정**을 함께 못 박는다.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO = Path(__file__).resolve().parents[2]
NGINX = REPO / "frontend" / "nginx.conf"
VITE = REPO / "frontend" / "vite.config.ts"


def test_openapi_schema_builds():
    """라우트 353개가 한 스키마로 직렬화되는지. 모델 충돌이 있으면 여기서 터진다."""
    schema = app.openapi()
    assert schema["info"]["title"] == "Piper Studio"
    assert json.dumps(schema)  # 직렬화 불가능한 스키마를 조기에 잡는다
    assert len(schema["paths"]) > 200


def test_docs_and_schema_are_served():
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        r = client.get("/docs")
        assert r.status_code == 200
        assert "swagger-ui" in r.text


def test_external_contract_is_visible_in_schema():
    """외부 계약이 스키마에 보여야 통합하는 쪽이 `/docs` 만 보고 붙을 수 있다."""
    paths = app.openapi()["paths"]
    assert "/api/ext/v1/missions" in paths
    assert "/api/ext/v1/heartbeat" in paths


def test_nginx_proxies_docs_paths():
    conf = NGINX.read_text()
    for path in ("/docs", "/redoc", "/openapi.json"):
        # 정확 일치라야 SPA 폴백보다 먼저 잡는다.
        assert f"location = {path} {{" in conf, f"nginx 가 {path} 를 프록시하지 않는다 → SPA 폴백이 먹는다"


def test_vite_proxies_docs_paths():
    conf = VITE.read_text()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert f"'{path}'" in conf, f"vite 개발 서버가 {path} 를 프록시하지 않는다"
