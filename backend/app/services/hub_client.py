"""
HuggingFace Hub 클라이언트.
모델/데이터셋 검색 및 다운로드.
"""

import asyncio
import logging
from functools import partial

from huggingface_hub import HfApi, snapshot_download

logger = logging.getLogger(__name__)

_api = HfApi()

# 다운로드 진행 상태 추적
_download_status: dict[str, dict] = {}


def _search_models(query: str = "", author: str = "lerobot", limit: int = 30) -> list[dict]:
    results = []
    for model in _api.list_models(
        search=query, author=author or None, limit=limit, sort="downloads", direction=-1
    ):
        results.append({
            "repo_id": model.id,
            "author": model.author,
            "downloads": model.downloads,
            "last_modified": model.last_modified.isoformat() if model.last_modified else None,
            "tags": model.tags or [],
            "pipeline_tag": model.pipeline_tag,
        })
    return results


def _search_datasets(query: str = "", author: str = "lerobot", limit: int = 30) -> list[dict]:
    results = []
    for ds in _api.list_datasets(
        search=query, author=author or None, limit=limit, sort="downloads", direction=-1
    ):
        results.append({
            "repo_id": ds.id,
            "author": ds.author,
            "downloads": ds.downloads,
            "last_modified": ds.last_modified.isoformat() if ds.last_modified else None,
            "tags": ds.tags or [],
        })
    return results


def _get_model_info(repo_id: str) -> dict:
    info = _api.model_info(repo_id)
    siblings = info.siblings or []
    return {
        "repo_id": info.id,
        "author": info.author,
        "downloads": info.downloads,
        "tags": info.tags or [],
        "card_data": info.card_data.__dict__ if info.card_data else {},
        "files": [
            {"filename": s.rfilename, "size": s.size}
            for s in siblings
        ],
    }


def _get_dataset_info(repo_id: str) -> dict:
    info = _api.dataset_info(repo_id)
    siblings = info.siblings or []
    return {
        "repo_id": info.id,
        "author": info.author,
        "downloads": info.downloads,
        "tags": info.tags or [],
        "card_data": info.card_data.__dict__ if info.card_data else {},
        "files": [
            {"filename": s.rfilename, "size": s.size}
            for s in siblings
        ],
    }


def _download(repo_id: str, repo_type: str, local_dir: str) -> str:
    """동기 다운로드 (스레드에서 실행)."""
    _download_status[repo_id] = {"status": "downloading", "progress": 0}
    try:
        path = snapshot_download(
            repo_id=repo_id,
            repo_type=repo_type,
            local_dir=local_dir,
        )
        _download_status[repo_id] = {"status": "completed", "path": path}
        return path
    except Exception as e:
        _download_status[repo_id] = {"status": "error", "error": str(e)}
        raise


async def search_models(query: str = "", author: str = "lerobot", limit: int = 30) -> list[dict]:
    return await asyncio.get_event_loop().run_in_executor(
        None, partial(_search_models, query, author, limit)
    )


async def search_datasets(query: str = "", author: str = "lerobot", limit: int = 30) -> list[dict]:
    return await asyncio.get_event_loop().run_in_executor(
        None, partial(_search_datasets, query, author, limit)
    )


async def get_model_info(repo_id: str) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, partial(_get_model_info, repo_id)
    )


async def get_dataset_info(repo_id: str) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, partial(_get_dataset_info, repo_id)
    )


async def start_download(repo_id: str, repo_type: str, local_dir: str) -> None:
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, partial(_download, repo_id, repo_type, local_dir))


def get_download_status(repo_id: str) -> dict:
    return _download_status.get(repo_id, {"status": "not_found"})
