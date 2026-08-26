"""작업 단계(phase) 라벨 API (feature/01-phase-annotation.md §7).

원본 데이터셋을 **절대 in-place 로 고치지 않는다** — 라벨은 사이드카에만 쓴다.

⚠ 경로가 `/api/datasets/...` 가 아니라 별도 prefix 인 이유:
`datasets.py` 의 `@router.get("/{dataset_id:path}")` 가 catch-all 이라
같은 라우터에 넣으면 등록 순서에 의존하게 된다. 실제로 `/upload-status` 와 `/hf-cli` 가
그 catch-all 에 먹혀 404 였다. 접두사를 분리해 구조적으로 피한다.
"""

import asyncio
import logging
from dataclasses import asdict, fields

import pandas as pd
from fastapi import APIRouter, HTTPException
from piper_phase import Params
from pydantic import BaseModel

from piper_phase import labeler as PL

from app.core.config import settings
from app.services.dataset_scanner import baked_info, find_dataset_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/phase", tags=["phase"])

# 분석은 수 분 걸릴 수 있어 요청 스레드에서 돌리지 않는다.
# 동시에 두 분석이 돌면 사이드카를 서로 덮어쓰므로 하나만 허용한다.
_lock = asyncio.Lock()
_bake_lock = asyncio.Lock()
_PARAM_FIELDS = {f.name for f in fields(Params)}


def _dataset(dataset_id: str):
    path = find_dataset_path(dataset_id)
    if path is None:
        raise HTTPException(404, f"데이터셋을 찾을 수 없습니다: {dataset_id}")
    return path


def _params(overrides: dict | None, ds_path) -> Params:
    """기본값은 데이터셋의 fps 에서. 알 수 없는 키는 **조용히 무시하지 않는다.**"""
    values = {"fps": PL.dataset_fps(ds_path)}
    unknown = set(overrides or {}) - _PARAM_FIELDS
    if unknown:
        raise HTTPException(400, f"알 수 없는 파라미터: {', '.join(sorted(unknown))}")
    values.update(overrides or {})
    return Params(**values)


@router.get("/defaults")
async def param_defaults():
    """라벨러 파라미터 기본값 + 페이즈 이름. UI 슬라이더가 여기서 온다."""
    from piper_phase import PHASE_NAMES

    return {"params": asdict(Params()), "phases": list(PHASE_NAMES)}


class AnalyzeRequest(BaseModel):
    params: dict = {}
    episodes: list[int] | None = None
    save: bool = True
    # 파라미터 미리보기용 (§3.5): save=False 로 돌려보고 구간을 화면에 겹쳐 본다.
    # 요약만으로는 트랙을 못 그린다 — 구간이 응답에 실려야 저장 없이 비교가 된다.
    include_segments: bool = False


@router.post("/{dataset_id:path}/analyze")
async def analyze(dataset_id: str, body: AnalyzeRequest):
    """전 에피소드 라벨링 + 사이드카 저장. 이상 에피소드 목록을 함께 준다."""
    ds = _dataset(dataset_id)
    p = _params(body.params, ds)
    if _lock.locked():
        raise HTTPException(409, "다른 분석이 진행 중입니다")
    async with _lock:
        result = await asyncio.to_thread(PL.analyze, ds, p, body.episodes)
        summary = PL.summary({**result, "_signals": None})
        episodes_detail = result["episodes"] if body.include_segments else None
        if body.save:
            # save() 는 병합하며 result["episodes"] 를 바꾼다 — 응답용은 위에서 잡아뒀다
            await asyncio.to_thread(PL.save, ds, result)
    out = {"dataset_id": dataset_id, "params": asdict(p), **summary}
    if episodes_detail is not None:
        # summary 의 "episodes"(개수)와 키가 겹치므로 상세는 딴 이름으로
        out["episode_labels"] = episodes_detail
    return out


@router.get("/{dataset_id:path}/labels")
async def get_labels(dataset_id: str):
    """사이드카 전체 (에피소드별 구간 + 검토 상태)."""
    data = PL.load(_dataset(dataset_id))
    if data is None:
        raise HTTPException(404, "아직 분석하지 않았습니다 — /analyze 를 먼저 실행하세요")
    return data


@router.get("/{dataset_id:path}/summary")
async def get_summary(dataset_id: str):
    """사이클 분포 + 이상 에피소드 목록 (에피소드 뷰어의 ⚠ 배지·정렬용).

    이상 판정은 백엔드 `flag_outliers` 한 곳에만 있다 — 프론트가 자기 규칙을
    들면 CLI/API/UI 세 곳이 서로 다른 ⚠ 를 붙이게 된다.
    """
    data = PL.load(_dataset(dataset_id))
    if data is None:
        raise HTTPException(404, "아직 분석하지 않았습니다 — /analyze 를 먼저 실행하세요")
    return PL.summary(data)


class EpisodeLabels(BaseModel):
    segments: list[list[int]]          # [[start, end(포함), phase], ...]
    reviewed: bool = True
    note: str = ""


@router.put("/{dataset_id:path}/labels/{episode}")
async def put_episode_labels(dataset_id: str, episode: int, body: EpisodeLabels):
    """한 에피소드 구간 저장 (수동 편집)."""
    ds = _dataset(dataset_id)
    data = PL.load(ds)
    if data is None:
        raise HTTPException(404, "아직 분석하지 않았습니다")
    key = str(episode)
    if key not in data["episodes"]:
        raise HTTPException(404, f"에피소드 {episode} 가 사이드카에 없습니다")

    prev = data["episodes"][key]
    frames = prev.get("frames", 0)
    _validate_segments(body.segments, frames)

    from piper_phase import HOLD

    prev.update({
        "segments": [list(s) for s in body.segments],
        "cycles": sum(1 for s in body.segments if s[2] == HOLD),
        "reviewed": body.reviewed,
        "edited_by": "auto+manual",
        "note": body.note,
    })
    labels_path, _ = PL.sidecar_paths(ds)
    import json

    labels_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return prev


def _validate_segments(segments: list[list[int]], frames: int) -> None:
    """구간이 프레임 전체를 빈틈·겹침 없이 덮어야 한다.

    깨진 구간을 저장하면 굽기 단계에서 프레임 하나가 라벨 없이 남는다.
    """
    from piper_phase import PHASE_NAMES

    if not segments:
        raise HTTPException(400, "구간이 비어 있습니다")
    cursor = 0
    for s in segments:
        if len(s) != 3:
            raise HTTPException(400, f"구간 형식이 [start, end, phase] 여야 합니다: {s}")
        start, end, phase = s
        if start != cursor:
            raise HTTPException(400, f"구간에 빈틈/겹침이 있습니다: {start} (기대 {cursor})")
        if end < start:
            raise HTTPException(400, f"end < start: {s}")
        if not 0 <= phase < len(PHASE_NAMES):
            raise HTTPException(400, f"알 수 없는 페이즈 코드: {phase}")
        cursor = end + 1
    if frames and cursor != frames:
        raise HTTPException(400, f"구간이 {cursor} 프레임까지만 덮습니다 (전체 {frames})")


@router.get("/{dataset_id:path}/signals/{episode}")
async def get_signals(dataset_id: str, episode: int):
    """그래프용 신호 배열 (캐시에서).

    손목 변화율은 비디오 디코딩이 필요해 매번 계산할 수 없다 —
    분석 1회에 같이 떨궈 UI 가 즉시 읽는다.
    """
    ds = _dataset(dataset_id)
    _, signals_path = PL.sidecar_paths(ds)
    if not signals_path.exists():
        raise HTTPException(404, "신호 캐시가 없습니다 — /analyze 를 먼저 실행하세요")
    df = pd.read_parquet(signals_path)
    e = df[df.episode_index == episode]
    if e.empty:
        raise HTTPException(404, f"에피소드 {episode} 신호가 없습니다")
    return {
        "episode": episode,
        "frames": len(e),
        "speed": e["speed"].round(3).tolist(),
        "gripper_gap": e["gripper_gap"].round(3).tolist(),
        "phase": e["phase"].tolist(),
    }


@router.get("/{dataset_id:path}/status")
async def status(dataset_id: str):
    """사이드카 유무 + 검토 진행률. 목록 화면 배지용."""
    data = PL.load(_dataset(dataset_id))
    if data is None:
        return {"analyzed": False}
    eps = data["episodes"]
    return {
        "analyzed": True,
        "episodes": len(eps),
        "reviewed": sum(1 for v in eps.values() if v.get("reviewed")),
        "params": data.get("params", {}),
    }


# ── ACT-Aux 용 굽기 (feature/act-aux.md §4) ──────────────────────────────────

class BakeRequest(BaseModel):
    reviewed_only: bool = False
    force: bool = True      # 이전 bake 결과물이면 덮어쓴다 (bake 가 결과물인지 확인한다)


@router.post("/{dataset_id:path}/bake")
async def bake(dataset_id: str, body: BakeRequest):
    """사이드카 → LeRobot subtask 로 구운 `<name>_stage` 데이터셋을 만든다.

    백엔드는 lerobot 을 직접 import 하지 않는다 — `lerobot_policy_act_aux.bake` 를
    학습 파이썬(`settings.grpc_python`)으로 subprocess 실행한다 (CLAUDE.md 의 CLI 래핑 원칙).
    수십 초 걸리므로 요청 스레드에서 돌리지 않고, 동시에 둘은 허용하지 않는다.
    """
    ds = _dataset(dataset_id)
    if baked_info(ds) is not None:
        raise HTTPException(400, "이미 구운 사본입니다 — 원본 데이터셋에서 굽습니다")
    if PL.load(ds) is None:
        raise HTTPException(400, "아직 분석하지 않았습니다 — /analyze 를 먼저 실행하세요")
    if _bake_lock.locked():
        raise HTTPException(409, "다른 굽기가 진행 중입니다")

    cmd = [settings.grpc_python, "-m", "lerobot_policy_act_aux.bake", str(ds)]
    if body.reviewed_only:
        cmd.append("--reviewed-only")
    if body.force:
        cmd.append("--force")

    async with _bake_lock:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except TimeoutError:
            proc.kill()
            raise HTTPException(504, "굽기가 15분 안에 끝나지 않았습니다")
    lines = [ln for ln in raw.decode(errors="replace").splitlines()
             if ln.strip() and "Warning" not in ln and "warn(" not in ln]
    if proc.returncode != 0:
        logger.error("bake 실패 (%s): %s", dataset_id, "\n".join(lines[-20:]))
        raise HTTPException(500, "굽기 실패: " + ("\n".join(lines[-5:]) or f"exit {proc.returncode}"))

    out_id = f"{dataset_id}_stage"
    out_path = find_dataset_path(out_id)
    return {
        "output_id": out_id,
        "baked": baked_info(out_path) if out_path else None,
        "log": lines[-30:],
    }
