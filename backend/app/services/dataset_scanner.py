"""
로컬 데이터셋 스캔.
HuggingFace Hub 캐시 구조: datasets--org--name/snapshots/hash/
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


def scan_datasets() -> list[dict]:
    datasets_dir = settings.datasets_dir
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

        # meta/info.json 또는 직접 info.json
        info_path = snapshot / "meta" / "info.json"
        if not info_path.exists():
            info_path = snapshot / "info.json"
        meta = _parse_meta(info_path)

        stat = snapshot.stat()
        results.append({
            "id": repo_id,
            "path": str(snapshot),
            "total_episodes": meta.get("total_episodes", 0),
            "total_frames": meta.get("total_frames", 0),
            "fps": meta.get("fps"),
            "features": meta.get("features", {}),
            "size_bytes": _dir_size(snapshot),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })

    results.sort(key=lambda d: d["modified"], reverse=True)
    return results


def get_dataset(dataset_id: str) -> dict | None:
    parts = dataset_id.split("/", 1)
    if len(parts) != 2:
        return None
    dirname = f"datasets--{parts[0]}--{parts[1]}"
    ds_dir = settings.datasets_dir / dirname

    snapshot = _latest_snapshot(ds_dir)
    if not snapshot:
        return None

    info_path = snapshot / "meta" / "info.json"
    if not info_path.exists():
        info_path = snapshot / "info.json"
    meta = _parse_meta(info_path)

    # 에피소드 목록
    episodes = []
    episodes_path = snapshot / "meta" / "episodes.jsonl"
    if episodes_path.exists():
        for line in episodes_path.read_text().strip().split("\n"):
            if line:
                try:
                    episodes.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # tasks
    tasks = []
    tasks_path = snapshot / "meta" / "tasks.jsonl"
    if tasks_path.exists():
        for line in tasks_path.read_text().strip().split("\n"):
            if line:
                try:
                    tasks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    return {
        "id": dataset_id,
        "path": str(snapshot),
        "total_episodes": meta.get("total_episodes", 0),
        "total_frames": meta.get("total_frames", 0),
        "fps": meta.get("fps"),
        "features": meta.get("features", {}),
        "episodes": episodes,
        "tasks": tasks,
        "size_bytes": _dir_size(snapshot),
        "modified": datetime.fromtimestamp(snapshot.stat().st_mtime).isoformat(),
    }


def delete_dataset(dataset_id: str) -> bool:
    parts = dataset_id.split("/", 1)
    if len(parts) != 2:
        return False
    dirname = f"datasets--{parts[0]}--{parts[1]}"
    ds_dir = settings.datasets_dir / dirname
    if not ds_dir.exists():
        return False
    shutil.rmtree(ds_dir)
    logger.info("Deleted dataset: %s", ds_dir)
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
