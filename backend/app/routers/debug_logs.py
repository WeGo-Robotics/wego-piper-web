"""디버그 런 뷰어 — 추론 디버그 레코더(wrapper/debug_recorder.py)가 남긴 폴더 조회/재생.

폴더 구조: settings.debug_dir/{run_id}/
  meta.json, observations.jsonl, inference.jsonl, control.jsonl, images/{cam}/{seq:06d}.jpg
"""

import json
import logging
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])

_STREAMS = {"observations", "inference", "control"}


def _base() -> Path:
    return Path(settings.debug_dir)


def _run_dir(run_id: str) -> Path:
    """run_id를 검증하고 실제 디렉터리 경로 반환. 경로 이탈 차단."""
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise HTTPException(400, "Invalid run id")
    base = _base().resolve()
    path = (base / run_id).resolve()
    if base not in path.parents and path != base:
        raise HTTPException(400, "Invalid run id")
    if not path.is_dir():
        raise HTTPException(404, "Run not found")
    return path


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _read_meta(run_dir: Path) -> dict:
    p = run_dir / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _dir_size_kb(run_dir: Path) -> float:
    total = 0
    for f in run_dir.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return round(total / 1024, 1)


def _cameras(run_dir: Path) -> list[str]:
    img_dir = run_dir / "images"
    if not img_dir.is_dir():
        return []
    return sorted(d.name for d in img_dir.iterdir() if d.is_dir())


def _run_summary(run_dir: Path) -> dict:
    meta = _read_meta(run_dir)
    st = run_dir.stat()
    return {
        "id": run_dir.name,
        "mode": meta.get("mode", ""),
        "meta": meta,
        "cameras": _cameras(run_dir),
        "counts": {
            "observations": _count_lines(run_dir / "observations.jsonl"),
            "inference": _count_lines(run_dir / "inference.jsonl"),
            "control": _count_lines(run_dir / "control.jsonl"),
        },
        "size_kb": _dir_size_kb(run_dir),
        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
    }


@router.get("/runs")
async def list_runs():
    """디버그 런 목록 (최신순)."""
    base = _base()
    if not base.exists():
        return []
    runs = [d for d in base.iterdir() if d.is_dir()]
    runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [_run_summary(d) for d in runs]


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """런 상세 (메타 + 카운트 + 카메라)."""
    return _run_summary(_run_dir(run_id))


@router.get("/runs/{run_id}/records/{stream}")
async def get_records(run_id: str, stream: str, offset: int = 0, limit: int = 5000):
    """스트림(jsonl) 레코드 반환. 페이지네이션."""
    if stream not in _STREAMS:
        raise HTTPException(400, f"stream must be one of {sorted(_STREAMS)}")
    run_dir = _run_dir(run_id)
    path = run_dir / f"{stream}.jsonl"
    if not path.exists():
        return {"records": [], "total": 0, "offset": offset, "truncated": False}

    offset = max(0, offset)
    limit = max(1, min(limit, 20000))
    records = []
    total = 0
    with open(path) as f:
        for i, line in enumerate(f):
            total += 1
            if i < offset or len(records) >= limit:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    truncated = total > offset + len(records)
    return {"records": records, "total": total, "offset": offset, "truncated": truncated}


@router.get("/runs/{run_id}/images/{cam}/{seq}")
async def get_image(run_id: str, cam: str, seq: int):
    """녹화된 카메라 프레임(jpg) 서빙."""
    if "/" in cam or "\\" in cam or ".." in cam:
        raise HTTPException(400, "Invalid camera")
    run_dir = _run_dir(run_id)
    path = (run_dir / "images" / cam / f"{seq:06d}.jpg").resolve()
    if run_dir.resolve() not in path.parents:
        raise HTTPException(400, "Invalid path")
    if not path.exists():
        raise HTTPException(404, "Frame not found")
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "max-age=3600"},
    )


class SimulateRequest(BaseModel):
    lowpass_alpha: float = 0.5
    max_velocity: float = 180.0
    max_gripper_velocity: float = 300.0
    max_jerk: float = 0.0
    interpolation_steps: int = 0
    fps: float = 20.0


@router.post("/runs/{run_id}/simulate")
async def simulate_filter(run_id: str, body: SimulateRequest):
    """녹화된 raw action(target)을 사용자 필터 파라미터로 재생.

    실제 필터 코드(wrapper/action_filter.py:ActionFilter)를 그대로 재사용한다.
    current_state로는 녹화된 actual을 사용(로봇 없음) — velocity/jerk/lowpass에 충실,
    interpolation은 큐 상호작용이 없어 근사, 서버 chunk-level smoothing/offset은 범위 밖.
    """
    run_dir = _run_dir(run_id)
    control_path = run_dir / "control.jsonl"
    if not control_path.exists():
        raise HTTPException(404, "control.jsonl not found")

    # wrapper 모듈 import (순수 파이썬)
    wrapper_dir = str(Path(__file__).resolve().parents[3] / "wrapper")
    if wrapper_dir not in sys.path:
        sys.path.insert(0, wrapper_dir)
    try:
        from action_filter import ActionFilter
    except Exception as e:
        raise HTTPException(500, f"action_filter import 실패: {e}")

    # control 레코드 로드
    rows: list[dict] = []
    with open(control_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not rows:
        raise HTTPException(400, "control 레코드가 비어있습니다")

    meta = _read_meta(run_dir)
    motors = meta.get("motor_names") or sorted((rows[0].get("target") or {}).keys())

    filt = ActionFilter(fps=body.fps)
    filt.lowpass_alpha = max(0.05, min(1.0, body.lowpass_alpha))
    filt.max_velocity = max(0.0, min(1000.0, body.max_velocity))
    filt.max_gripper_velocity = max(0.0, min(500.0, body.max_gripper_velocity))
    filt.max_jerk = max(0.0, min(5000.0, body.max_jerk))
    filt.interpolation_steps = max(0, min(10, body.interpolation_steps))

    t0 = rows[0].get("t") or 0
    t_arr: list[float] = []
    series = {m: {"target": [], "actual": [], "filtered_recorded": [], "filtered_sim": []} for m in motors}

    for row in rows:
        target = row.get("target") or None
        actual = row.get("actual") or {}
        recorded = row.get("filtered") or {}
        sim = filt.apply(target, actual) if target else None
        t_arr.append(round((row.get("t") or t0) - t0, 4))
        for m in motors:
            s = series[m]
            s["target"].append(_num(target, m))
            s["actual"].append(_num(actual, m))
            s["filtered_recorded"].append(_num(recorded, m))
            s["filtered_sim"].append(_num(sim, m))

    return {"motors": motors, "t": t_arr, "series": series, "steps": len(rows)}


def _num(d, key):
    if not d:
        return None
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) else None


@router.get("/runs/{run_id}/download")
async def download_run(run_id: str):
    """런 폴더를 zip으로 다운로드."""
    run_dir = _run_dir(run_id)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in run_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(run_dir))
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{run_id}.zip",
        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)),
    )


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    """런 폴더 삭제."""
    run_dir = _run_dir(run_id)
    shutil.rmtree(run_dir)
    logger.info("Deleted debug run: %s", run_dir)
    return {"status": "deleted", "id": run_id}
