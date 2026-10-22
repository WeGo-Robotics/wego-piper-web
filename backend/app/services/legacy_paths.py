"""옛 데이터 디렉토리 이름 → 새 이름. 기동 때 한 번 (feature/plugins.md §14-2).

검출 가중치·캡처 데이터셋 디렉토리가 `yolo_*` 에서 `detector_*` 로 바뀌었다.
배포 기계에는 옛 이름 아래 커스텀 가중치와 캡처가 있다 — 이름이 바뀌었다고
그걸 잃으면 안 된다.

규칙:
- 옛 것만 있다 → `rename` (같은 파일시스템이라 원자적)
- 둘 다 있는데 새 쪽이 비어 있다(`.gitkeep` 뿐 — 소스 체크아웃·새 이미지) → 옛 것의
  내용을 새 쪽으로 옮기고 옛 디렉토리를 지운다
- 둘 다 있고 새 쪽에 내용이 있다 → **건드리지 않고** 경고. 사람이 합친다

⚠ **삭제 예정 — 한 릴리스 뒤.** 이 파일만이 저장소에서 옛 이름을 적을 수 있다
(`test_no_model_branding.py` 의 허용 목록).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LEGACY_DIRS: tuple[tuple[str, str], ...] = (
    ("yolo_models", "detector_models"),
    ("yolo_datasets", "detector_datasets"),
)


def _only_placeholder(d: Path) -> bool:
    return all(p.name == ".gitkeep" for p in d.iterdir())


def migrate_legacy_dirs(new_dirs: dict[str, Path]) -> list[str]:
    """`{새 이름: 새 경로}` 를 받아 옛 이름의 디렉토리를 옮긴다. 옮긴 것의 설명을 돌려준다."""
    moved: list[str] = []
    for old_name, new_name in LEGACY_DIRS:
        new = new_dirs.get(new_name)
        if new is None:
            continue
        old = new.with_name(old_name)
        if not old.is_dir():
            continue
        if not new.exists():
            old.rename(new)
            moved.append(f"{old} → {new}")
        elif _only_placeholder(new):
            for child in old.iterdir():
                child.rename(new / child.name)
            old.rmdir()
            moved.append(f"{old}/* → {new}/")
        else:
            logger.warning("옛 디렉토리와 새 디렉토리가 둘 다 비어 있지 않습니다 — 손으로 합치세요: %s, %s",
                           old, new)
    for m in moved:
        logger.info("데이터 디렉토리 이관: %s", m)
    return moved
