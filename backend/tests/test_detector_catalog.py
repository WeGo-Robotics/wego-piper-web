"""검출 모델 카탈로그와 가중치 로더.

카탈로그는 화면의 선택지이고, 로더는 그 선택을 실제로 여는 코드다. **둘이
갈리면 고를 수는 있는데 시작만 하면 죽는 항목이 생긴다** — RT-DETR 를 넣으면서
정확히 그럴 뻔했다: `YOLO()` 는 RT-DETR 가중치를 못 연다.
"""

import sys
from pathlib import Path

import pytest

_DAEMONS = Path(__file__).resolve().parents[2] / "daemons"
sys.path.insert(0, str(_DAEMONS))


def _catalog():
    from app.routers.vision import _YOLO_CATALOG

    return _YOLO_CATALOG


def test_rtdetr_is_offered():
    """요청받은 대로 목록에 있어야 한다."""
    files = [m["file"] for m in _catalog()]
    assert any(f.startswith("rtdetr") for f in files), "RT-DETR 가 목록에 없다"


def test_every_catalogued_model_has_a_loader():
    """⚠ **카탈로그와 로더는 같이 움직여야 한다.**

    아키텍처마다 클래스가 다르다. 목록에만 추가하면 화면에서는 보이는데
    시작하는 순간 죽는다.
    """
    from detector_loader import load_detector
    import inspect

    src = inspect.getsource(load_detector)
    for m in _catalog():
        stem = m["file"].lower()
        if stem.startswith("rtdetr"):
            assert "RTDETR" in src, f"{m['file']} 을 열 클래스가 없다"
        else:
            assert "YOLO" in src, f"{m['file']} 을 열 클래스가 없다"


def test_the_loader_picks_by_weight_name():
    """이름으로 고른다 — 확장자만 보면 전부 `.pt` 라 구분이 안 된다."""
    import inspect
    from detector_loader import load_detector

    src = inspect.getsource(load_detector)
    assert 'startswith("rtdetr")' in src, "RT-DETR 를 안 가려낸다"
    # 경로가 붙어 와도(커스텀 가중치는 절대경로다) 파일명으로 판단해야 한다
    assert 'rsplit("/", 1)[-1]' in src, "경로가 붙으면 판정이 틀어진다"


@pytest.mark.parametrize("daemon", ["yolod.py", "yolo_prelabel.py", "yolo_traind.py"])
def test_all_three_daemons_share_the_loader(daemon):
    """한 곳만 고치면 데모에서는 되는데 학습에서는 안 되는 식으로 갈린다."""
    src = (_DAEMONS / daemon).read_text()
    assert "load_detector" in src, f"{daemon} 이 공용 로더를 안 쓴다"
    assert "YOLO(args.model)" not in src, f"{daemon} 이 아직 직접 연다"


def test_unverified_numbers_are_left_blank():
    """모르는 값을 그럴듯하게 적으면 화면이 조용히 거짓말을 한다.

    UI 는 `params_m == null` 을 이미 견딘다 — 지어내는 것보다 비우는 게 낫다.
    """
    rt = [m for m in _catalog() if m["file"].startswith("rtdetr")]
    assert rt and all(m["params_m"] is None for m in rt), "확인 안 한 수치가 적혀 있다"
    assert all(m["size_mb"] for m in rt), "실측한 용량은 있어야 한다"
