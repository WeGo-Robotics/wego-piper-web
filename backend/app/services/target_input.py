"""체크포인트가 목표 위치를 입력으로 받는가 (feature/target-conditioning.md).

목표 입력 체크포인트는 `observation.state` 뒤에 목표 칸(위 카메라 좌표를 푸리에 특징으로 편 것)이 붙은
데이터셋으로 학습했다. 추론이 같은 칸을 만들려면 그 사양(이름·주파수·종횡비)이 있어야 한다 — 사양은 학습
데이터셋의 `meta/piper_target.json` 에 있고, 여기서 찾아 wrapper 에 `--target-spec` 으로 싣는다.
사람이 고르지 않는다 (`action_space.py` 와 같은 이유·같은 순서):

1. 체크포인트 폴더의 표식 `piper_target_input.json` — 추론을 시작할 때 남긴다. 한 번 남으면 학습 데이터셋이
   사라져도 사양이 남는다.
2. `train_config.json` 의 데이터셋 → 그 데이터셋의 `meta/piper_target.json` (변환기 `lerobot_policy_piper.target`).
3. 데이터셋을 못 찾았는데 이름에 `_target` 이 있으면 **모른다(`unknown`) — 추론을 막는다.** 사양 없이는 state 를
   만들 수 없다(wrapper 도 길이로 막지만, 여기서 이유를 말하는 편이 낫다).
4. 아니면 목표 입력이 아니다 (이 기능 전부터 있던 체크포인트 전부).

틀렸을 때 무엇이 일어나나 — 델타와 달리 팔이 돌진하지는 않는다. 사양이 빠지면 wrapper 가 길이(31 vs 14)로
시작을 거절하고, 사양이 다른 데이터셋 것이면 목표가 엉뚱한 칸으로 들어가 정책이 다른 자리를 집으러 간다.
그래서 표식과 인자를 대조한다.

백엔드는 정책 패키지를 import 하지 않는다 — 이름을 여기 다시 적고 테스트가 변환기·wrapper 와 대조한다.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

NONE = "none"
TARGET_V1 = "rff_v1"                          # = lerobot_policy_piper.target.ENCODING = wrapper/target_input.TARGET_V1
UNKNOWN = "unknown"
CKPT_MARKER = "piper_target_input.json"       # = wrapper/target_input.CKPT_MARKER
DATASET_META = "piper_target.json"            # = lerobot_policy_piper.target.META_FILE
_TARGET_NAME = re.compile(r"_target(_|$)")    # `<name>_target`, 굽기까지 하면 `<name>_target_stage`


def dataset_target(ds_path: Path | None) -> dict | None:
    """데이터셋의 목표 입력 근거 메타. 목표 입력이 아니면 None."""
    if ds_path is None:
        return None
    try:
        meta = json.loads((ds_path / "meta" / DATASET_META).read_text())
    except (OSError, ValueError):
        return None
    return meta if meta.get("input") == TARGET_V1 and isinstance(meta.get("spec"), dict) else None


def resolve(ckpt: Path) -> dict:
    """`{"input", "spec", "dataset", "source"}`. spec 은 목표 입력일 때만."""
    from app.services.action_space import train_dataset

    try:
        marked = json.loads((ckpt / CKPT_MARKER).read_text())
        if marked.get("encoding") == TARGET_V1:
            spec = {k: v for k, v in marked.items() if k != "dataset"}
            return {"input": TARGET_V1, "spec": spec, "dataset": marked.get("dataset"), "source": "marker"}
    except (OSError, ValueError):
        pass
    repo_id, ds_path = train_dataset(ckpt)
    if repo_id is None:
        return {"input": NONE, "spec": None, "dataset": None, "source": "default"}
    if ds_path is not None:
        meta = dataset_target(ds_path)
        if meta is not None:
            return {"input": TARGET_V1, "spec": meta["spec"], "dataset": repo_id, "source": "dataset"}
        return {"input": NONE, "spec": None, "dataset": repo_id, "source": "dataset"}
    if _TARGET_NAME.search(repo_id.rsplit("/", 1)[-1]):
        return {"input": UNKNOWN, "spec": None, "dataset": repo_id, "source": "name"}
    return {"input": NONE, "spec": None, "dataset": repo_id, "source": "default"}


def unknown_message(info: dict) -> str:
    dataset = info.get("dataset")
    return (f"이 체크포인트는 목표 입력 데이터셋({dataset})으로 학습한 것으로 보이는데 그 데이터셋을 찾을 "
            f"수 없어 목표 칸을 만들 수 없습니다. 시작하지 않습니다 — 데이터셋을 되돌리세요")


def target_dims(info: dict) -> int:
    """state 뒤에 붙은 목표 칸 수. 목표 입력이 아니면 0."""
    return len((info.get("spec") or {}).get("names") or []) if info.get("input") == TARGET_V1 else 0


def remember(ckpt: Path, info: dict) -> bool:
    """목표 입력으로 판정한 체크포인트에 사양을 남긴다 — 이후로는 학습 데이터셋이 없어도 같은 칸을 만든다.

    추론 **시작**에서만 부른다 (미리보기는 파일을 고치지 않는다). HF 캐시 스냅샷에는 쓰지 않는다
    (`action_space.remember` 와 같은 이유). 남겼으면 True.
    """
    if info.get("input") != TARGET_V1 or info.get("source") != "dataset":
        return False
    if "snapshots" in ckpt.parts:
        return False
    try:
        (ckpt / CKPT_MARKER).write_text(json.dumps(
            {**(info.get("spec") or {}), "dataset": info.get("dataset")}, indent=2, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        logger.warning("목표 입력 표식을 남기지 못했다 (%s): %s", ckpt, e)
        return False
