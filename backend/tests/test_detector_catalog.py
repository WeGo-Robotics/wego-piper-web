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
    from app.routers.vision import _DETECTOR_CATALOG

    return _DETECTOR_CATALOG


def test_the_catalog_offers_only_rtdetr():
    """⚠ **ultralytics 는 AGPL-3.0 이다.** 목록에 YOLO 계열이 하나라도 남으면,
    그걸 고르는 순간 AGPL 라이브러리가 필요해진다 — 라이선스를 정리한 의미가
    사라진다. 그래서 "RT-DETR 이 있다"가 아니라 **"그것뿐이다"** 를 본다."""
    fams = {m["family"] for m in _catalog()}
    assert fams and all("RT-DETR" in f for f in fams), f"YOLO 계열이 남아 있다: {fams}"
    for m in _catalog():
        assert not m["file"].endswith(".pt"), f"{m['file']} 은 ultralytics 형식이다"


def test_the_weights_come_from_the_original_publisher():
    """⚠ **아키텍처가 Apache 인 것과 가중치가 Apache 인 것은 다르다.** 예전
    `rtdetr-l.pt` 는 아키텍처만 Apache 이고 가중치는 ultralytics 배포본이라
    AGPL 이었다. 출처를 안 바꾸면 이름만 바뀐다."""
    for m in _catalog():
        assert m["file"].startswith("PekingU/"), \
            f"{m['file']}: 원 배포본이 아니다"


def test_the_loader_does_not_touch_ultralytics():
    """로더가 AGPL 라이브러리를 부르면 그 한 줄로 전부 되돌아간다."""
    from conftest import python_code_only

    # ⚠ docstring 까지 걷어낸다 — 이 파일은 **왜** ultralytics 를 버렸는지
    #   설명하느라 그 낱말을 여러 번 쓴다. 설명문을 코드로 세면 안 된다.
    src = python_code_only((_DAEMONS / "detector_loader.py").read_text())
    # ⚠ **낱말이 아니라 import 를 본다.** 이 파일은 `.pt` 를 거절하면서 그 이유를
    #   사용자에게 말하느라 "ultralytics" 를 **메시지 안에** 쓴다. 낱말로 세면
    #   친절한 에러 문구가 검사를 실패시킨다.
    assert "import ultralytics" not in src and "from ultralytics" not in src, \
        "로더가 아직 ultralytics 를 import 한다"
    assert "transformers" in src, "무엇으로 여는지 알 수 없다"


def test_the_loader_refuses_ultralytics_weights():
    """⚠ 조용히 무시하면 "왜 내 가중치가 안 먹지"로 돌아온다. `.pt` 는 열 수
    없다고 **말해야** 한다 — 그 형식을 열려면 AGPL 라이브러리가 필요하다."""
    from detector_loader import load_detector

    with pytest.raises(ValueError, match="ultralytics"):
        load_detector("yolo11n.pt")


def test_unverified_numbers_are_left_blank():
    """모르는 값을 그럴듯하게 적으면 화면이 조용히 거짓말을 한다.

    UI 는 `params_m == null` 을 이미 견딘다 — 지어내는 것보다 비우는 게 낫다.
    """
    cat = _catalog()
    assert cat and all(m["params_m"] is None for m in cat), "확인 안 한 수치가 적혀 있다"
    assert all(m["size_mb"] for m in cat), "실측한 용량은 있어야 한다"


@pytest.mark.parametrize("daemon", ["yolod.py", "yolo_prelabel.py", "yolo_traind.py"])
def test_all_three_daemons_share_the_loader(daemon):
    """한 곳만 고치면 데모에서는 되는데 학습에서는 안 되는 식으로 갈린다."""
    from conftest import python_code_only

    src = python_code_only((_DAEMONS / daemon).read_text())
    assert "load_detector" in src or "rtdetr_train" in src, f"{daemon} 이 공용 조각을 안 쓴다"
    assert "ultralytics" not in src, f"{daemon} 이 아직 ultralytics 를 쓴다"


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


def test_predict_returns_a_list_like_ultralytics():
    """⚠ **회귀.** ultralytics 는 이미지별 결과 **리스트**를 주고, 두 데몬 모두
    `model.predict(...)[0]` 으로 첫 장을 꺼낸다. 어댑터가 하나만 돌려주자
    `TypeError: '_Result' object is not subscriptable` 로 검출이 즉사했다 —
    모델은 멀쩡히 로드된 뒤였으므로 "켜자마자 꺼진다"로만 보였다.
    """
    import inspect

    from detector_loader import RTDetr

    src = inspect.getsource(RTDetr.predict)
    assert "return [" in src, "리스트로 안 돌려준다"
    for daemon in ("yolod.py", "yolo_prelabel.py"):
        body = (_DAEMONS / daemon).read_text()
        if "model.predict(" in body:
            assert ")[0]" in body, f"{daemon} 의 호출 형태가 바뀌었다 — 어댑터도 같이 봐야 한다"
