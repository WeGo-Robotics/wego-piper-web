"""게이트웨이 로그인 — **LAN 의 누구나가 아니라, 아는 사람만.**

## 왜 이제 와서

`/api/ext/v1` 말고는 인증이 아예 없었다. 같은 네트워크에서 `:8000` 에 닿는 누구나
데이터셋을 지우고, 팔을 움직이고, **Vast 키가 저장된 호스트에서는 GPU 를 빌려 돈을
쓸 수 있었다.**

## ⚠ 비밀번호를 안 정하면 인증은 **꺼진 채**다

"기본 잠김" 이 더 안전해 보이지만, 그러면 릴리스를 받는 순간 배포된 로봇이 전부
잠긴다 — 현장에서 화면이 안 열리는 것은 안전 기능이 아니라 사고다. 그래서 **비밀번호를
정해야 켜진다.** 대신 안 정한 상태는 화면이 계속 경고한다: 모르고 열려 있는 것이 제일
나쁘다.

⚠ 그래서 **처음 정하는 사람은 아무나일 수 있다.** 먼저 정한 사람이 주인이 된다. 사전
신뢰가 없는 LAN 장비의 부트스트랩은 원래 그렇고, 여기서 더 할 수 있는 것이 없다 —
설치 직후에 정하는 것이 답이다.

## ⚠ E-stop 은 어떤 경우에도 인증을 안 탄다

세션이 만료된 채 추론이 돌고 있으면 heartbeat 가 401 이 되고, estopd 는 그걸 "브라우저가
죽었다" 로 읽어 **2.5초 뒤 SIGKILL 한다**(실측 전례). 인증을 붙이려다 추론을 끊는 것이다.
그래서 `/api/estop/*` 는 미들웨어가 통째로 비켜 간다 — 멈추는 것을 막을 이유는 어차피
없다.

## ⚠ 표준 라이브러리만 쓴다

`itsdangerous` 도 `passlib` 도 안 쓴다. 배포 이미지는 `pip install --no-deps` 라 새
의존성이 **안 깔린다**(실측: `backend/Dockerfile`). 잘 되는 줄 알고 릴리스했다가
`ModuleNotFoundError` 로 게이트웨이가 아예 안 뜨는 쪽이 훨씬 나쁘다. PBKDF2 와 HMAC 은
`hashlib`·`hmac` 에 있다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 세션 쿠키 이름. MJPEG 프리뷰(`<img src>`)와 WebSocket 도 브라우저가 알아서 실어
#: 보내므로, 헤더 방식보다 이쪽이 화면 전체를 덮기에 맞다.
COOKIE = "piper_session"

#: 세션 수명. **길게 잡는다** — 짧으면 추론을 걸어 두고 자리를 뜬 사이에 만료되고,
#: 돌아와 보면 화면이 로그인으로 튕겨 있다. E-stop 은 인증을 안 타므로 만료가 안전에
#: 직접 닿지는 않지만, 급할 때 비밀번호를 치게 만드는 것은 그 자체로 나쁘다.
TTL_S = 30 * 24 * 3600

#: PBKDF2 반복. 2026년 기준 상식적인 값이고, 로그인은 드문 일이라 0.1초쯤은 싸다.
ROUNDS = 200_000

#: 최소 길이. 짧아도 되게 둔 것은 현장에서 PIN 을 쓰기 때문이고, 그래서 **대신
#: 무차별 대입을 막는다**(`throttle_s`). 둘 중 하나만 있으면 안 된다.
MIN_LEN = 4

#: 이 횟수부터 지연이 붙는다. 프로세스 메모리에만 있다 — 재시작하면 풀린다.
#: 재시작할 수 있는 사람은 이미 호스트를 쥔 사람이라 더 막아도 의미가 없다.
FAIL_GRACE = 5
FAIL_MAX_S = 60.0

_fails: dict[str, tuple[int, float]] = {}


def _file() -> Path:
    from app.core.config import settings

    return Path(settings.config_dir) / "auth.json"


#: 파일을 **요청마다 읽지 않는다.** 미들웨어가 모든 요청에서 이걸 부르고, MJPEG
#: 프리뷰·폴링 때문에 초당 수십 번이 된다. `stat` 으로 바뀐 것만 다시 읽는다.
_cache: tuple[tuple | None, dict] = (None, {})


def _load() -> dict:
    global _cache
    f = _file()
    try:
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        key = None
    except Exception:                                               # noqa: BLE001
        key = ("?",)
    if key == _cache[0]:
        return _cache[1]

    if key is None:
        _cache = (None, {})
        return _cache[1]
    try:
        d = json.loads(f.read_text())
    except Exception as exc:                                        # noqa: BLE001
        # ⚠ 깨진 파일을 "비밀번호 없음" 으로 읽으면 **인증이 조용히 꺼진다.** 그건
        #   잠기는 것보다 나쁘다 — 사람은 켜져 있다고 믿는다. 그래서 크게 남긴다.
        logger.error("인증 설정을 못 읽었습니다 — 로그인이 막힙니다: %s", exc)
        d = {"broken": True}
    _cache = (key, d)
    return d


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    # ⚠ 먼저 만들고 권한을 준 뒤에 쓴다. 쓰고 나서 `chmod` 하면 그 사이에 남이 읽는다.
    f.touch(mode=0o600, exist_ok=True)
    os.chmod(f, 0o600)
    f.write_text(json.dumps(d))
    _cache_clear()


def _cache_clear() -> None:
    """캐시를 버린다. ⚠ 쓰기 직후에 부른다 — `stat` 해상도 때문에 같은 나노초에 두 번
    쓰면 바뀐 것을 못 본다(비밀번호 설정 → 즉시 로그인 이 그 경로다)."""
    global _cache
    _cache = (None, {})


def enabled() -> bool:
    """비밀번호가 정해져 있나 = 인증이 켜져 있나."""
    d = _load()
    return bool(d.get("hash")) or bool(d.get("broken"))


def broken() -> bool:
    """설정 파일이 깨졌나. 켜진 것으로 치되 **아무도 못 들어간다** — 말해 줘야 한다."""
    return bool(_load().get("broken"))


def _digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS).hex()


def set_password(password: str) -> None:
    """비밀번호를 정한다(또는 바꾼다).

    ⚠ **서명 비밀도 같이 새로 만든다.** 그래서 비밀번호를 바꾸면 돌아다니던 세션이 전부
    죽는다 — 바꾸는 이유가 보통 "남이 알아버렸다" 이므로 그게 맞는 동작이다.
    """
    if len(password) < MIN_LEN:
        raise ValueError(f"비밀번호는 {MIN_LEN}자 이상이어야 합니다")
    salt = secrets.token_bytes(16)
    _save({"salt": salt.hex(), "hash": _digest(password, salt),
           "secret": secrets.token_hex(32), "rounds": ROUNDS})
    _fails.clear()
    logger.info("게이트웨이 비밀번호가 설정됐습니다 — 기존 세션은 모두 끊깁니다")


def clear_password() -> None:
    """인증을 끈다. ⚠ 끄면 LAN 의 누구나 다시 들어온다 — 부르는 쪽이 확인받아야 한다."""
    f = _file()
    f.unlink(missing_ok=True)
    _cache_clear()
    _fails.clear()
    logger.warning("게이트웨이 비밀번호가 해제됐습니다 — 인증이 꺼졌습니다")


def throttle_s(who: str = "-") -> float:
    """지금 로그인을 시도하면 몇 초 기다려야 하나. 0이면 바로 된다."""
    n, last = _fails.get(who, (0, 0.0))
    if n <= FAIL_GRACE:
        return 0.0
    wait = min(FAIL_MAX_S, 2.0 ** (n - FAIL_GRACE))
    left = (last + wait) - time.monotonic()
    return max(0.0, left)


def verify(password: str, who: str = "-") -> bool:
    """비밀번호가 맞나. **틀리면 다음 시도가 느려진다.**"""
    d = _load()
    if not d.get("hash"):
        return False
    want = _digest(password, bytes.fromhex(d["salt"]))
    ok = hmac.compare_digest(want, d["hash"])
    if ok:
        _fails.pop(who, None)
    else:
        n, _ = _fails.get(who, (0, 0.0))
        _fails[who] = (n + 1, time.monotonic())
    return ok


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(ttl: float = TTL_S) -> str:
    """세션 토큰. `payload.signature` — 서버에 세션 목록을 두지 않는다.

    ⚠ 상태를 안 두는 것은 **재시작 때문**이다. 게이트웨이는 릴리스·설정 변경으로 자주
    재시작하는데, 그때마다 모두가 로그아웃되면 사람이 인증을 꺼 버린다.
    """
    d = _load()
    secret = d.get("secret")
    if not secret:
        raise RuntimeError("비밀번호가 설정되지 않았습니다")
    payload = _b64(json.dumps({"exp": int(time.time() + ttl)}).encode())
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def check(token: str | None) -> bool:
    """세션이 유효한가. 서명과 만료를 **둘 다** 본다."""
    if not token or "." not in token:
        return False
    d = _load()
    secret = d.get("secret")
    if not secret:
        return False
    payload, _, sig = token.partition(".")
    want = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(want, _unb64(sig)):
            return False
        body = json.loads(_unb64(payload))
    except Exception:                                               # noqa: BLE001
        return False
    return float(body.get("exp", 0)) > time.time()
