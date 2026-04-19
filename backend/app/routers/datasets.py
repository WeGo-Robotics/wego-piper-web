import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.cli_mapping import build_edit_dataset_args
from app.services.dataset_scanner import (
    scan_datasets,
    get_dataset,
    delete_dataset,
    check_disk_usage,
    find_dataset_path,
)
from app.services.process_manager import process_manager, ProcessManager

logger = logging.getLogger(__name__)

# Hub 업로드 전용 ProcessManager (추론/학습과 독립)
_upload_pm = ProcessManager()


def _find_hf_cli() -> str:
    """huggingface-cli 경로 탐색. 설정 → grpc_python env → conda envs → PATH."""
    from app.core.config import settings
    import shutil, glob
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
    try:
        args = build_edit_dataset_args(dataset_id, body.operation, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))

    await process_manager.start(args)
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
    if _upload_pm.state.value in ("running", "starting"):
        raise HTTPException(409, "업로드가 이미 진행 중입니다.")

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


@router.get("/upload-status")
async def upload_status():
    """업로드 진행 상태."""
    return {"state": _upload_pm.state.value, "pid": _upload_pm.pid}


@router.post("/upload-stop")
async def upload_stop():
    """업로드 중지."""
    await _upload_pm.stop()
    return {"status": "stopped"}


@router.post("/{dataset_id:path}/decode-cache")
async def create_decode_cache(dataset_id: str):
    """데이터셋의 mp4 영상을 프레임별 jpg로 디코딩 캐시 생성."""
    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "Dataset not found")
    if _upload_pm.state.value in ("running", "starting"):
        raise HTTPException(409, "다른 작업이 진행 중입니다.")

    from app.core.config import settings
    # LeRobot 공식 캐시 형식: images/{key}/episode-{ep:06d}/frame-{frame:06d}.png
    script = (
        "import cv2, json\n"
        "from pathlib import Path\n"
        "import pyarrow.parquet as pq\n"
        f"ds = Path('{ds_path}')\n"
        "info = json.loads((ds / 'meta/info.json').read_text())\n"
        "n_eps = info.get('total_episodes', 0)\n"
        "vid_keys = [k for k in info.get('features', {}) if k.startswith('observation.images.')]\n"
        "print(f'Dataset: {n_eps} episodes, {len(vid_keys)} cameras: {vid_keys}', flush=True)\n"
        "# 에피소드별 프레임 수 확인\n"
        "ep_files = sorted((ds / 'meta/episodes').rglob('*.parquet'))\n"
        "ep_lengths = {}\n"
        "if ep_files:\n"
        "    for f in ep_files:\n"
        "        df = pq.read_table(f).to_pandas().reset_index()\n"
        "        for _, row in df.iterrows():\n"
        "            ep_lengths[int(row['episode_index'])] = int(row['length'])\n"
        "for vk in vid_keys:\n"
        "    vid_path = ds / info.get('video_path', 'videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4').format(video_key=vk, chunk_index=0, file_index=0)\n"
        "    if not vid_path.exists():\n"
        "        print(f'  SKIP {vk}: {vid_path} not found', flush=True)\n"
        "        continue\n"
        "    # 이미 캐시가 있으면 스킵\n"
        "    first_ep_dir = ds / 'images' / vk / 'episode-000000'\n"
        "    if first_ep_dir.exists() and any(first_ep_dir.iterdir()):\n"
        "        print(f'  SKIP {vk}: cache exists', flush=True)\n"
        "        continue\n"
        "    cap = cv2.VideoCapture(str(vid_path))\n"
        "    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))\n"
        "    print(f'  {vk}: {total_frames} frames from {vid_path.name}', flush=True)\n"
        "    frame_global = 0\n"
        "    for ep_idx in range(n_eps):\n"
        "        ep_len = ep_lengths.get(ep_idx, 0)\n"
        "        if ep_len == 0:\n"
        "            continue\n"
        "        out_dir = ds / 'images' / vk / f'episode-{ep_idx:06d}'\n"
        "        out_dir.mkdir(parents=True, exist_ok=True)\n"
        "        for fi in range(ep_len):\n"
        "            ret, frame = cap.read()\n"
        "            if not ret:\n"
        "                break\n"
        "            fpath = out_dir / f'frame-{fi:06d}.png'\n"
        "            cv2.imwrite(str(fpath), frame)\n"
        "            frame_global += 1\n"
        "        if (ep_idx + 1) % 10 == 0 or ep_idx == n_eps - 1:\n"
        "            print(f'    {vk}: episode {ep_idx+1}/{n_eps} ({frame_global}/{total_frames} frames)', flush=True)\n"
        "    cap.release()\n"
        "print('Decode cache complete', flush=True)\n"
    )
    args = [settings.grpc_python, "-u", "-c", script]
    await _upload_pm.start(args)
    return {"status": "started", "dataset_id": dataset_id}


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
    new_lines = [l for l in lines if not l.startswith("PIPER_HF_CLI=")]
    if body.path:
        new_lines.append(f"PIPER_HF_CLI={body.path}")
    env_path.write_text("\n".join(new_lines) + "\n")
    # 런타임 설정도 갱신
    from app.core.config import settings
    settings.hf_cli = body.path
    return {"status": "ok", "path": body.path, "resolved": _find_hf_cli()}
