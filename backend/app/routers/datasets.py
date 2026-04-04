from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.cli_mapping import build_edit_dataset_args
from app.services.dataset_scanner import (
    scan_datasets,
    get_dataset,
    delete_dataset,
    check_disk_usage,
)
from app.services.process_manager import process_manager

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
