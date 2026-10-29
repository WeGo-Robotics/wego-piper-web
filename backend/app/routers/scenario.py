"""시나리오 설계기 API — 템플릿·계획 생성·작업판 인쇄 (feature/scenario-designer.md §15).

전부 순수 계산이다. 장치·버스·파일을 건드리지 않고, 계획도 저장하지 않는다 — 저장한 설정은
프리셋 스토어(`/api/presets/scenario`)가, 계획은 녹화 때 데이터셋 사이드카가 갖는다(2단계).
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.scenario import ScenarioError
from app.services.scenario import plan, printing, templates
from app.services.scenario.board import Board

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scenario", tags=["scenario"])


@router.get("/templates")
async def list_templates():
    return [t.to_dict() for t in templates.load_templates().values()]


class GenerateRequest(BaseModel):
    template: str
    seed: int = 0
    board: dict
    catalog: list[dict]
    values: dict = Field(default_factory=dict)


@router.post("/generate")
async def generate(body: GenerateRequest):
    try:
        # 수천 시도면 수백 ms 다 — 이벤트 루프(heartbeat·WS)를 막지 않게 스레드로
        return await run_in_threadpool(
            plan.generate, body.template, body.board, body.catalog, body.values, body.seed)
    except ScenarioError as e:
        raise HTTPException(400, str(e))


class PrintRequest(BaseModel):
    board: dict
    paper: Literal["a4", "a3"] = "a4"
    mode: Literal["faint", "bold", "overlay"] = "faint"
    line_mm: float = 0.25
    title: str = ""
    # 화면이 이미 옮긴 문장 — printing.LABELS_EN 의 키
    labels: dict[str, str] = Field(default_factory=dict)


@router.post("/print/layout")
async def print_layout(body: PrintRequest):
    """몇 장으로 나뉘나 — 인쇄 전에 화면이 보여 준다."""
    try:
        return printing.layout(Board.from_dict(body.board), body.paper).to_dict()
    except ScenarioError as e:
        raise HTTPException(400, str(e))


@router.post("/print")
async def print_board(body: PrintRequest):
    try:
        board = Board.from_dict(body.board)
        pdf = await run_in_threadpool(
            printing.render, board, paper=body.paper, mode=body.mode, line_mm=body.line_mm,
            title=body.title, labels=body.labels)
    except ScenarioError as e:
        raise HTTPException(400, str(e))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="board.pdf"'})
