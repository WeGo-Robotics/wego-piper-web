"""학습 API."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.cli_mapping import build_train_args
from app.services.train_manager import train_manager
from app.services.process_manager import process_manager

router = APIRouter(prefix="/api/training", tags=["training"])
logger = logging.getLogger(__name__)


class TrainStartRequest(BaseModel):
    dataset_repo_id: str
    policy_type: str = "act"
    pretrained_path: str = ""
    output_dir: str = ""
    batch_size: int = 8
    steps: int = 100000
    log_freq: int = 200
    save_freq: int = 20000
    eval_freq: int = 0
    num_workers: int = 4
    seed: int = 1000
    device: str = "cuda"
    optimizer_type: str = "adam"
    learning_rate: float = 0.0
    wandb_enable: bool = False
    wandb_project: str = ""
    resume: bool = False
    state_dim: int = 0
    action_dim: int = 0
    rename_map: str = ""


class TrainPreviewRequest(BaseModel):
    dataset_repo_id: str = ""
    policy_type: str = "act"
    pretrained_path: str = ""
    output_dir: str = ""
    batch_size: int = 8
    steps: int = 100000
    log_freq: int = 200
    save_freq: int = 20000
    num_workers: int = 4
    seed: int = 1000
    device: str = "cuda"
    optimizer_type: str = "adam"
    learning_rate: float = 0.0
    wandb_enable: bool = False
    wandb_project: str = ""
    resume: bool = False
    state_dim: int = 0
    action_dim: int = 0
    rename_map: str = ""


class RenameMapQuery(BaseModel):
    pretrained_path: str


@router.post("/rename-map")
async def get_rename_map(body: RenameMapQuery):
    """pretrained 모델의 policy_preprocessor.json에서 rename_map 추출."""
    from pathlib import Path
    import json
    pre_path = Path(body.pretrained_path) / "policy_preprocessor.json"
    if not pre_path.exists():
        return {"rename_map": {}}
    try:
        data = json.loads(pre_path.read_text())
        for step in data.get("steps", []):
            if step.get("registry_name") == "rename_observations_processor":
                return {"rename_map": step.get("config", {}).get("rename_map", {})}
    except Exception:
        pass
    return {"rename_map": {}}


class TrainCustomRequest(BaseModel):
    args: list[str]
    total_steps: int = 100000
    output_dir: str = ""


@router.post("/start")
async def start_training(body: TrainStartRequest):
    """학습 시작."""
    # GPU 경합 방지
    if process_manager.state.value not in ("idle", "error"):
        raise HTTPException(409, "추론이 실행 중입니다. 추론을 먼저 중지하세요.")
    if train_manager.is_running:
        raise HTTPException(409, "학습이 이미 실행 중입니다.")

    params = body.model_dump(exclude_none=True)
    # lr=0이면 기본값 사용 (전달하지 않음)
    if params.get("learning_rate", 0) <= 0:
        params.pop("learning_rate", None)
    if not params.get("pretrained_path"):
        params.pop("pretrained_path", None)
    if not params.get("wandb_project"):
        params.pop("wandb_project", None)
    if not params.get("output_dir"):
        params.pop("output_dir", None)

    args = build_train_args(params)

    try:
        await train_manager.start(args, total_steps=body.steps, output_dir=body.output_dir)
    except Exception as e:
        raise HTTPException(500, f"학습 시작 실패: {e}")
    return {"status": "started", "pid": train_manager.pm.pid, "args": args}


@router.post("/start-custom")
async def start_training_custom(body: TrainCustomRequest):
    """직접 편집한 CLI 인자로 학습 시작."""
    if process_manager.state.value not in ("idle", "error"):
        raise HTTPException(409, "추론이 실행 중입니다.")
    if train_manager.is_running:
        raise HTTPException(409, "학습이 이미 실행 중입니다.")
    if not body.args:
        raise HTTPException(400, "CLI 인자가 비어있습니다")
    try:
        await train_manager.start(body.args, total_steps=body.total_steps, output_dir=body.output_dir)
    except Exception as e:
        raise HTTPException(500, f"학습 시작 실패: {e}")
    return {"status": "started", "pid": train_manager.pm.pid, "args": body.args}


@router.post("/stop")
async def stop_training():
    """학습 중지."""
    await train_manager.stop()
    return {"status": "stopped"}


@router.get("/status")
async def training_status():
    """학습 상태 + 최근 메트릭."""
    return train_manager.get_status()


@router.get("/metrics")
async def training_metrics():
    """전체 메트릭 히스토리 (loss curve용)."""
    return train_manager.history.to_dict()


@router.post("/preview")
async def preview_train_args(body: TrainPreviewRequest):
    """학습 CLI 인자 미리보기."""
    params = body.model_dump(exclude_none=True)
    if params.get("learning_rate", 0) <= 0:
        params.pop("learning_rate", None)
    if not params.get("pretrained_path"):
        params.pop("pretrained_path", None)
    if not params.get("wandb_project"):
        params.pop("wandb_project", None)
    if not params.get("output_dir"):
        params.pop("output_dir", None)
    args = build_train_args(params)
    return {"args": args, "command": " ".join(args)}


@router.get("/checkpoints")
async def list_checkpoints():
    """학습 출력 디렉토리의 체크포인트 목록."""
    out_dir = train_manager.output_dir
    if not out_dir:
        return []
    ckpt_dir = Path(out_dir) / "checkpoints"
    if not ckpt_dir.exists():
        return []
    results = []
    for d in sorted(ckpt_dir.iterdir(), reverse=True):
        if d.is_dir() and d.name != "last":
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            results.append({
                "name": d.name,
                "step": int(d.name) if d.name.isdigit() else 0,
                "size_kb": round(size / 1024, 1),
                "path": str(d),
            })
    return results
