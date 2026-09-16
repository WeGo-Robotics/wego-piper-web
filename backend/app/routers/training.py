"""학습 API."""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.cli_mapping import apply_dim_overrides, build_train_args, resolve_rename_map
from app.core.config import settings
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
    # 가중치 제목·설명 — 시작 성공 직후 output_dir/piper_notes.json 사이드카로
    # 남는다 (제목 = name). CLI 인자가 아니다 — build_train_args 전에 뺀다.
    title: str = ""
    description: str = ""


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


def _train_env(amp: str, *, remote: bool = False) -> dict[str, str] | None:
    """학습 프로세스에 넣을 환경변수.

    AMP: lerobot_train.py는 accelerator.autocast()로 학습하며, 혼합정밀도는
    --policy.use_amp(학습 루프 미사용)가 아니라 이 환경변수로만 켜진다.

    ⚠ **원격이면 HF 토큰을 같이 보낸다.** 데이터셋을 받고 가중치를 올리는 데 둘 다
    필요한데 임대 서버에는 우리 토큰이 없다. `SSHRunner._build_script` 가 이걸
    스크립트 안에서 `export` 하고 그 스크립트가 tmux 안에서 실행되므로 학습
    프로세스까지 닿는다 (ssh 세션 env 로는 못 넘는다 — tmux 서버가 환경을 얼린다).

    ⚠ 토큰이 **남의 기계**로 간다. 원격 학습을 쓴다는 것이 곧 그 선택이다 —
    fine-grained 전용 토큰을 쓰는 것이 다음 단계다 (§10 결정 3).
    """
    env: dict[str, str] = {}
    if amp and amp != "off":
        env["ACCELERATE_MIXED_PRECISION"] = amp
    if remote:
        # ⚠ **토큰 파일만 보면 안 된다.** `HF_TOKEN` 환경변수로 로그인한 게이트웨이는
        #   파일이 없어서 조용히 빈손으로 원격에 보내게 된다 — 그러면 비공개 데이터셋
        #   다운로드와 종료 시 푸시가 **둘 다** 실패하고, 그 사실은 임대 GPU 가 이미
        #   돌기 시작한 뒤에야 드러난다. `get_token()` 이 env→파일 순서를 대신 푼다.
        from huggingface_hub import get_token

        tok = (get_token() or "").strip()
        if tok:
            env["HF_TOKEN"] = tok
        if settings.hf_endpoint:
            env["HF_ENDPOINT"] = settings.hf_endpoint
    return env or None


async def _require_push_permission(repo_id: str) -> None:
    """`--policy.repo_id` 를 쓰면 학습이 **끝나고 나서** Hub 로 올린다.

    ⚠ **그래서 지금 막아야 한다.** 토큰이 없거나 읽기 전용이면 몇 시간을 돌린 뒤
    마지막 단계에서 실패한다. 체크포인트는 디스크에 남지만 화면에는 실패로 뜨고,
    사람은 학습이 통째로 날아간 줄 안다.

    ⚠ **네트워크 문제로는 막지 않는다.** 토큰이 나쁜 것과 HF 가 안 닿는 것은
    다르다 — 사내망이 느리다고 학습을 못 걸게 하면 그게 더 나쁘다. 확인할 수
    없으면 통과시키고 로그에 남긴다.
    """
    import asyncio

    from app.services import hub_client

    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(hub_client.get_api().whoami), timeout=10)
    except Exception as e:
        logger.warning("HF 권한을 확인 못 했다 (학습은 진행): %s", e)
        return

    role = info.get("auth", {}).get("accessToken", {}).get("role", "")
    if not info.get("name"):
        raise HTTPException(400,
            f"HuggingFace 에 로그인되어 있지 않습니다. 학습이 끝난 뒤 "
            f"'{repo_id}' 로 업로드하다 실패합니다 — 설정 → 저장소에서 토큰을 "
            f"넣거나, 저장소 ID 를 비우고 로컬에만 남기세요.")
    # ⚠ `read` 일 때만 막는다. fine-grained 토큰은 role 이 달리 오는데,
    #   그걸 싸잡아 막으면 멀쩡한 토큰으로 학습을 못 건다.
    if role == "read":
        raise HTTPException(400,
            f"HuggingFace 토큰이 읽기 전용입니다({info['name']}). 학습이 끝난 뒤 "
            f"'{repo_id}' 로 업로드하다 실패합니다 — 설정 → 저장소에서 write 권한 "
            f"토큰으로 바꾸거나, 저장소 ID 를 비우세요.")


@router.post("/start")
async def start_training(body: TrainStartRequest):
    """학습 시작."""
    require_idle(Activity.TRAINING)

    params = body.model_dump(exclude_none=True)
    # amp는 CLI 인자가 아니라 환경변수로 주입 → params에서 분리
    amp = params.pop("amp", "bf16")
    # 제목·설명은 사이드카다 — 인자 빌더에 흘리지 않는다 (화이트리스트라 무시되긴
    # 하지만, 뜻이 다른 것을 같은 dict 에 두지 않는다)
    title = str(params.pop("title", "") or "").strip()
    description = str(params.pop("description", "") or "").strip()
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
    # ⚠ 몇 시간 뒤가 아니라 **지금** 막는다
    # ⚠ **원격이면 `policy_repo_id` 가 없으면 안 된다.** 없으면 `cli_mapping` 이
    #   `--policy.push_to_hub=false` 를 강제하는데, 로컬에선 맞고 임대 서버에서는
    #   **회수 경로를 지우는 설정**이다 — 학습은 멀쩡히 끝나고 가중치만 사라진다.
    #   그리고 §12-4 실측: 푸시는 **종료 시점 한 번뿐**이라 이 한 번이 유일한 기회다.
    if not params.get("policy_repo_id") and getattr(train_manager.runner, "is_remote", False):
        raise HTTPException(
            400, "원격 학습에는 가중치를 올릴 저장소(policy_repo_id)가 필요합니다 — "
                 "없으면 학습이 끝나도 결과를 가져올 수 없습니다.")
    if params.get("policy_repo_id"):
        await _require_push_permission(params["policy_repo_id"])

    rename_map = ""
    if params.get("pretrained_path"):
        rename_map = resolve_rename_map(params["pretrained_path"])
        apply_dim_overrides(
            params["pretrained_path"], body.state_dim, body.action_dim
        )

    # ⚠ **원격 학습은 로컬의 사실 세 가지가 안 통한다** (2026-09-16 실기, §11~§12).
    #   손으로 우회하던 것을 여기서 채운다.
    remote = getattr(train_manager.runner, "is_remote", False)
    args = build_train_args(
        params, rename_map=rename_map,
        # ⚠ 로컬 인터프리터의 절대경로는 임대 서버에 **없다.** 이미지의 venv 는
        #   `/opt/venv` 이고 그 경로는 SSH 로그인 셸 PATH 에도 없어서, 여기를 안 채우면
        #   원격에서 `No such file or directory` 로 즉사한다.
        python=settings.train_remote_python if remote else None,
    )

    try:
        await train_manager.start(
            args, total_steps=body.steps, output_dir=body.output_dir,
            env_extra=_train_env(amp, remote=remote),
        )
    except Exception as e:
        raise HTTPException(500, f"학습 시작 실패: {e}")

    # 사이드카는 **시작이 성공한 뒤** 쓴다 — 실패한 시작의 흔적을 남기지 않는다.
    # 시작 시점에 쓰는 이유: 학습이 중간에 멈추거나 게이트웨이가 재시작돼도
    # 이름·설명은 남아야 하고, 모델 페이지의 편집기가 언제든 고칠 수 있다.
    # ⚠ output_dir 이 비어 있으면(자동) LeRobot 이 고르는 경로를 여기서 모른다
    #   — 그때는 못 쓴다. 폼이 그렇게 안내한다.
    notes_written = False
    if (title or description) and body.output_dir:
        try:
            from pathlib import Path as _P

            from app.services.notes_sidecar import write_notes

            _P(body.output_dir).mkdir(parents=True, exist_ok=True)
            write_notes(_P(body.output_dir), kind="model",
                        name=title, description=description)
            notes_written = True
        except Exception as exc:
            logger.warning("가중치 제목·설명 사이드카 기록 실패 (%s): %s",
                           body.output_dir, exc)
    return {"status": "started", "pid": train_manager.runner.pid, "args": args,
            "notes_written": notes_written}


@router.post("/start-custom")
async def start_training_custom(body: TrainCustomRequest):
    """직접 편집한 CLI 인자로 학습 시작."""
    require_idle(Activity.TRAINING)
    if not body.args:
        raise HTTPException(400, "CLI 인자가 비어있습니다")

    # ⚠ **직접 편집한 인자에도 건다.** 여기로 오는 명령이 화면에서 만든 것과 같은
    #   실패를 겪는다 — 오히려 손으로 고친 쪽이 `push_to_hub` 를 끄는 걸 잊기 쉽다.
    repo = next((a.split("=", 1)[1] for a in body.args
                 if a.startswith("--policy.repo_id=")), "")
    pushes = repo and not any(
        a.replace(" ", "").lower() in ("--policy.push_to_hub=false", "--policy.push_to_hub=0")
        for a in body.args)
    if pushes:
        await _require_push_permission(repo)

    # ⚠ **원격 배선은 여기에도 건다.** 위 주석과 같은 이유다 — 손으로 고친 쪽이
    #   오히려 잊기 쉽다. 화면에서 만든 경로만 고치고 여기를 빼 두면, 같은 실패를
    #   "직접 편집" 으로 들어온 사람만 겪는다.
    remote = getattr(train_manager.runner, "is_remote", False)
    args = list(body.args)
    note = None
    if remote:
        if not repo:
            raise HTTPException(
                400, "원격 학습에는 --policy.repo_id 가 필요합니다 — "
                     "없으면 학습이 끝나도 결과를 가져올 수 없습니다.")
        # ⚠ 로컬 절대경로는 임대 서버에 없다. 다만 **사람이 일부러 쓴 경로는 건드리지
        #   않는다** — preview 가 넣어 준 로컬 인터프리터일 때만 바꾸고, 바꿨다는 사실을
        #   응답에 적는다. 조용히 덮어쓰면 "내가 쓴 것과 다른 게 돌았다" 가 된다.
        if args and args[0] == settings.grpc_python:
            args[0] = settings.train_remote_python
            note = f"원격 실행이라 인터프리터를 {settings.train_remote_python} 로 바꿨습니다"

    try:
        await train_manager.start(
            args, total_steps=body.total_steps, output_dir=body.output_dir,
            env_extra=_train_env(body.amp, remote=remote),
        )
    except Exception as e:
        raise HTTPException(500, f"학습 시작 실패: {e}")
    return {"status": "started", "pid": train_manager.runner.pid, "args": args, "note": note}


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
