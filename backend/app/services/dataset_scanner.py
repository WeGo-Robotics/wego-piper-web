"""
로컬 데이터셋 스캔.
1. HuggingFace Hub 캐시: datasets--org--name/snapshots/hash/
2. LeRobot 캐시: org/name/ (meta/info.json 직접 포함)
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.core.hf_layout import (
    dirname_from_repo_id,
    latest_snapshot,
    repo_id_from_dirname,
    resolve_info_json,
)

logger = logging.getLogger(__name__)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _parse_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return {}






def _scan_hub_datasets(datasets_dir: Path) -> list[dict]:
    """HuggingFace Hub 캐시 스캔 (datasets--org--name/snapshots/hash/)."""
    if not datasets_dir.exists():
        return []
    results = []
    for candidate in datasets_dir.iterdir():
        if not candidate.is_dir():
            continue
        repo_id = repo_id_from_dirname(candidate.name, "datasets")
        if not repo_id:
            continue
        snapshot = latest_snapshot(candidate)
        if not snapshot:
            continue
        info_path = resolve_info_json(snapshot)
        if not info_path:
            continue
        meta = _parse_meta(info_path)
        stat = snapshot.stat()
        results.append({
            "id": repo_id,
            "path": str(snapshot),
            "source": "hub",
            "total_episodes": meta.get("total_episodes", 0),
            "total_frames": meta.get("total_frames", 0),
            "fps": meta.get("fps"),
            "features": meta.get("features", {}),
            "size_bytes": _dir_size(snapshot),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return results


def _scan_lerobot_datasets(lerobot_dir: Path) -> list[dict]:
    """LeRobot 캐시 스캔 (org/name/ — meta/info.json 직접 포함)."""
    if not lerobot_dir.exists():
        return []
    results = []
    for org_dir in lerobot_dir.iterdir():
        if not org_dir.is_dir():
            continue
        for ds_dir in org_dir.iterdir():
            if not ds_dir.is_dir():
                continue
            info_path = resolve_info_json(ds_dir)
            if not info_path:
                continue
            repo_id = f"{org_dir.name}/{ds_dir.name}"
            meta = _parse_meta(info_path)
            stat = ds_dir.stat()
            results.append({
                "id": repo_id,
                "path": str(ds_dir),
                "source": "lerobot",
                "total_episodes": meta.get("total_episodes", 0),
                "total_frames": meta.get("total_frames", 0),
                "fps": meta.get("fps"),
                "features": meta.get("features", {}),
                "size_bytes": _dir_size(ds_dir),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return results


def scan_datasets() -> list[dict]:
    results = _scan_hub_datasets(settings.datasets_dir)
    results += _scan_lerobot_datasets(settings.lerobot_dir)
    # 중복 제거 (같은 repo_id면 최신 것만)
    seen: dict[str, dict] = {}
    for ds in results:
        existing = seen.get(ds["id"])
        if existing is None or ds["modified"] > existing["modified"]:
            seen[ds["id"]] = ds
    results = list(seen.values())
    results.sort(key=lambda d: d["modified"], reverse=True)
    return results


def _find_dataset_dir(dataset_id: str) -> tuple[Path, str] | None:
    """dataset_id로 실제 디렉토리와 source를 찾는다."""
    parts = dataset_id.split("/", 1)
    if len(parts) != 2:
        return None
    org, name = parts

    # 1. LeRobot 캐시 (org/name/)
    lerobot_path = settings.lerobot_dir / org / name
    if lerobot_path.exists() and (lerobot_path / "meta" / "info.json").exists():
        return lerobot_path, "lerobot"

    # 2. Hub 캐시 (datasets--org--name/snapshots/hash/)
    hub_dir = settings.datasets_dir / (dirname_from_repo_id(f"{org}/{name}", "datasets") or "")
    snapshot = latest_snapshot(hub_dir)
    if snapshot:
        return snapshot, "hub"

    return None


def find_dataset_path(dataset_id: str) -> Path | None:
    """dataset_id → 실제 디렉토리 경로."""
    result = _find_dataset_dir(dataset_id)
    return result[0] if result else None


def episode_meta(dataset_id: str, episode: int) -> tuple[Path, dict, dict] | None:
    """(데이터셋 경로, info.json meta, 에피소드 레코드) — 상세 응답의 부분집합.

    `get_dataset` 은 `_dir_size`(전체 트리 순회)까지 계산한다 — 에피소드 하나의
    비디오 위치(chunk/file/timestamp)만 필요한 호출자(YOLO 캡처의 비디오 폴백)용
    가벼운 경로다. 못 찾으면 None.
    """
    result = _find_dataset_dir(dataset_id)
    if not result:
        return None
    ds_path, _ = result
    info_path = resolve_info_json(ds_path)
    meta = _parse_meta(info_path) if info_path else {}
    episodes = _read_meta_table(ds_path / "meta" / "episodes.jsonl", ds_path / "meta" / "episodes")
    rec = next(
        (e for e in _sanitize_records(episodes)
         if int(e.get("episode_index", e.get("index", -1))) == episode),
        None,
    )
    return (ds_path, meta, rec) if rec is not None else None


def _sanitize_value(v):
    """numpy/pandas 타입을 JSON 직렬화 가능한 타입으로 변환."""
    import numpy as np
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, dict):
        return {k2: _sanitize_value(v2) for k2, v2 in v.items()}
    if isinstance(v, list):
        return [_sanitize_value(x) for x in v]
    return v


def _sanitize_records(records: list[dict]) -> list[dict]:
    return [{k: _sanitize_value(v) for k, v in row.items()} for row in records]


def _read_meta_table(jsonl_path: Path, parquet_path: Path) -> list[dict]:
    """jsonl 또는 parquet에서 메타 테이블 읽기."""
    # jsonl 우선
    if jsonl_path.exists():
        rows = []
        for line in jsonl_path.read_text().strip().split("\n"):
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows
    # parquet (단일 파일 또는 디렉토리)
    pq_file = parquet_path
    if parquet_path.is_dir():
        # episodes/chunk-000/file-000.parquet 구조
        pq_files = sorted(parquet_path.rglob("*.parquet"))
        if not pq_files:
            return []
        try:
            import pyarrow.parquet as pq
            tables = [pq.read_table(f) for f in pq_files]
            import pyarrow as pa
            merged = pa.concat_tables(tables)
            df = merged.to_pandas().reset_index()
            # stats 컬럼 제거 (numpy nested array → JSON 직렬화 불가)
            df = df[[c for c in df.columns if not c.startswith("stats/")]]
            return _sanitize_records(df.to_dict("records"))
        except Exception as e:
            logger.warning("Failed to read parquet dir %s: %s", parquet_path, e)
            return []
    elif pq_file.exists():
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(pq_file)
            return _sanitize_records(table.to_pandas().reset_index().to_dict("records"))
        except Exception as e:
            logger.warning("Failed to read parquet %s: %s", pq_file, e)
            return []
    return []


def get_dataset(dataset_id: str) -> dict | None:
    result = _find_dataset_dir(dataset_id)
    if not result:
        return None
    ds_path, source = result

    info_path = resolve_info_json(ds_path)
    meta = _parse_meta(info_path) if info_path else {}

    # 에피소드 목록 (jsonl → parquet fallback)
    episodes = _read_meta_table(ds_path / "meta" / "episodes.jsonl", ds_path / "meta" / "episodes")

    # tasks (jsonl → parquet fallback)
    tasks = _read_meta_table(ds_path / "meta" / "tasks.jsonl", ds_path / "meta" / "tasks.parquet")

    return {
        "id": dataset_id,
        "path": str(ds_path),
        "source": source,
        "total_episodes": meta.get("total_episodes", 0),
        "total_frames": meta.get("total_frames", 0),
        "fps": meta.get("fps"),
        "features": meta.get("features", {}),
        "episodes": episodes,
        "tasks": tasks,
        "size_bytes": _dir_size(ds_path),
        "modified": datetime.fromtimestamp(ds_path.stat().st_mtime).isoformat(),
    }


def delete_dataset(dataset_id: str) -> bool:
    result = _find_dataset_dir(dataset_id)
    if not result:
        return False
    ds_path, source = result
    # LeRobot 경로면 dataset 폴더 자체, Hub면 상위 datasets-- 폴더 삭제
    if source == "hub":
        target = ds_path.parent.parent  # snapshots/hash → datasets--org--name
    else:
        target = ds_path
    if not target.exists():
        return False
    shutil.rmtree(target)
    logger.info("Deleted dataset: %s", target)
    return True


def check_disk_usage() -> dict:
    datasets_dir = settings.datasets_dir
    models_dir = settings.models_dir

    datasets_size = 0
    models_size = 0

    if datasets_dir.exists():
        for d in datasets_dir.iterdir():
            if d.is_dir() and d.name.startswith("datasets--"):
                datasets_size += _dir_size(d)

    if models_dir.exists():
        for d in models_dir.iterdir():
            if d.is_dir() and d.name.startswith("models--"):
                models_size += _dir_size(d)

    total_gb = (datasets_size + models_size) / (1024**3)

    return {
        "datasets_bytes": datasets_size,
        "models_bytes": models_size,
        "total_gb": round(total_gb, 2),
        "warning": total_gb > settings.disk_warning_threshold_gb,
        "threshold_gb": settings.disk_warning_threshold_gb,
    }
