"""게이트웨이 로그인 — **LAN 의 누구나가 아니라, 아는 사람만.**

이 파일이 지키는 것은 둘이고, 둘이 서로 반대 방향이다:

1. 비밀번호를 정하면 `/api/*` 가 **정말로** 막힌다 (WebSocket 포함).
2. 그래도 **E-stop 은 절대 안 막힌다.** 여기가 틀리면 인증을 붙인 대가로 돌던 추론이
   죽는다 — heartbeat 가 401 이면 estopd 는 "브라우저가 죽었다" 로 읽고 2.5초 뒤
   SIGKILL 한다(실측 전례).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth

PW = "pin1234"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    auth._cache_clear()
    auth._fails.clear()
    yield
    auth._cache_clear()
    auth._fails.clear()


@pytest.fixture
def c():
    return TestClient(app)


def _login(c, pw=PW):
    return c.post("/api/auth/login", json={"password": pw})


# ─────────────────────────────────────────────────────────────────────────────
# 켜기 전에는 예전 그대로다
#
# ⚠ "기본 잠김" 으로 만들면 릴리스를 받는 순간 배포된 로봇이 전부 잠긴다. 현장에서
#   화면이 안 열리는 것은 안전 기능이 아니라 사고다.
# ─────────────────────────────────────────────────────────────────────────────

def test_without_a_password_nothing_is_blocked(c):
    assert c.get("/api/auth/status").json()["enabled"] is False
    assert c.get("/api/models").status_code == 200


def test_the_screen_can_tell_that_it_is_wide_open(c):
    """⚠ 모르고 열려 있는 것이 제일 나쁘다 — 화면이 경고할 수 있어야 한다."""
    d = c.get("/api/auth/status").json()
    assert d["enabled"] is False and d["authenticated"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 켜면 정말 막힌다
# ─────────────────────────────────────────────────────────────────────────────

def test_setting_a_password_closes_the_door(c):
    auth.set_password(PW)
    assert TestClient(app).get("/api/models").status_code == 401


def test_logging_in_opens_it(c):
    auth.set_password(PW)
    assert _login(c).status_code == 200
    assert c.get("/api/models").status_code == 200, "로그인했는데 막힌다"


def test_a_wrong_password_is_refused(c):
    auth.set_password(PW)
    assert _login(c, "틀린비번").status_code == 401
    assert c.get("/api/models").status_code == 401


def test_a_forged_cookie_does_not_pass(c):
    auth.set_password(PW)
    c.cookies.set(auth.COOKIE, "eyJleHAiOjk5OTk5OTk5OTl9.xxxx")
    assert c.get("/api/models").status_code == 401


def test_the_log_stream_is_behind_the_door_too(c):
    """⚠ `/ws` 는 학습 로그와 프로세스 상태를 흘린다. 여기가 열려 있으면 막은 의미가
    반쯤 사라진다 — `BaseHTTPMiddleware` 로 짰으면 이걸 못 본다."""
    from starlette.websockets import WebSocketDisconnect

    auth.set_password(PW)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/ws"):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# ⚠⚠ 그래도 멈추는 길은 절대 안 막는다
# ─────────────────────────────────────────────────────────────────────────────

def test_estop_is_never_behind_the_door(c):
    """⚠ **이 테스트가 이 기능에서 제일 중요하다.** 멈추는 것을 막을 이유가 없고,
    heartbeat 가 401 이 되면 estopd 가 돌던 추론을 SIGKILL 한다."""
    auth.set_password(PW)
    anon = TestClient(app)
    for path in ("/api/estop/heartbeat", "/api/estop/trigger"):
        assert anon.post(path).status_code != 401, f"{path} 가 막혔다 — 추론이 죽는다"
    assert anon.get("/api/estop/status").status_code != 401


def test_health_stays_open_because_compose_watches_it(c):
    auth.set_password(PW)
    assert TestClient(app).get("/health").status_code == 200


def test_the_external_api_keeps_its_own_token(c):
    """⚠ `/api/ext/v1` 은 Bearer 가 따로 있다. 세션까지 요구하면 외부 시스템이 통째로
    끊긴다 — 로그인 화면이 없는 클라이언트다."""
    auth.set_password(PW)
    r = TestClient(app).get("/api/ext/v1/state")
    # 막히더라도 **관문이 막은 것이면 안 된다** — 외부 API 는 자기 사유로 답해야 한다.
    assert "로그인이 필요합니다" not in r.text, "관문이 외부 API 까지 삼켰다"


# ─────────────────────────────────────────────────────────────────────────────
# 비밀번호를 다루는 규칙
# ─────────────────────────────────────────────────────────────────────────────

def test_changing_the_password_kills_every_other_session(c):
    """⚠ 바꾸는 이유는 보통 "남이 알아버렸다" 다. 돌아다니던 세션이 살아 있으면 바꾼
    의미가 없다."""
    auth.set_password(PW)
    other = TestClient(app)
    _login(other)
    assert other.get("/api/models").status_code == 200

    _login(c)
    assert c.put("/api/auth/password",
                 json={"current": PW, "new": "next5678"}).status_code == 200
    assert other.get("/api/models").status_code == 401, "옛 세션이 살아 있다"
    assert c.get("/api/models").status_code == 200, "바꾼 사람까지 튕겼다"


def test_you_cannot_change_it_without_knowing_it(c):
    """⚠ 세션만으로 바꾸게 두면, 자리를 비운 사이 열린 화면 앞에 앉은 사람이 **주인을
    갈아치운다.**"""
    auth.set_password(PW)
    _login(c)
    r = c.put("/api/auth/password", json={"current": "몰라", "new": "next5678"})
    assert r.status_code == 401
    assert auth.verify(PW), "비밀번호가 바뀌어 버렸다"


def test_turning_it_off_also_needs_the_password(c):
    """⚠ 끄면 LAN 의 누구나 다시 들어온다 — 로그인만으로 끄게 두면 안 된다."""
    auth.set_password(PW)
    _login(c)
    assert c.request("DELETE", "/api/auth/password",
                     json={"current": "몰라"}).status_code == 401
    assert auth.enabled() is True
    assert c.request("DELETE", "/api/auth/password",
                     json={"current": PW}).status_code == 200
    assert auth.enabled() is False


def test_a_short_password_is_refused(c):
    r = c.put("/api/auth/password", json={"current": "", "new": "1"})
    assert r.status_code == 400 and str(auth.MIN_LEN) in r.json()["detail"]


def test_the_password_never_comes_back_out(c):
    """⚠ 응답에 섞이면 그 화면이 곧 유출 경로다 — Vast API 키와 같은 규칙(§12-22)."""
    auth.set_password(PW)
    _login(c)
    for r in (c.get("/api/auth/status"), _login(c)):
        assert PW not in r.text and "hash" not in r.text and "salt" not in r.text


def test_guessing_gets_slower(c):
    """⚠ PIN 을 쓸 수 있게 짧은 비밀번호를 허용했으므로, **대신** 무차별 대입을 막는다.
    둘 중 하나만 있으면 안 된다."""
    auth.set_password(PW)
    for _ in range(auth.FAIL_GRACE + 1):
        _login(c, "틀림")
    r = _login(c, "틀림")
    assert r.status_code == 429, "몇 번을 틀려도 속도가 그대로다"
    assert "초 뒤에" in r.json()["detail"], "얼마나 기다려야 하는지 안 말한다"


def test_the_right_password_clears_the_penalty(c):
    """⚠ 벌칙이 안 풀리면, 오타 몇 번 낸 사람이 맞는 비밀번호로도 못 들어온다."""
    auth.set_password(PW)
    for _ in range(auth.FAIL_GRACE):
        _login(c, "틀림")
    assert _login(c).status_code == 200
    assert auth.throttle_s("testclient") == 0.0


def test_a_broken_config_locks_rather_than_opens(c):
    """⚠ 깨진 파일을 "비밀번호 없음" 으로 읽으면 **인증이 조용히 꺼진다.** 사람은 켜져
    있다고 믿는다 — 잠기는 쪽이 낫고, 화면이 사유를 말한다."""
    auth.set_password(PW)
    auth._file().write_text("{깨졌다")
    auth._cache_clear()
    assert auth.enabled() is True and auth.broken() is True
    assert TestClient(app).get("/api/models").status_code == 401
    assert c.get("/api/auth/status").json()["broken"] is True
