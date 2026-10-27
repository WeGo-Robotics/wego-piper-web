"""
로컬 체크포인트 스캔.
HuggingFace Hub 캐시 구조: models--org--name/snapshots/hash/config.json
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.core.hf_layout import dirname_from_repo_id, latest_snapshot, repo_id_from_dirname

logger = logging.getLogger(__name__)


def _notes(root: Path) -> dict:
    """이름·설명 사이드카 (piper_notes.json). 체크포인트 형식에 그 자리가 없어
    디렉토리 바로 아래에 둔다 — notes_sidecar 참고."""
    from app.services.notes_sidecar import read_notes

    return read_notes(root, kind="model")


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
    from app.core.policies import POLICIES, canonical

    raw = config.get("type") or config.get("policy_type") or config.get("_target_") or "unknown"
    name = raw.rsplit(".", 1)[-1] if "." in raw else raw
    # 이름을 바꾸기 전에 저장한 체크포인트(`act_aux`)는 지금 이름으로 — 못 알아보면 목록에서 사라진다
    name = canonical(name)
    if name in POLICIES:
        return name, True

    # 옛 `_target_` 형식은 클래스 이름으로 온다 (`SmolVLAConfig`). 이제 이 판정이
    # 체크포인트를 **숨기는** 방향으로 쓰이므로, 못 알아보면 멀쩡한 모델이 사라진다.
    key = name.removesuffix("Config").lower()
    for policy in POLICIES:
        if policy.replace("_", "") == key:
            return policy, True
    return name, False


def _action_space(model_dir: Path) -> str:
    """`services/action_space.resolve` 의 결과만. 목록이 이것 때문에 죽으면 안 된다."""
    from app.services import action_space

    try:
        return action_space.resolve(model_dir)["action_space"]
    except Exception as e:                                       # noqa: BLE001
        logger.warning("액션 공간 판정 실패 (%s): %s", model_dir, e)
        return action_space.UNKNOWN


def _train_cameras(model_dir: Path) -> dict:
    """`services/train_cameras.resolve` — 학습 때 카메라의 해상도·기종. 목록이 이것 때문에 죽으면 안 된다."""
    from app.services import train_cameras

    try:
        return train_cameras.resolve(model_dir)
    except Exception as e:                                       # noqa: BLE001
        logger.warning("학습 카메라 판정 실패 (%s): %s", model_dir, e)
        return {"cameras": {}, "dataset": None, "source": "none"}


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






def _created_at(root: Path, *markers: Path) -> str:
    """"만든 날짜" — dataset_scanner 와 같은 규칙 (birthtime → 표식 파일 → mtime)."""
    cands: list[float] = []
    try:
        cands.append(float(root.stat().st_birthtime))
    except (AttributeError, OSError):
        pass
    for m in markers:
        try:
            cands.append(m.stat().st_mtime)
        except OSError:
            pass
    # 디렉토리 mtime 은 항상 후보 — 표식 파일이 디렉토리 변경보다 뒤에 써지면
    # "만든 날짜 > 수정 날짜"가 된다 (dataset_scanner 와 같은 규칙).
    cands.append(root.stat().st_mtime)
    return datetime.fromtimestamp(min(cands)).isoformat()


# LeRobot 의 자동 실행 폴더 `outputs/train/<날짜>/<시각>_<정책>` — 이름이 곧 시작 시각이다
_RUN_STARTED = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2})/(\d{2})-(\d{2})-(\d{2})_[^/]*$")


def _run_started(rel: Path, job_dir: Path) -> str:
    """학습을 **시작한** 시각. 폴더 시각은 체크포인트가 추가될 때마다 밀려서(마지막 저장 시각이
    된다), 자동 경로면 이름에서 읽는다. 아니면(Output Dir 을 직접 적은 학습) 파일 시각."""
    m = _RUN_STARTED.search(rel.as_posix())
    if m:
        d, hh, mm, ss = m.groups()
        try:
            return datetime.fromisoformat(f"{d}T{hh}:{mm}:{ss}").isoformat()
        except ValueError:
            pass
    return _created_at(job_dir, job_dir / "train_config.json", job_dir / "checkpoints")


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
            "created": _created_at(snapshot, config_path),
            "notes": _notes(snapshot),
            # 절대 / delta_v1 / unknown — 화면 배지용. 추론 시작은 따로 다시 판정한다
            "action_space": _action_space(snapshot),
            "train_cameras": _train_cameras(snapshot),
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
        # ⚠ **학습 폼의 제목·설명은 run 루트**(output_dir/piper_notes.json)에
        #   남는다 — 체크포인트는 그 뒤에 여러 개 생기고, 이름은 학습(run)의
        #   것이다. 체크포인트에 자기 사이드카가 없으면 run 의 것을 물려받는다;
        #   있으면(모델 페이지 편집기로 쓴 것) 그쪽이 이긴다.
        run_notes = _notes(job_dir)
        try:
            run_created = _run_started(job_dir.relative_to(models_dir), job_dir)
        except ValueError:
            run_created = _run_started(Path(job_dir.name), job_dir)
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
            own_notes = _notes(model_dir)
            results.append({
                "id": model_id,
                "run": run_id,
                "run_notes": run_notes,
                "run_created": run_created,
                "checkpoint": step_label,
                "step": step_num,
                "path": str(model_dir),
                "policy_type": policy_type,
                "is_policy": is_policy,
                "config": config,
                "requirements": extract_model_requirements(config, model_dir),
                "size_bytes": _dir_size(model_dir),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "created": _created_at(model_dir, config_path),
                "source_dir": str(models_dir),
                "action_space": _action_space(model_dir),
                "train_cameras": _train_cameras(model_dir),
                "notes": own_notes if (own_notes.get("name") or own_notes.get("description"))
                         else run_notes,
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


def _train_config(model_dir: Path) -> dict:
    """체크포인트의 `train_config.json` — 스텝·배치·lr·출발점이 여기 있다. 없으면(허브 모델) 빈 값."""
    return _parse_config(model_dir / "train_config.json")


def _base_of(pretrained: str | None, by_real: dict[str, dict]) -> dict | None:
    """이어 학습의 출발점. 처음부터면 None.

    ⚠ `train_config` 의 경로를 그대로 믿지 않는다 — 길이를 맞추거나 워밍스타트한 학습은
    `derived_pretrained/…` **사본**에서 출발하고, 그 사본은 원본 파일로 가는 링크다
    (core/base_align.py). 가중치 파일을 따라가면 원본이 나온다.
    """
    if not pretrained:
        return None
    p = Path(pretrained)
    try:
        real = (p / "model.safetensors").resolve()
    except OSError:
        real = p
    owner = by_real.get(str(real.parent))
    if owner is not None:
        return {"run": owner.get("run"), "id": owner["id"], "checkpoint": owner.get("checkpoint"),
                "label": owner["id"]}
    for part in real.parts:
        repo = repo_id_from_dirname(part, "models")
        if repo:
            return {"run": None, "id": None, "checkpoint": None, "label": repo}
    return {"run": None, "id": None, "checkpoint": None, "label": str(p)}


def _final_loss(run_dir: Path) -> float | None:
    """학습이 남긴 곡선의 마지막 loss. 곡선 파일은 게이트웨이가 학습 중에 쓴다 —
    그 전(또는 원격) 학습에는 없다."""
    from app.services.weight_eval import TRAIN_METRICS_FILE

    try:
        losses = json.loads((run_dir / TRAIN_METRICS_FILE).read_text()).get("losses") or []
        return float(losses[-1]) if losses else None
    except (OSError, ValueError, TypeError):
        return None


def scan_runs() -> list[dict]:
    """가중치 표 — **학습 하나가 한 행**이다. 학습 산출물이 아닌 모델(허브에서 받은 것)은
    모델 하나가 한 행.

    ⚠ **`last` 는 따로 세지 않는다.** LeRobot 의 `checkpoints/last` 는 마지막 스텝 폴더로
    가는 링크라, 둘을 나란히 두면 같은 가중치가 두 번 보인다(실측: 체크포인트 207개 중
    35개). 실제 폴더로 합치고 `final` 로 표시한다. 고르는 id 는 스텝 쪽이다 — `last` 는
    학습이 이어지면 다른 폴더를 가리킨다.
    """
    models = scan_models()
    by_real: dict[str, dict] = {}
    for m in models:
        try:
            by_real.setdefault(str(Path(m["path"]).resolve()), m)
        except OSError:
            pass

    groups: dict[tuple[str, str], list[dict]] = {}
    for m in models:
        key = ("run", m["run"]) if m.get("run") else ("model", m["id"])
        groups.setdefault(key, []).append(m)

    rows = []
    for (kind, key), members in groups.items():
        # 실제 폴더별로 합친다 — `last` 링크와 그 스텝 폴더가 한 칸이 된다
        by_dir: dict[str, list[dict]] = {}
        for m in members:
            try:
                real = str(Path(m["path"]).resolve())
            except OSError:
                real = m["path"]
            by_dir.setdefault(real, []).append(m)
        ckpts = []
        for same in by_dir.values():
            named = [m for m in same if m.get("checkpoint") != "last"]
            main = named[0] if named else same[0]
            own = main.get("notes") or {}
            ckpts.append({
                "id": main["id"],
                # 이 칸이 가리키는 id 전부 — 화면이 예전에 고른 `…/last` 도 이 칸으로 알아본다
                "ids": [m["id"] for m in same],
                "checkpoint": main.get("checkpoint"),
                "step": main.get("step"),
                "final": any(m.get("checkpoint") == "last" for m in same),
                "path": main["path"],
                "name": own.get("name") or "",
                "size_bytes": main.get("size_bytes", 0),
            })
        # 마지막 체크포인트가 먼저, 그 다음은 최신 스텝부터
        ckpts.sort(key=lambda c: (not c["final"], -(c["step"] or 0)))
        head = next(m for m in members if m["id"] == ckpts[0]["id"])
        tc = _train_config(Path(head["path"]))
        policy_cfg = tc.get("policy") or {}
        if kind == "run":
            # 표에서 고치는 자리 — 학습 폼 제목·설명과 같은 run 루트 사이드카
            notes = head.get("run_notes") or {}
            run_dir = Path(head["path"]).parents[2]
        else:
            notes = head.get("notes") or {}
            run_dir = Path(head["path"])
        # 보이는 이름 — run 사이드카가 먼저, 없으면 체크포인트에 붙인 이름(모델 페이지 편집기)
        shown = notes if notes.get("name") else next(
            (m["notes"] for m in members if (m.get("notes") or {}).get("name")), notes)
        rows.append({
            "key": key,
            "kind": kind,
            "run": key if kind == "run" else None,
            "name": shown.get("name") or "",
            "description": shown.get("description") or "",
            # 사이드카 그대로 — 표의 이름 편집이 설명을 지우지 않게 이걸 함께 보낸다
            "notes": notes,
            "dataset": (head.get("train_cameras") or {}).get("dataset")
                       or (tc.get("dataset") or {}).get("repo_id"),
            "policy_type": head.get("policy_type"),
            "is_policy": head.get("is_policy", True),
            "action_space": head.get("action_space"),
            "created": head.get("run_created") or head.get("created"),
            "modified": max(m["modified"] for m in members),
            "steps": tc.get("steps"),
            "batch_size": tc.get("batch_size"),
            "lr": (tc.get("optimizer") or {}).get("lr") or policy_cfg.get("optimizer_lr"),
            "base": _base_of(policy_cfg.get("pretrained_path"), by_real),
            "final_loss": _final_loss(run_dir) if kind == "run" else None,
            "train_cameras": head.get("train_cameras"),
            "size_bytes": sum(c["size_bytes"] for c in ckpts),
            "path": str(run_dir),
            "checkpoints": ckpts,
        })
    rows.sort(key=lambda r: r["created"] or "", reverse=True)
    return rows


def run_dir_of(run: str) -> Path | None:
    """학습(run) id → run 루트. 스캔에 있는 것만 — 임의 경로에 사이드카를 쓰지 않는다."""
    for m in scan_models():
        if m.get("run") == run:
            return Path(m["path"]).parents[2]
    return None


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
