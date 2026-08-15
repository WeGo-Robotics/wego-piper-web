"""에피소드 편집 뒤 사이드카 동기화 (feature/episode-editor.md §4 의 ⚠ 버그).

`lerobot-edit-dataset` 의 in-place delete 는 원본을 `<이름>_old` 로 옮기고
데이터셋을 **처음부터 다시 쓴다** — 에피소드 번호는 빈틈 없이 당겨지고,
LeRobot 이 모르는 meta 파일(우리 사이드카)은 새 meta 에 **없다.**

그대로 두면 두 가지 사고 중 하나가 난다:
- 사이드카가 사라진다 (분석 결과·카메라 해석 유실), 또는
- 사람이 `_old` 에서 손으로 복사한다 → **키가 옛 번호라 삭제 지점 뒤의 라벨
  전부가 한 칸 밀린 채 맞는 것처럼 보인다.** 로그에 안 남는 종류의 오염이다.

여기서 `_old` 를 정본으로 삼아 번호를 재매핑해 새 meta 로 가져온다.
호출자는 [wrapper/edit_dataset.py](../../wrapper/edit_dataset.py) — 편집과 같은
프로세스에서 돌므로 게이트웨이 재시작과 무관하게 항상 따라붙는다.
"""

import json
import logging
import shutil
from pathlib import Path

from .labeler import LABELS_FILE, SIGNALS_FILE

logger = logging.getLogger(__name__)

# 에피소드 번호와 무관한 사이드카 — 그대로 복사하면 된다
COPY_AS_IS = ("piper_cameras.json",)


def _episode_mapping(total_before: int, deleted: list[int]) -> dict[int, int]:
    """lerobot dataset_tools.delete_episodes 와 같은 규칙: 남은 것을 순서대로 당긴다."""
    gone = set(deleted)
    kept = [i for i in range(total_before) if i not in gone]
    return {old: new for new, old in enumerate(kept)}


def remap_after_delete(ds_root: Path, deleted: list[int]) -> list[str]:
    """`_old` 백업의 사이드카를 재매핑해 새 meta 로 옮긴다. 옮긴 파일 이름을 돌려준다.

    사이드카가 없으면 조용히 아무것도 안 한다 — 분석한 적 없는 데이터셋이 대부분이다.
    """
    ds_root = Path(ds_root)
    old_meta = ds_root.with_name(ds_root.name + "_old") / "meta"
    new_meta = ds_root / "meta"
    if not old_meta.is_dir() or not new_meta.is_dir():
        logger.info("사이드카 동기화 생략 — 백업(%s) 또는 새 meta 가 없다", old_meta)
        return []

    info = json.loads((old_meta / "info.json").read_text())
    mapping = _episode_mapping(int(info["total_episodes"]), deleted)
    moved: list[str] = []

    # 1. 페이즈 라벨 — episodes 키가 에피소드 번호다
    labels_path = old_meta / LABELS_FILE
    if labels_path.is_file():
        data = json.loads(labels_path.read_text())
        eps = data.get("episodes", {})
        data["episodes"] = {
            str(mapping[int(k)]): v for k, v in eps.items() if int(k) in mapping
        }
        (new_meta / LABELS_FILE).write_text(json.dumps(data, ensure_ascii=False))
        moved.append(LABELS_FILE)
        logger.info("페이즈 라벨 재매핑: %d → %d 에피소드", len(eps), len(data["episodes"]))

    # 2. 신호 parquet — episode_index 컬럼
    signals_path = old_meta / SIGNALS_FILE
    if signals_path.is_file():
        import pandas as pd

        df = pd.read_parquet(signals_path)
        df = df[df["episode_index"].isin(mapping)].copy()
        df["episode_index"] = df["episode_index"].map(mapping)
        df.to_parquet(new_meta / SIGNALS_FILE, index=False)
        moved.append(SIGNALS_FILE)

    # 3. 번호 무관 사이드카 — 그대로
    for name in COPY_AS_IS:
        src = old_meta / name
        if src.is_file() and not (new_meta / name).is_file():
            shutil.copy2(src, new_meta / name)
            moved.append(name)

    return moved
