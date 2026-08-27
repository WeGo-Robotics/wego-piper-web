import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.cli_mapping import build_edit_dataset_args
from app.services.dataset_scanner import (
    scan_datasets,
    get_dataset,
    delete_dataset,
    check_disk_usage,
    find_dataset_path,
)
from app.services.dataset_jobs import edit_pm as _edit_pm, upload_pm as _upload_pm
import asyncio

from app.services.exclusivity import Activity, require_idle

logger = logging.getLogger(__name__)


def _find_hf_cli() -> str:
    """huggingface-cli 경로 탐색. 설정 → grpc_python env → conda envs → PATH."""
    from app.core.config import settings
    import glob
    import shutil
    if settings.hf_cli and Path(settings.hf_cli).exists():
        return settings.hf_cli
    candidates = [
        str(Path(settings.grpc_python).parent / "huggingface-cli"),
        *glob.glob(str(Path(settings.grpc_python).parents[1] / "envs" / "*/bin/huggingface-cli")),
    ]
    return next((c for c in candidates if Path(c).exists()), shutil.which("huggingface-cli") or "")

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.get("")
async def list_datasets():
    return scan_datasets()


@router.get("/disk-usage")
async def disk_usage():
    return check_disk_usage()


# ⚠ 아래 고정 경로들은 `/{dataset_id:path}` **위에** 있어야 한다.
# catch-all 이 먼저 매칭되면 `GET /api/datasets/upload-status` 가
# "Dataset not found" 404 가 된다 — 실제로 그 상태였다.

@router.get("/upload-status")
async def upload_status():
    """업로드 진행 상태."""
    return {"state": _upload_pm.state.value, "pid": _upload_pm.pid}


@router.post("/upload-stop")
async def upload_stop():
    """업로드 중지."""
    await _upload_pm.stop()
    return {"status": "stopped"}


@router.get("/hf-cli")
async def get_hf_cli():
    """huggingface-cli 경로 조회."""
    from app.core.config import settings
    resolved = _find_hf_cli()
    return {"configured": settings.hf_cli, "resolved": resolved}


class HfCliRequest(BaseModel):
    path: str


@router.post("/hf-cli")
async def set_hf_cli(body: HfCliRequest):
    """huggingface-cli 경로 설정 (.env에 저장)."""
    if body.path and not Path(body.path).exists():
        raise HTTPException(400, f"경로가 존재하지 않습니다: {body.path}")
    # .env 파일에 PIPER_HF_CLI 추가/수정
    env_path = Path(__file__).resolve().parents[2] / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = [line for line in lines if not line.startswith("PIPER_HF_CLI=")]
    if body.path:
        new_lines.append(f"PIPER_HF_CLI={body.path}")
    env_path.write_text("\n".join(new_lines) + "\n")
    # 런타임 설정도 갱신
    from app.core.config import settings
    settings.hf_cli = body.path
    return {"status": "ok", "path": body.path, "resolved": _find_hf_cli()}


# ⚠ 이 GET 은 아래 `GET /{dataset_id:path}` (상세) 보다 **위**에 있어야 한다.
# 같은 메서드의 catch-all 이 먼저 등록되면 프레임 경로 전체가 상세 응답으로 먹힌다
# (`/upload-status` 가 실제로 겪은 사고와 같은 종류다).
@router.get("/{dataset_id:path}/episodes/{episode}/frames/{cam}/{frame}")
async def episode_frame(dataset_id: str, episode: int, cam: str, frame: int):
    """디코딩 캐시에서 프레임 이미지 서빙 (jpg 우선, png 폴백).

    프레임 번호는 **에피소드 내 상대** 번호다. 캐시가 없으면 404 —
    UI 는 decode-cache 생성 버튼을 노출한다 (feature/episode-editor.md §3).
    """
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    key = cam if cam.startswith("observation.images.") else f"observation.images.{cam}"
    ep_dir = ds_path / "images" / key / f"episode-{episode:06d}"
    for ext in ("jpg", "png"):
        path = ep_dir / f"frame-{frame:06d}.{ext}"
        if path.exists():
            # 캐시 프레임은 불변 — 재생 중 같은 프레임을 브라우저가 다시 받지 않게 한다
            return FileResponse(path, headers={"Cache-Control": "public, max-age=86400, immutable"})
    raise HTTPException(404, "디코딩 캐시에 프레임이 없습니다 — decode-cache 를 먼저 생성하세요")


@router.get("/{dataset_id:path}/videos/{cam}/{chunk}/{file}")
async def episode_video(dataset_id: str, cam: str, chunk: int, file: int):
    """chunk mp4 원본 서빙 — Range 는 FileResponse 가 처리한다 (206, 실측 확인).

    에피소드가 아니라 **파일 단위**다: 같은 chunk 의 에피소드들이 브라우저 캐시를
    공유한다. 에피소드 경계(`videos/{key}/from·to_timestamp`)는 상세 응답의
    episodes 레코드에 이미 들어 있어 프론트가 currentTime 으로 진입한다
    (feature/episode-editor.md §3 — 뷰어 기본은 동영상, 프레임 캐시는 폴백·편집용).
    """
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    key = cam if cam.startswith("observation.images.") else f"observation.images.{cam}"
    # info.json 의 video_path 템플릿 (scripts/decode_cache.py 와 같은 규칙)
    template = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
    info_path = ds_path / "meta" / "info.json"
    if info_path.exists():
        try:
            template = json.loads(info_path.read_text()).get("video_path") or template
        except Exception:
            pass
    path = (ds_path / template.format(video_key=key, chunk_index=chunk, file_index=file)).resolve()
    if not str(path).startswith(str(ds_path.resolve())) or not path.exists():
        raise HTTPException(404, "비디오 파일이 없습니다")
    return FileResponse(path, media_type="video/mp4")


@router.get("/{dataset_id:path}/consistency")
async def dataset_consistency(dataset_id: str):
    """개수·목록·프레임이 같은 이야기를 하는지.

    끊긴 녹화는 `info.json` 만 크고 `meta/episodes` 가 작다 — LeRobot 이 에피소드
    메타를 10개씩 모아 쓰기 때문이다. 화면은 개수를 `info.json` 에서 읽으므로
    **개수는 뜨는데 목록에는 없는** 상태가 되고, 그걸 본 사용자는 데이터가
    날아간 줄 알고 지운다. 실제로 그렇게 지워졌다.
    """
    from app.services.dataset_repair import check

    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    return check(ds_path)


@router.post("/{dataset_id:path}/repair-index")
async def dataset_repair_index(dataset_id: str, apply: bool = False):
    """`data/` 에 남은 프레임으로 `meta/episodes` 를 다시 짓는다.

    기본은 미리보기(`apply=false`) — 무엇이 되살아나는지 먼저 보여주고 나서 쓴다.
    쓸 때는 원본을 `.bak` 으로 남긴다.
    """
    from app.services.dataset_repair import rebuild_index

    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    # ⚠ 파일을 쓰는 동안 녹화·편집이 같은 데이터셋을 만지면 안 된다
    if apply:
        require_idle(Activity.DATASET_EDIT)
    out = await asyncio.to_thread(rebuild_index, ds_path, dry_run=not apply)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error", "복구 실패"))
    return out


# ⚠ **아래 catch-all 보다 위에 있어야 한다** — 같은 메서드의 `:path` 가 먼저
#   등록되면 위 경로들이 전부 상세 응답으로 먹힌다 (`/upload-status` 전례).
@router.get("/{dataset_id:path}")
async def dataset_detail(dataset_id: str):
    ds = get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds


@router.delete("/{dataset_id:path}")
async def remove_dataset(dataset_id: str):
    if not delete_dataset(dataset_id):
        raise HTTPException(404, "Dataset not found")
    return {"status": "deleted"}


class EditDatasetRequest(BaseModel):
    operation: str  # delete_episodes, split, merge, remove_features, info
    params: dict = {}


@router.post("/{dataset_id:path}/edit")
async def edit_dataset(dataset_id: str, body: EditDatasetRequest):
    require_idle(Activity.DATASET_EDIT)
    try:
        args = build_edit_dataset_args(dataset_id, body.operation, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))

    await _edit_pm.start(args)
    return {"status": "started", "command": args}


class UpdateTaskRequest(BaseModel):
    episode_indices: list[int]
    task: str


@router.post("/{dataset_id:path}/update-task")
async def update_episode_task(dataset_id: str, body: UpdateTaskRequest):
    """에피소드별 task 텍스트 변경."""
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")

    import pyarrow.parquet as pq
    import pyarrow as pa
    import pandas as pd

    # 1. tasks.parquet에서 기존 task 목록 로드
    tasks_path = ds_path / "meta" / "tasks.parquet"
    tasks_jsonl = ds_path / "meta" / "tasks.jsonl"
    if tasks_path.exists():
        tasks_df = pq.read_table(tasks_path).to_pandas()
    elif tasks_jsonl.exists():
        tasks_df = pd.DataFrame([
            {"task_index": int(d.get("task_index", i)), "task": d["task"]}
            for i, d in enumerate(
                json.loads(line) for line in tasks_jsonl.read_text().strip().split("\n") if line
            )
        ])
    else:
        tasks_df = pd.DataFrame(columns=["task_index", "task"])

    # 2. 새 task가 없으면 추가
    existing_tasks = dict(zip(tasks_df["task"], tasks_df["task_index"]))
    if body.task not in existing_tasks:
        new_idx = int(tasks_df["task_index"].max() + 1) if len(tasks_df) > 0 else 0
        tasks_df = pd.concat([tasks_df, pd.DataFrame([{"task_index": new_idx, "task": body.task}])], ignore_index=True)
        existing_tasks[body.task] = new_idx
    target_task_index = existing_tasks[body.task]

    # 3. tasks.parquet 저장
    tasks_df = tasks_df.reset_index(drop=True)
    if "task" in tasks_df.columns and tasks_df.index.name == "task":
        tasks_df = tasks_df.reset_index()
    pq.write_table(pa.Table.from_pandas(tasks_df.set_index("task") if "task" in tasks_df.columns else tasks_df), tasks_path)

    # 4. data parquet에서 해당 에피소드의 task_index 변경
    data_files = sorted((ds_path / "data").rglob("*.parquet"))
    updated_frames = 0
    for f in data_files:
        table = pq.read_table(f)
        df = table.to_pandas()
        mask = df["episode_index"].isin(body.episode_indices)
        if mask.any():
            df.loc[mask, "task_index"] = target_task_index
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), f)
            updated_frames += mask.sum()

    # 5. episodes parquet에서 tasks 컬럼 갱신
    ep_dir = ds_path / "meta" / "episodes"
    if ep_dir.is_dir():
        for f in sorted(ep_dir.rglob("*.parquet")):
            table = pq.read_table(f)
            df = table.to_pandas()
            if "tasks" in df.columns:
                for idx in body.episode_indices:
                    row_mask = df["episode_index"] == idx
                    if row_mask.any():
                        df.loc[row_mask, "tasks"] = df.loc[row_mask, "tasks"].apply(
                            lambda x: [body.task] if isinstance(x, list) else body.task
                        )
                pq.write_table(pa.Table.from_pandas(df, preserve_index=False), f)

    # 6. info.json의 total_tasks 갱신
    info_path = ds_path / "meta" / "info.json"
    if info_path.exists():
        info = json.loads(info_path.read_text())
        info["total_tasks"] = len(tasks_df)
        info_path.write_text(json.dumps(info, indent=4))

    logger.info("Updated task for episodes %s → '%s' (%d frames)", body.episode_indices, body.task, updated_frames)
    return {"status": "ok", "task_index": target_task_index, "updated_frames": int(updated_frames)}


class UploadRequest(BaseModel):
    private: bool = False
    tags: list[str] = []


@router.post("/{dataset_id:path}/upload")
async def upload_to_hub(dataset_id: str, body: UploadRequest):
    """로컬 데이터셋을 HuggingFace Hub에 업로드."""
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    require_idle(Activity.UPLOAD)

    hf_cli = _find_hf_cli()
    if not hf_cli:
        raise HTTPException(500, "huggingface-cli를 찾을 수 없습니다. 설정에서 경로를 지정하세요.")
    # 파일 수에 따라 upload / upload-large-folder 선택
    file_count = sum(1 for _ in ds_path.rglob("*") if _.is_file())
    if file_count > 1000:
        args = [hf_cli, "upload-large-folder", dataset_id, str(ds_path), "--repo-type=dataset"]
    else:
        args = [hf_cli, "upload", dataset_id, str(ds_path), ".", "--repo-type=dataset"]
    if body.private:
        args.append("--private")
    await _upload_pm.start(args)
    return {"status": "started", "dataset_id": dataset_id, "pid": _upload_pm.pid}


class DecodeCacheRequest(BaseModel):
    """기본값(PNG 원본 해상도) = LeRobot 공식 캐시 형식. 뷰어는 jpeg+320 으로 요청한다."""
    format: Literal["png", "jpeg"] = "png"
    max_dim: int = 0     # 긴 변 축소 (0 = 원본)
    quality: int = 85    # JPEG 품질


@router.post("/{dataset_id:path}/decode-cache")
async def create_decode_cache(dataset_id: str, body: DecodeCacheRequest | None = None):
    """데이터셋 mp4 → 프레임 이미지 캐시 생성 ([scripts/decode_cache.py]).

    인라인 스크립트를 파일로 뺐다 — 멀티 chunk 데이터셋에서 에피소드가 조용히
    빠지던 것을 고치면서다 (feature/episode-editor.md §3c). 두 포맷은 같은
    디렉토리에 공존하고, 프레임 서빙은 jpg 우선으로 읽는다.
    """
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    # 디코딩 캐시는 업로드와 같은 ProcessManager 를 공유한다 (둘 다 느린 디스크 작업)
    require_idle(Activity.UPLOAD)

    from app.core.config import settings
    opts = body or DecodeCacheRequest()
    script = Path(__file__).resolve().parents[2] / "scripts" / "decode_cache.py"
    args = [settings.grpc_python, "-u", str(script), str(ds_path),
            "--format", opts.format, "--max-dim", str(opts.max_dim),
            "--quality", str(opts.quality)]
    await _upload_pm.start(args)
    return {"status": "started", "dataset_id": dataset_id, "format": opts.format}


@router.post("/{dataset_id:path}/decode-cache/delete")
async def delete_decode_cache(dataset_id: str):
    """데이터셋의 디코딩 캐시(프레임 jpg) 삭제."""
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    import shutil
    deleted = 0
    # 1. videos/.../file-000/ (mp4 옆 프레임 디렉토리)
    videos_dir = ds_path / "videos"
    if videos_dir.exists():
        for mp4 in videos_dir.rglob("*.mp4"):
            cache_dir = mp4.parent / mp4.stem
            if cache_dir.is_dir():
                deleted += sum(1 for _ in cache_dir.rglob("*") if _.is_file())
                shutil.rmtree(cache_dir)
    # 2. images/ (LeRobot 공식 디코딩 캐시)
    images_dir = ds_path / "images"
    if images_dir.exists():
        deleted += sum(1 for _ in images_dir.rglob("*") if _.is_file())
        shutil.rmtree(images_dir)
    return {"status": "ok", "deleted_files": deleted}
