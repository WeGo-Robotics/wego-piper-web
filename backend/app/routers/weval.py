"""가중치 분석 API — 추론 분석 화면의 체크포인트·오프라인 평가 탭 (feature/weight-analyzer.md).

체크포인트·학습 실행 id 에는 `/` 가 들어간다(`2026-10-20/14-28-18_act/020000`). 경로 매개변수로
받으면 라우트가 서로 먹으므로 **쿼리로** 받는다.
"""

import base64
import binascii
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services import weight_eval as WE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/weval", tags=["weight-eval"])


def _404(exc: Exception):
    raise HTTPException(404, str(exc))


@router.get("/runs")
async def list_runs():
    """학습 실행 목록 — 체크포인트 탭의 선택지."""
    return WE.runs()


@router.get("/run")
async def run_detail(run: str):
    """한 실행의 체크포인트들: step · 학습 곡선 · 실기 성공 · 오프라인 결과 (W1)."""
    try:
        return WE.run_detail(run)
    except LookupError as exc:
        _404(exc)


class JobRequest(BaseModel):
    checkpoint: str
    dataset: str
    episodes: list[int] = []
    stride: int = 3


@router.post("/jobs")
async def start_job(body: JobRequest):
    """에피소드 평가 시작. 같은 조합의 결과가 있으면 돌지 않고 그 키를 돌려준다."""
    try:
        return await WE.start_job(body.checkpoint, body.dataset, body.episodes, body.stride)
    except LookupError as exc:
        _404(exc)


@router.post("/jobs/stop")
async def stop_job():
    await WE.stop_job()
    return {"ok": True}


@router.get("/jobs/current")
async def current_job():
    return WE.status()


@router.get("/results")
async def list_results(checkpoint: str | None = None, dataset: str | None = None):
    return WE.jobs(checkpoint, dataset)


@router.get("/results/{key}")
async def result_summary(key: str):
    try:
        return WE.summary(key)
    except (OSError, ValueError) as exc:
        _404(exc)


@router.get("/results/{key}/episodes/{episode}")
async def result_episode(key: str, episode: int):
    try:
        return WE.episode_series(key, episode)
    except (OSError, ValueError, LookupError) as exc:
        _404(exc)


class FramesRequest(BaseModel):
    dataset: str
    episode: int
    frame: int


@router.post("/frames")
async def frames(body: FramesRequest):
    """한 프레임의 카메라 이미지 — 장면 편집기의 바탕. 평가와 같은 디코더에서 꺼낸다."""
    try:
        return await WE.frames(body.dataset, body.episode, body.frame)
    except LookupError as exc:
        _404(exc)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))


@router.get("/frames/{sid}/{camera}.png")
async def frame_png(sid: str, camera: str):
    try:
        return FileResponse(WE.frame_png(sid, camera), media_type="image/png")
    except LookupError as exc:
        _404(exc)


class ReactionRequest(BaseModel):
    checkpoint: str
    dataset: str
    episode: int
    frame: int
    # 카메라 → 편집 이미지(PNG data URL 또는 순수 base64). 비우면 원본 한 벌만 돈다.
    # ⚠ 폼 업로드가 아니라 JSON 이다 — 엔코더 프로브와 같다(python-multipart 를 안 들인다).
    edits: dict[str, str] = {}


def _decode(data: str) -> bytes:
    payload = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "편집 이미지를 읽을 수 없습니다")


@router.post("/reaction")
async def reaction(body: ReactionRequest):
    """원본 프레임과 편집 프레임의 두 예측 청크 + 말단 궤적 (feature/weight-analyzer.md §5)."""
    edits = {cam: _decode(img) for cam, img in body.edits.items()}
    try:
        return await WE.reaction(body.checkpoint, body.dataset, body.episode, body.frame, edits)
    except LookupError as exc:
        _404(exc)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, str(exc))
