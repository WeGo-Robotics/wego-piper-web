from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings
from app.services import hub_client

router = APIRouter(prefix="/api/hub", tags=["hub"])


@router.get("/models")
async def list_hub_models(q: str = "", author: str = "lerobot", limit: int = 30):
    return await hub_client.search_models(q, author, limit)


@router.get("/models/{repo_id:path}")
async def hub_model_detail(repo_id: str):
    return await hub_client.get_model_info(repo_id)


@router.get("/datasets")
async def list_hub_datasets(q: str = "", author: str = "lerobot", limit: int = 30):
    return await hub_client.search_datasets(q, author, limit)


@router.get("/datasets/{repo_id:path}")
async def hub_dataset_detail(repo_id: str):
    return await hub_client.get_dataset_info(repo_id)


class DownloadRequest(BaseModel):
    repo_id: str
    repo_type: str = "model"  # "model" or "dataset"


@router.post("/download")
async def start_download(body: DownloadRequest):
    local_dir = str(
        settings.models_dir if body.repo_type == "model" else settings.datasets_dir
    )
    await hub_client.start_download(body.repo_id, body.repo_type, local_dir)
    return {"status": "started", "repo_id": body.repo_id}


@router.get("/download/status")
async def download_status(repo_id: str):
    return hub_client.get_download_status(repo_id)
