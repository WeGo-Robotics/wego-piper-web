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
    try:
        models = _api.list_models(
            search=query, author=author or None, limit=limit, sort="downloads", direction=-1
        )
    except TypeError:
        models = _api.list_models(
            search=query, author=author or None, limit=limit,
        )
    for model in models:
        tags = model.tags or []
        # 태그에서 정보 추출
        dataset_tags = [t.removeprefix("dataset:") for t in tags if t.startswith("dataset:")]
        policy_tags = [t.removesuffix("-policy") for t in tags if t.endswith("-policy")]
        model_name = (model.id or "").lower().split("/")[-1]

        # policy_type: 태그 → 이름에서 유추
        if not policy_tags:
            for pt in ("smolvla", "act", "diffusion", "pi0", "pi05", "vqbet", "tdmpc", "sac", "rtc"):
                if pt in model_name:
                    policy_tags = [pt]
                    break

        is_base = "base" in model_name.split("_") or (not policy_tags and not dataset_tags)

        # card_data에서 추가 정보
        base_model = None
        card_datasets = None
        try:
            cd = model.card_data
            if cd:
                base_model = getattr(cd, "base_model", None)
                card_datasets = getattr(cd, "datasets", None)
        except Exception:
            pass

        # base_model이 없으면 policy_type에서 잘 알려진 베이스 모델 추론
        # policy base = lerobot 체크포인트, vlm base = VLM backbone
        _KNOWN_POLICY_BASES = {
            "smolvla": "lerobot/smolvla_base",
            "act": None,
            "diffusion": None,
            "pi0": "lerobot/pi0_base",
            "pi05": "lerobot/pi05_base",
            "vqbet": None,
            "tdmpc": None,
        }
        _KNOWN_VLM_BASES = {
            "smolvla": "HuggingFaceTB/SmolVLM2-500M-Video-Instruct",
            "pi0": "google/paligemma-3b-pt-224",
            "pi05": "google/paligemma-3b-pt-224",
        }
        if not base_model and not is_base and policy_tags:
            pt = policy_tags[0]
            policy_base = _KNOWN_POLICY_BASES.get(pt)
            vlm_base = _KNOWN_VLM_BASES.get(pt)
            if policy_base and vlm_base:
                base_model = f"{policy_base} (VLM: {vlm_base})"
            elif policy_base:
                base_model = policy_base
            elif vlm_base:
                base_model = f"VLM: {vlm_base}"

        results.append({
            "repo_id": model.id,
            "author": model.author,
            "downloads": model.downloads,
            "last_modified": model.last_modified.isoformat() if model.last_modified else None,
            "tags": tags,
            "pipeline_tag": model.pipeline_tag,
            "base_model": base_model,
            "datasets": card_datasets or dataset_tags or [],
            "policy_type": policy_tags[0] if policy_tags else None,
            "is_base": is_base,
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
    """동기 다운로드 (스레드에서 실행). HF 캐시 구조로 저장."""
    _download_status[repo_id] = {"status": "downloading", "progress": 0}
    try:
        path = snapshot_download(
            repo_id=repo_id,
            repo_type=repo_type,
            # local_dir 미지정 → HF 기본 캐시 (models--org--name/snapshots/hash/)
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
