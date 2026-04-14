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


def _repo_id_from_dirname(dirname: str) -> str | None:
    """datasets--wego-hansu--piper_data → wego-hansu/piper_data"""
    if not dirname.startswith("datasets--"):
        return None
    parts = dirname.split("--", 2)
    if len(parts) < 3:
        return None
    return f"{parts[1]}/{parts[2]}"


def _latest_snapshot(ds_dir: Path) -> Path | None:
    snapshots_dir = ds_dir / "snapshots"
    if not snapshots_dir.exists():
        return None
    candidates = [d for d in snapshots_dir.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def _scan_hub_datasets(datasets_dir: Path) -> list[dict]:
    """HuggingFace Hub 캐시 스캔 (datasets--org--name/snapshots/hash/)."""
    if not datasets_dir.exists():
        return []
    results = []
    for candidate in datasets_dir.iterdir():
        if not candidate.is_dir():
            continue
        repo_id = _repo_id_from_dirname(candidate.name)
        if not repo_id:
            continue
        snapshot = _latest_snapshot(candidate)
        if not snapshot:
            continue
        info_path = snapshot / "meta" / "info.json"
        if not info_path.exists():
            info_path = snapshot / "info.json"
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
            info_path = ds_dir / "meta" / "info.json"
            if not info_path.exists():
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
    hub_dir = settings.datasets_dir / f"datasets--{org}--{name}"
    snapshot = _latest_snapshot(hub_dir)
    if snapshot:
        return snapshot, "hub"

    return None


def get_dataset(dataset_id: str) -> dict | None:
    result = _find_dataset_dir(dataset_id)
    if not result:
        return None
    ds_path, source = result

    info_path = ds_path / "meta" / "info.json"
    if not info_path.exists():
        info_path = ds_path / "info.json"
    meta = _parse_meta(info_path)

    # 에피소드 목록
    episodes = []
    episodes_path = ds_path / "meta" / "episodes.jsonl"
    if episodes_path.exists():
        for line in episodes_path.read_text().strip().split("\n"):
            if line:
                try:
                    episodes.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # tasks
    tasks = []
    tasks_path = ds_path / "meta" / "tasks.jsonl"
    if tasks_path.exists():
        for line in tasks_path.read_text().strip().split("\n"):
            if line:
                try:
                    tasks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

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
