"""웹 채팅 — 우측 사이드바 (feature/agent-cli.md §4-6).

게이트웨이가 자기 기계의 AI CLI 를 한 턴씩 띄운다. 사건은 WS `agent_chat` 으로, 여기는 시작·중지·모드·
새 대화·상태뿐이다. 못 띄우는 환경(컨테이너·CLI 없음)은 503 과 이유다 — 화면이 그 이유를 그대로 보인다.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.agent_chat import chat

router = APIRouter(prefix="/api/agent/chat", tags=["agent"])


class SayRequest(BaseModel):
    text: str


class ModeRequest(BaseModel):
    mode: str


@router.get("")
async def chat_status():
    """지금 상태 — 쓸 수 있나(이유)·CLI·모드·세션·말풍선·마지막 턴."""
    return chat.status()


@router.post("")
async def chat_say(body: SayRequest):
    """한 턴 — 글을 보내고 turn 번호를 받는다. 답은 WS 로 흐른다."""
    try:
        turn = await chat.start_turn(body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except LookupError as exc:
        raise HTTPException(503, str(exc))
    return {"turn": turn}


@router.post("/stop")
async def chat_stop():
    return {"stopped": await chat.stop()}


@router.post("/reset")
async def chat_reset():
    """새 대화 — 세션 id 와 말풍선을 비운다. 도는 턴이 있으면 먼저 멈춘다."""
    await chat.stop()
    chat.reset()
    return {"status": "reset"}


@router.put("/mode")
async def chat_mode(body: ModeRequest):
    """observe(읽기 + 로봇 상태) | control(미션·플러그인·검출기·E-stop 까지) | develop(plugins/ 아래 수정 + piper-plugin 루프).
    저장소의 다른 곳을 고치는 길은 어느 모드에도 없다."""
    try:
        chat.set_mode(body.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"mode": chat.mode}
