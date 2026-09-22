"""로그인 — **이 라우터만 인증을 안 탄다** (`core/authgate.py` 의 열린 목록).

## ⚠ 상태를 먼저 물어볼 수 있어야 한다

화면은 뜨자마자 "인증이 켜져 있나 · 나는 들어와 있나" 를 알아야 로그인 화면을 띄울지
정한다. 그 질문 자체가 로그인을 요구하면 아무도 시작할 수 없다.

## ⚠ 비밀번호는 응답에 **절대** 안 나간다

해시도 안 내보낸다. 이 라우터가 밖으로 주는 것은 `enabled`·`authenticated` 두 불리언과
실패 사유뿐이다 — Vast API 키를 다룰 때와 같은 규칙이다(§12-22).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from app.services import auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str = ""


class PasswordBody(BaseModel):
    #: 이미 켜져 있으면 지금 비밀번호를 같이 받는다. 세션만으로 바꾸게 두면, 자리를 비운
    #: 사이 열린 화면 앞에 앉은 사람이 **주인을 갈아치울 수 있다.**
    current: str = ""
    new: str = ""


def _who(request: Request) -> str:
    """무차별 대입을 셀 단위. 프록시 뒤라서 완벽하지 않지만 없는 것보다 낫다."""
    return (request.client.host if request.client else "-") or "-"


def _session(response: Response) -> None:
    # ⚠ `secure=True` 를 안 준다 — 현장은 http 로 연다(`http://192.168.0.120`). 주면
    #   쿠키가 아예 저장되지 않아 **로그인이 성공했는데 계속 로그인 화면**이 된다.
    response.set_cookie(auth.COOKIE, auth.issue(), max_age=int(auth.TTL_S),
                        httponly=True, samesite="lax", path="/")


@router.get("/status")
async def status(session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """인증이 켜져 있나, 나는 들어와 있나. **로그인 없이 부를 수 있다.**"""
    on = auth.enabled()
    return {"enabled": on, "authenticated": (not on) or auth.check(session),
            "broken": auth.broken(), "min_length": auth.MIN_LEN}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not auth.enabled():
        # 꺼져 있으면 로그인할 것이 없다. 400 이 아니라 200 으로 사실을 말한다 —
        # 화면은 이 응답을 받고 그냥 들어가면 된다.
        return {"authenticated": True, "enabled": False}
    who = _who(request)
    wait = auth.throttle_s(who)
    if wait > 0:
        raise HTTPException(429, f"시도가 너무 잦습니다 — {wait:.0f}초 뒤에 다시 해 주세요")
    if not auth.verify(body.password, who):
        logger.warning("로그인 실패: %s", who)
        raise HTTPException(401, "비밀번호가 맞지 않습니다")
    _session(response)
    logger.info("로그인: %s", who)
    return {"authenticated": True, "enabled": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"authenticated": False}


@router.put("/password")
async def set_password(body: PasswordBody, request: Request, response: Response,
                       session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """비밀번호를 정하거나 바꾼다.

    ⚠ **처음 정할 때는 아무나 할 수 있다.** 사전 신뢰가 없는 LAN 장비의 부트스트랩은
    원래 그렇다(`services/auth.py` 머리말). 한 번 정해진 뒤에는 지금 비밀번호를 알아야
    바꿀 수 있다.
    """
    if auth.enabled():
        who = _who(request)
        wait = auth.throttle_s(who)
        if wait > 0:
            raise HTTPException(429, f"시도가 너무 잦습니다 — {wait:.0f}초 뒤에 다시 해 주세요")
        if not auth.verify(body.current, who):
            raise HTTPException(401, "지금 비밀번호가 맞지 않습니다")
    try:
        auth.set_password(body.new)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # ⚠ 바꾸면 서명 비밀이 새로 나므로 **내 세션도 죽는다.** 바로 다시 발급해 준다 —
    #   방금 바꾼 사람이 로그인 화면으로 튕기면 바꾼 것이 맞는지도 알 수 없다.
    _session(response)
    return {"enabled": True, "authenticated": True}


@router.delete("/password")
async def clear_password(body: PasswordBody, request: Request, response: Response):
    """인증을 끈다. ⚠ 끄면 LAN 의 누구나 다시 들어온다 — 지금 비밀번호를 받아야 한다."""
    if not auth.enabled():
        return {"enabled": False, "authenticated": True}
    who = _who(request)
    wait = auth.throttle_s(who)
    if wait > 0:
        raise HTTPException(429, f"시도가 너무 잦습니다 — {wait:.0f}초 뒤에 다시 해 주세요")
    if not auth.verify(body.current, who):
        raise HTTPException(401, "지금 비밀번호가 맞지 않습니다")
    auth.clear_password()
    response.delete_cookie(auth.COOKIE, path="/")
    return {"enabled": False, "authenticated": True}
