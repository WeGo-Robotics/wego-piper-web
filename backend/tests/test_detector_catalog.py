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


# ── 기본 선택은 이미 받아둔 가중치로 (화면) ──────────────────────────────────

_DEMO = (Path(__file__).resolve().parents[2] / "frontend" / "src"
         / "pages" / "YoloDemoPage.tsx")


def test_the_list_still_shows_models_that_need_downloading():
    """⚠ 목록에서 빼면 **새 기기에서 첫 모델을 받을 길이 없어진다.**

    받아야 하는 것은 `(다운로드 필요)` 로 그대로 보인다 — 바뀌는 것은 기본값뿐이다.
    """
    src = _DEMO.read_text()
    assert "m.downloaded === false" in src, "받아야 하는 항목 표시가 사라졌다"
    assert ".filter((m) => m.downloaded" not in src, "목록에서 걸러내고 있다"


def test_the_default_moves_to_a_local_weight():
    """시작을 눌렀는데 100MB 를 받느라 멈춘 것처럼 보이지 않게."""
    src = _DEMO.read_text()
    body = src.split("const pickLocalDefault", 1)[1].split("\n  const ", 1)[0]
    assert "m.downloaded === true" in body, "로컬 여부를 안 본다"


def test_an_explicit_choice_is_not_overridden():
    """`?model=` 는 학습 직후 단축 경로다. 고른 것을 되돌리는 화면이 제일 나쁘다."""
    src = _DEMO.read_text()
    body = src.split("const pickLocalDefault", 1)[1].split("\n  const ", 1)[0]
    assert "urlModel.current" in body, "URL 로 지정한 모델을 덮어쓴다"
    assert "defaultFixed.current" in body, "폴링 때마다 선택이 되돌아간다"


def test_deleting_the_selected_model_falls_back_to_a_local_one():
    """⚠ **회귀** — 삭제 뒤 'yolo11n.pt' 를 박아 넣었다. 그 파일이 이 기기에
    없으면 선택이 곧장 '받아야 하는 모델' 로 옮겨 앉는다."""
    src = _DEMO.read_text()
    body = src.split("const handleDeleteModel", 1)[1].split("\n  const ", 1)[0]
    assert "downloaded === true" in body, "삭제 뒤 로컬 가중치를 안 고른다"


# ── 런타임 산출물이 저장소에 안 들어가게 ────────────────────────────────────

_REPO = Path(__file__).resolve().parents[2]


def test_runtime_detection_artifacts_are_ignored():
    """⚠ `*.pt` 만으로는 부족하다.

    학습 유닛은 가중치 **옆에 지표 JSON** 을 남기고, 라벨러는 이미지와 `.txt`
    라벨을 남긴다. 확장자로만 막으면 그것들이 그대로 커밋에 섞인다.
    """
    import subprocess

    samples = [
        "backend/data/yolo_models/best.pt",
        "backend/data/yolo_models/best.json",      # 학습 지표 곁 파일
        "backend/data/yolo_datasets/s/images/a.jpg",
        "backend/data/yolo_datasets/s/labels/a.txt",
    ]
    for rel in samples:
        r = subprocess.run(["git", "check-ignore", "-q", rel],
                           cwd=_REPO, capture_output=True)
        assert r.returncode == 0, f"{rel} 이 커밋에 섞일 수 있다"


def test_the_directories_themselves_survive():
    """디렉토리가 사라지면 첫 실행이 mkdir 부터 해야 한다 — `.gitkeep` 을 남긴다."""
    import subprocess

    for rel in ("backend/data/yolo_models/.gitkeep",
                "backend/data/yolo_datasets/.gitkeep"):
        assert (_REPO / rel).exists(), f"{rel} 이 없다"
        r = subprocess.run(["git", "check-ignore", "-q", rel],
                           cwd=_REPO, capture_output=True)
        assert r.returncode != 0, f"{rel} 까지 무시하면 디렉토리가 안 남는다"
