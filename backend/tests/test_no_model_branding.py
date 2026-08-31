"""화면에 특정 검출 모델의 이름을 쓰지 않는다.

구현이 무엇이든 갈아끼울 수 있어야 하고, **라이선스가 그 교체를 강제할 수도
있다** — 지금 쓰는 `ultralytics` 는 AGPL-3.0 이고 RT-DETR 같은 대안은 Apache-2.0
이다. 화면 문구에 모델 이름이 박혀 있으면 교체할 때마다 문구를 다시 훑어야 한다.

⚠ **범위는 화면 문구뿐이다.** 모듈·데몬·API 경로·열거자 이름은 내부라 그대로
둔다. 이름을 가린다고 라이선스 의무가 사라지지도 않는다 — 의존성은 그대로다.
"""

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

_PAGES = ["config/pages.ts", "pages/VisionPage.tsx", "pages/YoloDemoPage.tsx",
          "pages/YoloTrainPage.tsx", "pages/EpisodesPage.tsx"]

# 화면 문구는 **전부 한글**이다. 경로(`/yolo-demo`)·컴포넌트(`YoloDemoPage`)·
# 변수(`yoloTarget`)는 ASCII 뿐이라, 한글이 있는 줄만 보면 내부 이름은 저절로
# 빠진다 — 이번 범위의 경계가 마침 거기다.
_HANGUL = re.compile(r"[가-힣]")
_INTERP = re.compile(r"\$\{[^}]*\}")     # 템플릿 안 식별자는 화면에 안 나온다
_BRAND = re.compile(r"yolo", re.IGNORECASE)


# 주석은 화면에 안 나온다 — 줄 **끝**에 붙은 것까지 걷어내야 한다.
# (`// 구버전 yolod` 같은 설명이 자꾸 걸렸다. 이 저장소에서 반복된 함정이라
#  `conftest.code_only` 도 같은 이유로 있다.)
_TRAILING = re.compile(r"//.*$|\{/\*.*?\*/\}")


def _visible(src: str) -> list[str]:
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("*", "//", "/**")):
            continue
        s = _TRAILING.sub("", s)
        if _HANGUL.search(s):
            out.append(s)
    return out


@pytest.mark.parametrize("rel", _PAGES)
def test_no_model_name_in_what_the_user_reads(rel):
    """제목·라벨·알림 문구에 모델 이름이 없어야 한다.

    데몬 이름(`yolod`)이 토스트로 나가는 것도 화면 문구다 — '검출기'로 부른다.
    """
    bad = [ln for ln in _visible((_SRC / rel).read_text())
           if _BRAND.search(_INTERP.sub("", ln))]
    assert not bad, f"{rel} 화면 문구에 모델 이름이 남아 있다: {bad}"


def test_the_sidebar_group_is_model_neutral():
    """묶음 이름은 사이드바에 그대로 뜬다."""
    src = (_SRC / "config" / "pages.ts").read_text()
    groups = src.split("PAGE_GROUPS = [", 1)[1].split("]", 1)[0]
    assert not _BRAND.search(groups), f"묶음 이름에 모델 이름이 있다: {groups}"


def test_the_activity_label_is_model_neutral():
    """⚠ 상태바와 409 메시지가 **이 문자열을 그대로** 쓴다 — 화면 문구다
    (test_page_registry.py 가 프론트에 자기 사전을 못 두게 막고 있다)."""
    from app.services.exclusivity import Activity, LABELS

    label = LABELS[Activity.YOLO_TRAIN]
    assert not _BRAND.search(label), f"활동 라벨에 모델 이름이 있다: {label}"


def test_the_internal_identifier_is_left_alone():
    """열거자 값은 **내부**다. 여기까지 바꾸면 저장된 프리셋·상태가 깨진다 —
    이번 범위는 사람이 읽는 문구뿐이다."""
    from app.services.exclusivity import Activity

    assert Activity.YOLO_TRAIN.value == "yolo_train", "내부 식별자를 건드렸다"


def test_the_model_catalogue_still_names_the_real_files():
    """받는 파일이 무엇인지는 **정확히** 말해야 한다.

    우리 제품 문구에서 브랜드를 빼는 것과, 제3자 산출물의 식별자를 숨기는 것은
    다르다. 후자는 사용자가 무엇을 내려받는지도, 그 라이선스가 무엇인지도 알 수
    없게 만든다.
    """
    from app.routers.vision import _DETECTOR_CATALOG as cat

    # ⚠ 예전에는 `.pt` 파일명이 식별자였다. 이제는 **배포자/모델 id** 다
    #   (`PekingU/rtdetr_v2_r18vd`). 오히려 더 정확하다 — 어느 배포본을 받는지가
    #   드러나야 라이선스를 알 수 있고, 그게 이 검사의 원래 목적이다.
    assert cat, "카탈로그가 비었다"
    assert all("/" in m["file"] for m in cat), "어느 배포본인지 알 수 없다"
