"""
HuggingFace Hub 클라이언트.
모델/데이터셋 검색 및 다운로드.
"""

import asyncio
import logging
from functools import partial

from huggingface_hub import HfApi, snapshot_download

from app.core import policies
from app.core.config import settings

logger = logging.getLogger(__name__)

# HF API 클라이언트 — **이 저장소의 유일한 인스턴스**.
# 두 곳에 있으면 endpoint 설정이 한쪽에만 걸려 나머지가 조용히 huggingface.co 를 본다.
# 사내 자체 Hub 로 옮길 때 여기와 `HF_ENDPOINT` 환경변수만 보면 된다 (ROADMAP 참고).
_api = HfApi(endpoint=settings.hf_endpoint or None)


# ⚠ **`direction` 은 이제 없다.** huggingface_hub 1.x 에서 빠졌고 내림차순이
# 기본이다. 예전 코드는 모델 쪽에만 `except TypeError` 폴백을 달아 넘겼는데,
# 그 폴백은 `sort` 까지 통째로 버려서 **정렬이 조용히 사라졌다.** 그리고
# 데이터셋 쪽에는 폴백조차 없어서 검색이 **500 으로 죽었다** — 화면에는
# "결과 없음" 으로만 보였다.
_SORT = "downloads"


def get_api() -> HfApi:
    """다른 모듈이 HfApi 를 직접 만들지 않도록 여기서 받아 쓴다."""
    return _api

# 다운로드 진행 상태 추적
_download_status: dict[str, dict] = {}


def _search_models(query: str = "", author: str = "lerobot", limit: int = 30) -> list[dict]:
    results = []
    models = _api.list_models(
        search=query, author=author or None, limit=limit, sort=_SORT
    )
    for model in models:
        tags = model.tags or []
        # 태그에서 정보 추출
        dataset_tags = [t.removeprefix("dataset:") for t in tags if t.startswith("dataset:")]
        policy_tags = [t.removesuffix("-policy") for t in tags if t.endswith("-policy")]
        model_name = (model.id or "").lower().split("/")[-1]

        # policy_type: 태그 → 이름에서 유추 (레지스트리에서 파생, 긴 이름 우선)
        if not policy_tags:
            guessed = policies.guess_from_name(model_name)
            if guessed:
                policy_tags = [guessed]

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
        # (레지스트리에서 파생. 이전에는 이 dict 두 개가 루프 안에 있어 모델마다 재생성됐다)
        if not base_model and not is_base and policy_tags:
            spec = policies.POLICIES.get(policy_tags[0], {})
            policy_base = spec.get("policy_base")
            vlm_base = spec.get("vlm_base")
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
        search=query, author=author or None, limit=limit, sort=_SORT
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
