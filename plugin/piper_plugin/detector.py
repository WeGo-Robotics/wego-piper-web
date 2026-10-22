"""검출 모델 로더 — `daemons/detector_loader.py` 를 플러그인에서 쓰는 문.

기본은 RT-DETR(transformers, Apache-2.0)이다. **번들 플러그인은 ultralytics 를 쓰지 않는다**
(AGPL — 그 파일 머리의 사유). 사용자 플러그인이 무엇을 import 하는지는 사용자 몫이다.

3b 단계에서 로더 본체가 이 패키지로 들어온다(feature/plugins.md §13-4). 그때까지는
저장소의 `daemons/` 를 찾아 import 한다 — 게이트웨이와 같은 기계에서 도는 것이 전제다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_MODEL = "PekingU/rtdetr_v2_r18vd"


def _daemons_dir() -> Path | None:
    env = os.environ.get("PIPER_DAEMONS_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "daemons"      # 저장소 체크아웃
    if here.is_dir():
        return here
    for cand in (Path("/app/daemons"),):                          # 컨테이너
        if cand.is_dir():
            return cand
    return None


def load(model: str = DEFAULT_MODEL, device: str = "cpu"):
    """모델 이름 → `predict(frame, conf=, imgsz=, device=)` 를 가진 객체 (ultralytics 모양)."""
    try:
        from detector_loader import load_detector
    except ImportError:
        d = _daemons_dir()
        if d is None:
            raise ImportError("detector_loader 를 찾을 수 없다 — PIPER_DAEMONS_DIR 를 주거나 저장소 안에서 돌려라")
        sys.path.insert(0, str(d))
        from detector_loader import load_detector
    return load_detector(model, device=device)
