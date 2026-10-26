"""체크포인트가 무엇을 내는가 — 절대 관절 위치냐 델타냐 (feature/act-delta.md).

가장 위험한 사고는 파라미터가 아니라 **모드 불일치**다:

| 사고 | 증상 |
|---|---|
| 델타 체크포인트를 절대로 실행 | 액션 ≈ 0 → 팔이 영점 자세로 **돌진** |
| 절대 체크포인트를 델타로 실행 | 현재 + 절대 목표 = 두 배 거리 점프 |

그래서 사람이 고르지 않는다. 판정 순서:

1. 체크포인트 폴더의 표식 `piper_action_space.json` — 추론을 시작할 때 남긴다. 한 번 남으면 학습 데이터셋이
   사라져도 판정이 안 바뀐다.
2. LeRobot 이 체크포인트마다 남기는 `train_config.json` 의 데이터셋 → 그 데이터셋의 `meta/piper_delta.json`
   (변환기 `lerobot_policy_piper.delta` 가 쓴다). 학습 도중의 체크포인트도, 원격에서 학습해 받은 것도 같은 길이다.
3. 데이터셋을 못 찾았는데 이름이 `_delta` 면 **모른다(`unknown`) — 추론을 막는다.** 변환기가 늘 그 이름으로
   만들기 때문이다. 아니면 절대(이 기능 전부터 있던 체크포인트 전부).
4. `train_config.json` 이 없으면(허브 모델 등) 절대.

백엔드는 정책 패키지를 import 하지 않는다 — 이름·값을 여기 다시 적고 테스트가 변환기·wrapper 와 대조한다.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

ABSOLUTE = "absolute"
DELTA_V1 = "delta_v1"
UNKNOWN = "unknown"
CKPT_MARKER = "piper_action_space.json"       # = wrapper/delta_action.CKPT_MARKER
DATASET_META = "piper_delta.json"             # = lerobot_policy_piper.delta.META_FILE
_DELTA_NAME = re.compile(r"_delta(_|$)")      # `<name>_delta`, 굽기까지 하면 `<name>_delta_stage`


def dataset_delta(ds_path: Path | None) -> dict | None:
    """데이터셋의 델타 근거 메타. 델타가 아니면 None."""
    if ds_path is None:
        return None
    try:
        meta = json.loads((ds_path / "meta" / DATASET_META).read_text())
    except (OSError, ValueError):
        return None
    return meta if meta.get("action_space") == DELTA_V1 else None


def train_dataset(ckpt: Path) -> tuple[str | None, Path | None]:
    """`train_config.json` 이 말하는 학습 데이터셋 — (repo_id, 찾은 경로)."""
    from app.services.dataset_scanner import find_dataset_path

    try:
        ds = json.loads((ckpt / "train_config.json").read_text()).get("dataset") or {}
    except (OSError, ValueError):
        return None, None
    repo_id, root = ds.get("repo_id"), ds.get("root")
    if root and (Path(root) / "meta" / "info.json").is_file():
        return repo_id, Path(root)
    return repo_id, (find_dataset_path(repo_id) if repo_id else None)


def _spec(meta: dict) -> dict:
    """wrapper 에 넘길 것 — 채널과 스텝당 상한만."""
    return {"delta_names": list(meta.get("delta_names") or []), "clamp": dict(meta.get("clamp") or {})}


def resolve(ckpt: Path) -> dict:
    """`{"action_space", "spec", "dataset", "source"}`. spec 은 델타일 때만."""
    try:
        marked = json.loads((ckpt / CKPT_MARKER).read_text())
        space = marked.get("action_space", ABSOLUTE)
        return {"action_space": space, "spec": _spec(marked) if space == DELTA_V1 else None,
                "dataset": marked.get("dataset"), "source": "marker"}
    except (OSError, ValueError):
        pass
    repo_id, ds_path = train_dataset(ckpt)
    if repo_id is None:
        return {"action_space": ABSOLUTE, "spec": None, "dataset": None, "source": "default"}
    if ds_path is not None:
        meta = dataset_delta(ds_path)
        if meta is not None:
            return {"action_space": DELTA_V1, "spec": _spec(meta), "dataset": repo_id, "source": "dataset"}
        return {"action_space": ABSOLUTE, "spec": None, "dataset": repo_id, "source": "dataset"}
    if _DELTA_NAME.search(repo_id.rsplit("/", 1)[-1]):
        return {"action_space": UNKNOWN, "spec": None, "dataset": repo_id, "source": "name"}
    return {"action_space": ABSOLUTE, "spec": None, "dataset": repo_id, "source": "default"}


def unknown_message(info: dict) -> str:
    dataset = info.get("dataset")
    return (f"이 체크포인트는 델타 데이터셋({dataset})으로 학습한 것으로 보이는데 그 데이터셋을 찾을 수 "
            f"없어 액션 공간을 확인할 수 없습니다. 절대로 돌리면 팔이 영점 자세로 돌진할 수 있어 시작하지 않습니다 — "
            f"데이터셋을 되돌리세요")


def remember(ckpt: Path, info: dict) -> bool:
    """델타로 판정한 체크포인트에 표식을 남긴다 — 이후로는 학습 데이터셋이 없어도 델타다.

    추론 **시작**에서만 부른다 (미리보기는 파일을 고치지 않는다). HF 캐시 스냅샷에는 쓰지 않는다 — 공유
    캐시를 오염시킨다(core/base_align.py 와 같은 이유). 남겼으면 True.
    """
    if info.get("action_space") != DELTA_V1 or info.get("source") != "dataset":
        return False
    if "snapshots" in ckpt.parts:
        return False
    try:
        (ckpt / CKPT_MARKER).write_text(json.dumps(
            {"action_space": DELTA_V1, "dataset": info.get("dataset"), **(info.get("spec") or {})},
            indent=2, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        logger.warning("액션 공간 표식을 남기지 못했다 (%s): %s", ckpt, e)
        return False
