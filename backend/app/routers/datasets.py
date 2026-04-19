import logging

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
from app.services.process_manager import process_manager

logger = logging.getLogger(__name__)

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
