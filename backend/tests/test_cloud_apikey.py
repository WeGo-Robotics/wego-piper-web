"""Vast API 키 — **넣는 길은 있고, 나오는 길은 없다** (feature/vast-training.md §9-1).

⚠ 이 파일이 지키는 것은 **비밀이 화면으로 안 나가는 것**이다. 게이트웨이는
`/api/ext/v1` 말고는 인증이 없어서, LAN 에서 이 포트에 닿는 누구나 응답을 읽는다 —
한 번 돌려주는 순간 그 응답이 곧 유출 경로다.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.cloud import apikey

SECRET = "abcd1234efgh5678ijkl"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    yield


def test_the_key_never_comes_back_out():
    """⚠ 응답에 키가 있으면 그 화면이 곧 유출 경로다. 끝 네 자리까지만."""
    apikey.save(SECRET)
    r = TestClient(app).get("/api/cloud/credentials")
    assert SECRET not in r.text, "키가 응답에 섞였다"
    d = r.json()
    assert d["configured"] is True and d["tail"] == SECRET[-4:]


def test_it_is_saved_where_a_reinstall_cannot_wipe_it():
    """⚠ 이미지 안에 두면 릴리스마다 사라지고, 무엇보다 **이미지에 비밀이 실린다.**
    SSH 키와 같은 데이터 볼륨(`config_dir`)에 둔다."""
    from app.core.config import settings

    apikey.save(SECRET)
    assert apikey.path().is_relative_to(settings.config_dir)


def test_the_file_is_not_readable_by_others():
    apikey.save(SECRET)
    assert apikey.path().stat().st_mode & 0o077 == 0, "그룹·타인이 읽을 수 있다"


def test_a_key_is_tried_before_it_is_saved(monkeypatch):
    """⚠ 검증 없이 저장하면 "설정됨" 이라 표시되는데 아무것도 안 되는 상태가 된다 —
    제일 헷갈리는 실패다."""
    from app.services.cloud.providers import vast

    def _refuse(self):
        raise RuntimeError("Invalid user key")

    monkeypatch.setattr(vast.VastProvider, "whoami", _refuse)
    r = TestClient(app).put("/api/cloud/credentials", json={"api_key": SECRET})
    assert r.status_code == 400, "틀린 키를 받아 줬다"
    assert not apikey.path().exists(), "검증에 실패했는데 저장했다"


def test_a_bad_key_is_a_400_not_a_502(monkeypatch):
    """⚠ 502 는 "게이트웨이가 고장" 이라는 뜻이라 사람이 엉뚱한 곳을 본다 — 실제로는
    붙여넣기를 다시 해야 하는 상황이다."""
    from app.services.cloud.providers import vast

    monkeypatch.setattr(vast.VastProvider, "whoami",
                        lambda self: (_ for _ in ()).throw(RuntimeError("Invalid user key")))
    r = TestClient(app).put("/api/cloud/credentials", json={"api_key": "x" * 20})
    assert r.status_code == 400
    assert "확인" in r.json()["detail"]
    assert "x" * 20 not in r.text, "틀린 키를 그대로 되돌려줬다"


def test_saving_a_good_key_stores_it_and_still_hides_it(monkeypatch):
    from app.services.cloud.providers import vast

    monkeypatch.setattr(vast.VastProvider, "whoami", lambda self: {"id": 1, "credit": 25.0})
    r = TestClient(app).put("/api/cloud/credentials", json={"api_key": SECRET})
    assert r.status_code == 200
    assert SECRET not in r.text
    assert r.json()["credit"] == 25.0, "맞게 들어갔는지 볼 근거가 없다"
    assert apikey.load() == SECRET


def test_the_deploy_key_wins_and_cannot_be_deleted_here(monkeypatch):
    """⚠ `.env` 의 키는 운영자가 배포 수단으로 심은 값이다. 화면이 그걸 덮거나 지우면
    운영자는 자기 키가 왜 안 먹는지 알 길이 없다."""
    apikey.save(SECRET)
    monkeypatch.setenv("VAST_API_KEY", "from-deploy-9999")
    assert apikey.load() == "from-deploy-9999", "저장된 것이 배포 설정을 이긴다"

    r = TestClient(app).delete("/api/cloud/credentials")
    d = r.json()
    assert d["configured"] is True and d["source"] == "env"
    assert "환경변수" in d["detail"], "왜 아직 설정돼 있는지 말하지 않는다"


def test_deleting_removes_only_the_stored_file():
    apikey.save(SECRET)
    r = TestClient(app).delete("/api/cloud/credentials")
    assert r.json()["configured"] is False
    assert not apikey.path().exists()


def test_the_screen_does_not_prefill_the_key():
    """⚠ 저장된 키를 입력칸에 채워 넣으면, 그 화면을 여는 누구나 읽게 된다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src"
           / "components" / "CloudPanel.tsx").read_text()
    assert "useState('')" in src.split("apiKey")[1][:40], "입력칸을 빈 값으로 시작하지 않는다"
    assert 'type="password"' in src, "키가 평문으로 보인다"
    assert "setApiKey('')" in src, "저장한 뒤 입력칸에 키가 남는다"


def test_the_key_goes_to_the_cli_as_env_not_argv():
    """⚠ `ps` 로 남의 프로세스 인자를 읽을 수 있는 기계에서 argv 는 비밀을 두는 자리가
    아니다. 프로바이더는 키를 환경변수로 넘긴다."""
    import inspect

    from app.services.cloud.providers import vast

    src = inspect.getsource(vast.VastProvider._env)
    assert 'env["VAST_API_KEY"]' in src
    assert "--api-key" not in inspect.getsource(vast.VastProvider._raw)


def test_every_cli_call_carries_the_saved_key():
    """⚠ **실측(2026-09-18)**: 화면으로 키를 넣었는데 SSH 등록 확인이 계속
    `401 Invalid user key` 였다. 원인은 라우터가 `vastai` 를 **맨 환경으로** 띄운
    자리가 둘 있었다는 것 — `create ssh-key`(등록 버튼)와 `show ssh-keys`(확인).
    저장한 키는 프로바이더가 env 로 넘기므로, 그 두 곳엔 안 갔고 CLI 는 제 파일
    (낡은 키)을 봤다.

    ⚠ 등록 버튼까지 그랬다는 게 더 나쁘다 — 눌러도 그 계정에 안 갈 수 있었다.
    """
    import inspect

    from app.routers import cloud as router

    for fn in (router.register_ssh_key, router._ssh_registered):
        src = inspect.getsource(fn)
        assert "subprocess.run" in src, "이 검사가 낡았다 — 호출 방식이 바뀌었나"
        assert "env=" in src, f"{fn.__name__} 이 맨 환경으로 CLI 를 띄운다"


def test_the_provider_exposes_one_environment():
    """⚠ 환경 조립이 두 벌이면 갈린다 — 한 곳만 키를 싣는 상태가 된다."""
    from app.services.cloud.providers import vast

    p = vast.VastProvider("k")
    assert p.env()["VAST_API_KEY"] == "k"
    assert vast.VastProvider().env().get("VAST_API_KEY") is None
