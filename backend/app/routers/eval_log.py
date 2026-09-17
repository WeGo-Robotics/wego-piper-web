import json
import time

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter(prefix="/api/eval", tags=["eval"])

# CWD 상대경로였던 것을 절대경로로. 도커에서는 log_dir 이 /data/logs.
# mkdir 은 import 시점이 아니라 기록 시점에 한다 — 마운트 전이거나 권한이 없으면
# import 가 죽으면서 앱 전체가 뜨지 않는다.
EVAL_DIR = settings.log_dir / "eval_logs"
EVAL_FILE = EVAL_DIR / "eval_log.jsonl"


class EvalEntry(BaseModel):
    success: bool
    checkpoint: str = ""
    memo: str = ""
    # 어떤 파라미터로 돌린 추론인지. 없으면 "이 체크포인트 성공률 70%" 의 70% 가
    # 어느 속도·필터 설정에서 나온 것인지 알 수 없다 (feature/parameter-presets.md).
    preset: str = ""
    params: dict = {}
    # 시뮬 평가라면 **어느 세계에서** 잰 성공률인가. 안 주면 백엔드가 지금 올라간 장면으로
    # 채운다 — 화면이 알 필요가 없고, 빠뜨리면 장면이 늘수록 성공률이 뒤섞인다.
    scene: str = ""


@router.post("/log")
async def log_eval(entry: EvalEntry):
    record = {
        "timestamp": time.time(),
        "success": entry.success,
        "checkpoint": entry.checkpoint,
        "memo": entry.memo,
        "preset": entry.preset,
        # 프리셋 이름만으로는 부족하다 — 프리셋을 나중에 고치면 그 때의 값이 사라진다.
        # 실제로 쓴 값을 함께 남긴다 (재현성).
        "params": entry.params,
        "scene": entry.scene or _current_scene(),
        "robot_id": settings.resolved_robot_id,
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVAL_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    return {"status": "logged"}


@router.get("/stats")
async def eval_stats(last_n: int = 0):
    if not EVAL_FILE.exists():
        return {"total": 0, "successes": 0, "rate": 0, "recent": []}

    records = []
    for line in EVAL_FILE.read_text().strip().split("\n"):
        if line:
            records.append(json.loads(line))

    if last_n > 0:
        subset = records[-last_n:]
    else:
        subset = records

    total = len(subset)
    successes = sum(1 for r in subset if r["success"])

    return {
        "total": total,
        "successes": successes,
        "rate": round(successes / total, 3) if total else 0,
        "recent": records[-10:][::-1],
        "by_preset": _rate_by(subset, "preset"),
        "by_checkpoint": _rate_by(subset, "checkpoint"),
        # ⚠ 장면을 여러 개 쓰면 이게 없는 성공률은 **아무 뜻이 없다** — 쉬운 세계와
        #   어려운 세계의 평균이다 (feature/sim-scene-editor.md 4단계).
        "by_scene": _rate_by(subset, "scene"),
    }


def _current_scene() -> str:
    """지금 시뮬에 올라간 장면 id. 실기 평가면 빈 문자열이다."""
    try:
        from app.services.sim_scenes import current_id

        return current_id() or ""
    except Exception:
        return ""


def _rate_by(records: list[dict], key: str) -> list[dict]:
    """그룹별 성공률 — **"어느 설정이 잘 됐나"** 를 비교할 수 있게 한다."""
    groups: dict[str, list[bool]] = {}
    for r in records:
        name = r.get(key) or ""
        if not name:
            continue
        groups.setdefault(name, []).append(bool(r.get("success")))
    out = [
        {
            key: name,
            "total": len(v),
            "successes": sum(v),
            "rate": round(sum(v) / len(v), 3),
        }
        for name, v in groups.items()
    ]
    return sorted(out, key=lambda x: (-x["rate"], -x["total"]))
