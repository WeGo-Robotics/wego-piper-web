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


@router.post("/log")
async def log_eval(entry: EvalEntry):
    record = {
        "timestamp": time.time(),
        "success": entry.success,
        "checkpoint": entry.checkpoint,
        "memo": entry.memo,
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
    }
