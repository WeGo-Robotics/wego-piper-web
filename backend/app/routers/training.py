"""학습 API."""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.cli_mapping import apply_dim_overrides, build_train_args, resolve_rename_map
from app.routers.presets import register_domain
from app.services.exclusivity import Activity, require_idle
from app.services.training import train_manager
from app.services.training.jobs import MAX_CONCURRENT_JOBS, job_registry

router = APIRouter(prefix="/api/training", tags=["training"])

# 프리셋에 담는 값 = "튜닝" 만. `dataset_repo_id` 나 `output_dir` 같은 **실행 대상**은
# 담지 않는다 — 담으면 다른 데이터셋에 재사용할 수 없다 (feature/parameter-presets.md).
PRESET_DOMAIN = "training"
PRESET_EXCLUDED = {
    "dataset_repo_id", "output_dir", "pretrained_path", "policy_repo_id",
    "resume", "state_dim", "action_dim", "rename_map",
}
logger = logging.getLogger(__name__)


class TrainStartRequest(BaseModel):
    dataset_repo_id: str
    policy_type: str = "act"
    pretrained_path: str = ""
    policy_repo_id: str = ""
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
    use_policy_training_preset: bool = True
    state_dim: int = 0
    action_dim: int = 0
    rename_map: str = ""
    policy_params: dict[str, Any] = Field(default_factory=dict)
    amp: str = "bf16"  # 혼합정밀도: "off" | "bf16" | "fp16" → ACCELERATE_MIXED_PRECISION env


class TrainPreviewRequest(BaseModel):
    dataset_repo_id: str = ""
    policy_type: str = "act"
    pretrained_path: str = ""
    policy_repo_id: str = ""
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
    use_policy_training_preset: bool = True
    state_dim: int = 0
    action_dim: int = 0
    rename_map: str = ""
    policy_params: dict[str, Any] = Field(default_factory=dict)
    amp: str = "bf16"  # 혼합정밀도: "off" | "bf16" | "fp16" → ACCELERATE_MIXED_PRECISION env


def preset_keys() -> set[str]:
    """프리셋이 담는 키 — `TrainStartRequest` 에서 파생한다 (사본을 만들지 않는다)."""
    return set(TrainStartRequest.model_fields) - PRESET_EXCLUDED


register_domain(PRESET_DOMAIN, preset_keys())


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
    amp: str = "bf16"


def _amp_env(amp: str) -> dict[str, str] | None:
    """AMP 선택값 → ACCELERATE_MIXED_PRECISION 환경변수 (off면 None).

    lerobot_train.py는 accelerator.autocast()로 학습하며, 혼합정밀도는
    --policy.use_amp(학습 루프 미사용)가 아니라 이 환경변수로만 켜진다.
    """
    return {"ACCELERATE_MIXED_PRECISION": amp} if amp and amp != "off" else None


@router.post("/start")
async def start_training(body: TrainStartRequest):
    """학습 시작."""
    require_idle(Activity.TRAINING)

    params = body.model_dump(exclude_none=True)
    # amp는 CLI 인자가 아니라 환경변수로 주입 → params에서 분리
    amp = params.pop("amp", "bf16")
    # lr=0이면 기본값 사용 (전달하지 않음)
    if params.get("learning_rate", 0) <= 0:
        params.pop("learning_rate", None)
    if not params.get("pretrained_path"):
        params.pop("pretrained_path", None)
    if not params.get("wandb_project"):
        params.pop("wandb_project", None)
    if not params.get("output_dir"):
        params.pop("output_dir", None)
    if not params.get("policy_repo_id"):
        params.pop("policy_repo_id", None)

    # 파일시스템 접근은 인자 조립과 분리돼 있다 (원격 학습 대비).
    # config.json 수정은 **파괴적**이라 시작 경로에서만 한다.
    rename_map = ""
    if params.get("pretrained_path"):
        rename_map = resolve_rename_map(params["pretrained_path"])
        apply_dim_overrides(
            params["pretrained_path"], body.state_dim, body.action_dim
        )
    args = build_train_args(params, rename_map=rename_map)

    try:
        await train_manager.start(
            args, total_steps=body.steps, output_dir=body.output_dir, env_extra=_amp_env(amp)
        )
    except Exception as e:
        raise HTTPException(500, f"학습 시작 실패: {e}")
    return {"status": "started", "pid": train_manager.runner.pid, "args": args}


@router.post("/start-custom")
async def start_training_custom(body: TrainCustomRequest):
    """직접 편집한 CLI 인자로 학습 시작."""
    require_idle(Activity.TRAINING)
    if not body.args:
        raise HTTPException(400, "CLI 인자가 비어있습니다")
    try:
        await train_manager.start(
            body.args, total_steps=body.total_steps, output_dir=body.output_dir, env_extra=_amp_env(body.amp)
        )
    except Exception as e:
        raise HTTPException(500, f"학습 시작 실패: {e}")
    return {"status": "started", "pid": train_manager.runner.pid, "args": body.args}


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


# ── job 레지스트리 (feature/cloud-training.md 3단계) ──
#
# 로컬 학습도 `job_id="local"` 로 여기 들어간다. 원격이 붙어도 UI 가 한 벌로 끝나고,
# 상태가 프로세스 밖(버스)에 있으므로 **서버가 재시작해도 학습이 계속 보인다.**


@router.get("/jobs")
async def list_jobs():
    """학습 job 목록 (최근 순). 로컬 + (나중에) 원격."""
    return {
        "jobs": [j.to_dict() for j in job_registry.list()],
        "max_concurrent": MAX_CONCURRENT_JOBS,
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = job_registry.get(job_id)
    if job is None:
        raise HTTPException(404, f"job 을 찾을 수 없습니다: {job_id}")
    return job.to_dict()


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """끝난 job 을 목록에서 지운다. 실행 중이면 거부한다."""
    job = job_registry.get(job_id)
    if job is None:
        raise HTTPException(404, f"job 을 찾을 수 없습니다: {job_id}")
    if job.is_active:
        raise HTTPException(409, "실행 중인 job 은 지울 수 없습니다 — 먼저 중지하세요")
    job_registry.delete(job_id)
    return {"status": "deleted"}


@router.get("/jobs/{job_id}/logs")
async def job_logs(job_id: str, start: int = 0, limit: int = 500):
    """job 로그 페이지네이션.

    6시간 학습이면 수만 줄이라 전부 WS 로 밀면 브라우저가 죽는다. WS 로는 신규분만 가고
    과거는 여기서 읽는다. `dropped` 가 0보다 크면 링버퍼 앞이 잘렸다는 뜻이다 —
    **조용히 빠뜨리지 않고 몇 줄인지 알려준다.**
    """
    if limit < 1 or limit > 2000:
        raise HTTPException(400, "limit 은 1~2000 이어야 합니다")
    return {"job_id": job_id, **job_registry.logs(job_id, start, limit)}


@router.post("/preview")
async def preview_train_args(body: TrainPreviewRequest):
    """학습 CLI 인자 미리보기."""
    params = body.model_dump(exclude_none=True)
    # amp는 CLI 인자가 아니라 환경변수 → command 문자열엔 넣지 않고 env로 별도 반환
    amp = params.pop("amp", "bf16")
    if params.get("learning_rate", 0) <= 0:
        params.pop("learning_rate", None)
    if not params.get("pretrained_path"):
        params.pop("pretrained_path", None)
    if not params.get("wandb_project"):
        params.pop("wandb_project", None)
    if not params.get("output_dir"):
        params.pop("output_dir", None)
    if not params.get("policy_repo_id"):
        params.pop("policy_repo_id", None)
    # ⚠ 미리보기는 **부작용이 없어야 한다** — 이전에는 build_train_args() 안에서
    # 체크포인트의 config.json 을 수정했다.
    rename_map = (
        resolve_rename_map(params["pretrained_path"]) if params.get("pretrained_path") else ""
    )
    args = build_train_args(params, rename_map=rename_map)
    env = {"ACCELERATE_MIXED_PRECISION": amp} if amp and amp != "off" else {}
    return {"args": args, "command": " ".join(args), "env": env}


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
