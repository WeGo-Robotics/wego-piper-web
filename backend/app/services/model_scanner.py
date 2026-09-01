"""
로컬 체크포인트 스캔.
HuggingFace Hub 캐시 구조: models--org--name/snapshots/hash/config.json
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.core.hf_layout import dirname_from_repo_id, latest_snapshot, repo_id_from_dirname

logger = logging.getLogger(__name__)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())



def _policy_type_of(config: dict) -> tuple[str, bool]:
    """config.json 에서 정책 타입과 **정책 체크포인트인지 여부**를 뽑는다.

    LeRobot 정책은 draccus ChoiceRegistry 판별자인 `type` 을 반드시 갖는다
    (`act`/`smolvla`/`pi0`...). 그게 없으면 정책이 아니다 —
    예: `HuggingFaceTB/SmolVLM2-500M-Video-Instruct` 는 smolvla 의 **비전-언어 백본**이라
    models 디렉토리에 있지만 학습·추론 대상이 아니다. 그런데 파인튜닝 목록에 떠서
    고르면 학습이 깨진다.

    아는 정책 이름인지는 `core/policies.py` 한 곳에서 판단한다 — 목록을 또 만들지 않는다.
    """
    from app.core.policies import POLICIES

    raw = config.get("type") or config.get("policy_type") or config.get("_target_") or "unknown"
    name = raw.rsplit(".", 1)[-1] if "." in raw else raw
    if name in POLICIES:
        return name, True

    # 옛 `_target_` 형식은 클래스 이름으로 온다 (`SmolVLAConfig`). 이제 이 판정이
    # 체크포인트를 **숨기는** 방향으로 쓰이므로, 못 알아보면 멀쩡한 모델이 사라진다.
    key = name.removesuffix("Config").lower()
    for policy in POLICIES:
        if policy.replace("_", "") == key:
            return policy, True
    return name, False


def _parse_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except Exception:
        return {}


def _load_rename_map(model_dir: Path) -> dict[str, str]:
    """policy_preprocessor.json에서 rename_map을 읽어 역변환 맵 반환.
    예: {"observation.images.side": "observation.images.camera1"} → {"camera1": "side"}
    """
    pre_path = model_dir / "policy_preprocessor.json"
    if not pre_path.exists():
        return {}
    try:
        data = json.loads(pre_path.read_text())
        for step in data.get("steps", []):
            if step.get("registry_name") == "rename_observations_processor":
                rename_map = step.get("config", {}).get("rename_map", {})
                # 역변환: camera1 → side
                reverse = {}
                for src, dst in rename_map.items():
                    src_name = src.split("observation.images.")[-1] if "observation.images." in src else src
                    dst_name = dst.split("observation.images.")[-1] if "observation.images." in dst else dst
                    reverse[dst_name] = src_name
                return reverse
    except Exception:
        pass
    return {}


def extract_model_requirements(config: dict, model_dir: Path | None = None) -> dict:
    """config.json에서 추론에 필요한 카메라/관절 정보를 추출."""
    input_features = config.get("input_features", {})
    output_features = config.get("output_features", {})

    # rename_map 역변환 (camera1 → side)
    reverse_rename = _load_rename_map(model_dir) if model_dir else {}

    # 카메라 이름 + 해상도
    required_cameras = []
    for key, feat in input_features.items():
        if key.startswith("observation.images."):
            cam_name = key.split("observation.images.")[1]
            # base 모델에서 상속됐지만 실제 학습 데이터엔 없는 이미지 슬롯 제외.
            # rename_map이 있으면 그 대상(camera1/2 등)만 실제 학습 카메라이므로,
            # rename 대상이 아닌 슬롯(camera3 등)은 유령 요구사항으로 보고 제외한다.
            # rename_map이 없으면(리네임 없이 학습) 전부 포함해 기존 동작을 보존.
            if reverse_rename and cam_name not in reverse_rename:
                logger.info("모델 요구사항에서 상속된 빈 카메라 슬롯 제외: %s", cam_name)
                continue
            shape = feat.get("shape", [])
            required_cameras.append({
                "name": reverse_rename.get(cam_name, cam_name),
                "model_name": cam_name,
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






def _scan_hf_cache(models_dir: Path) -> list[dict]:
    """HuggingFace Hub 캐시 형식: models--org--name/snapshots/hash/config.json"""
    results = []
    for candidate in models_dir.iterdir():
        if not candidate.is_dir():
            continue
        repo_id = repo_id_from_dirname(candidate.name, "models")
        if not repo_id:
            continue

        snapshot = latest_snapshot(candidate)
        if not snapshot:
            continue

        config_path = snapshot / "config.json"
        config = _parse_config(config_path)

        policy_type, is_policy = _policy_type_of(config)

        stat = snapshot.stat()
        results.append({
            "id": repo_id,
            "path": str(snapshot),
            "policy_type": policy_type,
            "is_policy": is_policy,
            "config": config,
            "requirements": extract_model_requirements(config, snapshot),
            "size_bytes": _dir_size(snapshot),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "source_dir": str(models_dir),
        })

    return results


def _scan_train_outputs(models_dir: Path) -> list[dict]:
    """학습 출력 형식: **/checkpoints/{step}/pretrained_model/config.json
    날짜 디렉토리(2026-04-13/01-46-49_smolvla/) 등 중첩 구조도 지원."""
    results = []
    # checkpoints 디렉토리를 재귀적으로 찾기
    for checkpoints_dir in models_dir.rglob("checkpoints"):
        if not checkpoints_dir.is_dir():
            continue
        job_dir = checkpoints_dir.parent
        for ckpt_dir in checkpoints_dir.iterdir():
            if not ckpt_dir.is_dir():
                continue
            model_dir = ckpt_dir / "pretrained_model"
            config_path = model_dir / "config.json"
            if not config_path.exists():
                continue

            config = _parse_config(config_path)
            policy_type, is_policy = _policy_type_of(config)

            step_label = ckpt_dir.name
            # models_dir 기준 상대경로로 ID 생성
            try:
                rel = job_dir.relative_to(models_dir)
                model_id = f"{rel}/{step_label}"
            except ValueError:
                rel = Path(job_dir.name)
                model_id = f"{job_dir.name}/{step_label}"
            # ⚠ **학습(run)과 체크포인트를 나눠서 내보낸다.** 한 학습이 체크포인트를
            #   10~20개 남기므로 목록이 금세 수십 개가 되고, 이름만으로는 어느
            #   학습의 몇 번째인지 읽기 어렵다(실측: 학습 13개에 체크포인트 77개).
            #   `id` 를 화면에서 쪼개게 하면 HF 허브 모델(`PekingU/rtdetr_v2_r18vd`)
            #   까지 "PekingU 학습" 으로 묶인다 — 여기서만 판단한다.
            run_id = str(rel)
            # 정렬용 숫자. `last` 처럼 숫자가 아닌 것은 None 이고 화면이 맨 위에 둔다.
            try:
                step_num: int | None = int(step_label)
            except ValueError:
                step_num = None

            stat = model_dir.stat()
            results.append({
                "id": model_id,
                "run": run_id,
                "checkpoint": step_label,
                "step": step_num,
                "path": str(model_dir),
                "policy_type": policy_type,
                "is_policy": is_policy,
                "config": config,
                "requirements": extract_model_requirements(config, model_dir),
                "size_bytes": _dir_size(model_dir),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "source_dir": str(models_dir),
            })

    return results


def _scan_one_dir(models_dir: Path) -> list[dict]:
    # `.exists()` 는 접근 권한이 없으면 False 가 아니라 PermissionError 를 던진다.
    # model_paths.json 에 남은 남의 홈 경로(예: 도커 시절의 /root/.cache/...) 하나 때문에
    # 모델 목록 전체가 500 이 됐다. 못 읽는 경로는 조용히 건너뛴다.
    try:
        if not models_dir.exists():
            return []
    except OSError as e:
        logger.warning("모델 경로를 읽을 수 없어 건너뜀: %s (%s)", models_dir, e)
        return []

    results = []
    results.extend(_scan_hf_cache(models_dir))
    results.extend(_scan_train_outputs(models_dir))
    return results


def scan_models() -> list[dict]:
    results = []
    seen_ids = set()
    for models_dir in settings.model_paths:
        for model in _scan_one_dir(models_dir):
            if model["id"] not in seen_ids:
                seen_ids.add(model["id"])
                results.append(model)

    results.sort(key=lambda m: m["modified"], reverse=True)
    return results


def get_model(model_id: str) -> dict | None:
    """scan_models()에서 id로 모델 검색. HF 캐시 + 학습 출력 모두 지원."""
    for model in scan_models():
        if model["id"] == model_id:
            # 파일 목록 추가
            model_path = Path(model["path"])
            model["files"] = [
                {"path": str(f.relative_to(model_path)), "size_bytes": f.stat().st_size}
                for f in sorted(model_path.rglob("*")) if f.is_file()
            ]
            return model
    return None


def delete_model(model_id: str) -> bool:
    import shutil

    # scan_models에서 모델 찾기
    model = get_model(model_id)
    if model:
        model_path = Path(model["path"])
        # 학습 출력: pretrained_model 폴더 → 체크포인트 폴더 삭제
        if model_path.name == "pretrained_model":
            ckpt_dir = model_path.parent  # e.g., .../checkpoints/005000
            shutil.rmtree(ckpt_dir)
            logger.info("Deleted checkpoint: %s", ckpt_dir)
        else:
            # HF 캐시: snapshot 폴더의 상위 (models--org--name)
            # 또는 직접 경로
            shutil.rmtree(model_path)
            logger.info("Deleted model: %s", model_path)
        return True

    # fallback: HF 캐시 형식
    parts = model_id.split("/", 1)
    if len(parts) != 2:
        return False
    dirname = dirname_from_repo_id(model_id, "models") or ""
    model_dir = settings.models_dir / dirname
    if not model_dir.exists():
        return False
    shutil.rmtree(model_dir)
    logger.info("Deleted model: %s", model_dir)
    return True
