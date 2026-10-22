"""특정 검출 모델의 이름을 **저장소 어디에도** 쓰지 않는다 (feature/plugins.md §14).

구현이 무엇이든 갈아끼울 수 있어야 하고, **라이선스가 그 교체를 강제할 수도
있다** — `ultralytics` 는 AGPL-3.0 이고 지금 쓰는 RT-DETR 은 Apache-2.0 이다.
검출기는 이미 바뀌었는데 이름이 남아 있었다. 예전에는 화면 문구만 막았다 —
이제 범위는 **트리 전체**다: 파일·모듈·API 경로·설정 키·데이터 디렉토리·유닛 이름·
버스 키·문서·주석.

예외(허용 목록)는 셋이다:
- `CHANGELOG.md` — 이력은 다시 쓰지 않는다
- `feature/plugins.md` — 개명 표가 옛 이름을 적는다 (무엇에서 무엇으로)
- **삭제 예정** 파일 — 옛 경로 리다이렉트·옛 디렉토리 이관. 한 릴리스 뒤 지운다.
  이 파일들은 본문에 "삭제 예정" 이라고 적어야 한다 — 그래야 지울 때 찾는다
- 이 테스트 자신 (금지 단어를 정규식으로 든다)
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_BRAND = re.compile(r"yolo", re.IGNORECASE)

_HISTORY = {"CHANGELOG.md", "feature/plugins.md", "backend/tests/test_no_model_branding.py"}
_SCHEDULED_FOR_REMOVAL = {
    "frontend/src/config/legacyRoutes.tsx",
    "backend/app/services/legacy_paths.py",
    "backend/tests/test_legacy_paths.py",
}
_SKIP_PREFIX = ("backend/data/", "frontend/dist/", "frontend/node_modules/")


def _repo_files() -> list[str]:
    """git 이 아는 파일 + 아직 안 올린 파일. 무시 목록(캐시·빌드·venv)은 빠진다."""
    def ls(*extra: str) -> list[str]:
        out = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", *extra],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [l for l in out.splitlines() if l]
    return sorted(set(ls()) | set(ls("--others", "--exclude-standard")))


def _text(rel: str) -> str | None:
    p = ROOT / rel
    if not p.is_file():
        return None
    data = p.read_bytes()
    if b"\0" in data[:4096]:
        return None                       # 바이너리(pdf·png·…)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def test_the_tree_is_free_of_the_model_name():
    hits: list[str] = []
    for rel in _repo_files():
        if rel in _HISTORY or rel in _SCHEDULED_FOR_REMOVAL or rel.startswith(_SKIP_PREFIX):
            continue
        if _BRAND.search(rel):
            hits.append(f"{rel}  (파일 이름)")
            continue
        src = _text(rel)
        if src is None:
            continue
        for i, line in enumerate(src.splitlines(), 1):
            if _BRAND.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not hits, "모델 이름이 남아 있다 (feature/plugins.md §14-1 의 새 이름으로):\n" + "\n".join(hits)


@pytest.mark.parametrize("rel", sorted(_SCHEDULED_FOR_REMOVAL))
def test_legacy_files_say_they_are_temporary(rel):
    """옛 이름을 적을 수 있는 파일은 **스스로 삭제 예정이라고 말해야** 한다."""
    src = _text(rel)
    assert src is not None, f"{rel} 이 없다 — 허용 목록에서도 지워라"
    assert "삭제 예정" in src, f"{rel} 에 '삭제 예정' 표시가 없다"


def test_the_activity_label_is_model_neutral():
    """⚠ 상태바와 409 메시지가 **이 문자열을 그대로** 쓴다 — 화면 문구다."""
    from app.services.exclusivity import Activity, LABELS

    label = LABELS[Activity.DETECTOR_TRAIN]
    assert not _BRAND.search(label), f"활동 라벨에 모델 이름이 있다: {label}"
    assert Activity.DETECTOR_TRAIN.value == "detector_train"


def test_the_model_catalogue_still_names_the_real_files():
    """받는 파일이 무엇인지는 **정확히** 말해야 한다.

    우리 제품 문구에서 브랜드를 빼는 것과, 제3자 산출물의 식별자를 숨기는 것은
    다르다. 후자는 사용자가 무엇을 내려받는지도, 그 라이선스가 무엇인지도 알 수
    없게 만든다. 카탈로그 항목은 **배포자/모델 id**(`PekingU/rtdetr_v2_r18vd`)다.
    """
    from app.routers.vision import _DETECTOR_CATALOG as cat

    assert cat, "카탈로그가 비었다"
    assert all("/" in m["file"] for m in cat), "어느 배포본인지 알 수 없다"
