"""
로컬 체크포인트 스캔.
HuggingFace Hub 캐시 구조: models--org--name/snapshots/hash/config.json
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _parse_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except Exception:
        return {}


def extract_model_requirements(config: dict) -> dict:
    """config.json에서 추론에 필요한 카메라/관절 정보를 추출."""
    input_features = config.get("input_features", {})
    output_features = config.get("output_features", {})

    # 카메라 이름 + 해상도
    required_cameras = []
    for key, feat in input_features.items():
        if key.startswith("observation.images."):
            cam_name = key.split("observation.images.")[1]
            shape = feat.get("shape", [])
            required_cameras.append({
                "name": cam_name,
                "channels": shape[0] if len(shape) >= 1 else 3,
                "height": shape[1] if len(shape) >= 2 else None,
                "width": shape[2] if len(shape) >= 3 else None,
            })

    # 관절 수 (state dimension)
    state_feat = input_features.get("observation.state", {})
    state_dim = state_feat.get("shape", [0])[0] if state_feat else 0

    # 액션 차원
    action_feat = output_features.get("action", {})
    action_dim = action_feat.get("shape", [0])[0] if action_feat else 0

    return {
        "required_cameras": required_cameras,
        "state_dim": state_dim,
        "action_dim": action_dim,
    }


def _repo_id_from_dirname(dirname: str) -> str | None:
    """models--wego-hansu--piper_smolvla → wego-hansu/piper_smolvla"""
    if not dirname.startswith("models--"):
        return None
    parts = dirname.split("--", 2)
    if len(parts) < 3:
        return None
    return f"{parts[1]}/{parts[2]}"


def _latest_snapshot(model_dir: Path) -> Path | None:
    """snapshots/ 아래에서 가장 최근 수정된 스냅샷 디렉토리 반환."""
    snapshots_dir = model_dir / "snapshots"
    if not snapshots_dir.exists():
        return None
    candidates = [d for d in snapshots_dir.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def scan_models() -> list[dict]:
    models_dir = settings.models_dir
    if not models_dir.exists():
        return []

    results = []
    for candidate in models_dir.iterdir():
        if not candidate.is_dir():
            continue
        repo_id = _repo_id_from_dirname(candidate.name)
        if not repo_id:
            continue

        snapshot = _latest_snapshot(candidate)
        if not snapshot:
            continue

        config_path = snapshot / "config.json"
        config = _parse_config(config_path)

        policy_type = config.get("type", config.get("policy_type", config.get("_target_", "unknown")))
        if "." in policy_type:
            policy_type = policy_type.rsplit(".", 1)[-1]

        stat = snapshot.stat()
        results.append({
            "id": repo_id,
            "path": str(snapshot),
            "policy_type": policy_type,
            "config": config,
            "requirements": extract_model_requirements(config),
            "size_bytes": _dir_size(snapshot),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })

    results.sort(key=lambda m: m["modified"], reverse=True)
    return results


def get_model(model_id: str) -> dict | None:
    """model_id = 'wego-hansu/piper_smolvla' 형태."""
    parts = model_id.split("/", 1)
    if len(parts) != 2:
        return None
    dirname = f"models--{parts[0]}--{parts[1]}"
    model_dir = settings.models_dir / dirname

    snapshot = _latest_snapshot(model_dir)
    if not snapshot:
        return None

    config = _parse_config(snapshot / "config.json")
    policy_type = config.get("type", config.get("policy_type", config.get("_target_", "unknown")))
    if "." in policy_type:
        policy_type = policy_type.rsplit(".", 1)[-1]

    files = []
    for f in sorted(snapshot.rglob("*")):
        if f.is_file():
            files.append({
                "path": str(f.relative_to(snapshot)),
                "size_bytes": f.stat().st_size,
            })

    return {
        "id": model_id,
        "path": str(snapshot),
        "policy_type": policy_type,
        "config": config,
        "requirements": extract_model_requirements(config),
        "size_bytes": _dir_size(snapshot),
        "modified": datetime.fromtimestamp(snapshot.stat().st_mtime).isoformat(),
        "files": files,
    }


def delete_model(model_id: str) -> bool:
    import shutil

    parts = model_id.split("/", 1)
    if len(parts) != 2:
        return False
    dirname = f"models--{parts[0]}--{parts[1]}"
    model_dir = settings.models_dir / dirname
    if not model_dir.exists():
        return False
    shutil.rmtree(model_dir)
    logger.info("Deleted model: %s", model_dir)
    return True
