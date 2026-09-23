"""데이터셋의 **코드베이스 버전 표식** — 없으면 임대 GPU 가 받지도 못한다.

lerobot 은 로컬에 데이터셋이 없으면 Hub 에 `v3.0` 같은 버전을 물어보고, 없으면
`RevisionNotFoundError` 를 낸다 — **폴백이 없다**(`datasets/utils.py::get_safe_version`).
다운로드가 시작조차 안 되므로, 표식 없는 저장소는 원격 학습이 통째로 막힌다.

실측(2026-09-23), 로컬에 아무것도 없는 상태(= 임대 기계와 같은 조건):

    wego-mink/sim_two_box_3_120  (표식 없음) → RevisionNotFoundError 계열로 죽음
    wego-hansu/sim_data2         (v3.0)      → 성공, 스스로 받아서 씀

이 파일이 지키는 것 둘: 올릴 때 표식을 달 것, 그리고 **빌리기 전에** 막을 것.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.training import _require_dataset_versioned

REPO = "org/name"
SRC = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────────────────────
# 올릴 때 단다
# ─────────────────────────────────────────────────────────────────────────────

def test_the_upload_path_tags_what_it_uploads():
    """⚠ 허브로 가는 길이 둘인데 한쪽만 달고 있었다 — 녹화 푸시는
    `dataset.push_to_hub(tag_version=True)` 라 달리고, 화면의 [업로드] 는
    `hf upload` 라 **폴더만 올렸다.** 시뮬에서 만든 데이터셋이 전부 그 경우였다."""
    up = (SRC / "scripts" / "upload_dataset.py").read_text()
    assert "create_tag" in up, "업로드가 태그를 안 단다"
    assert "codebase_version" in up, "태그 이름을 info.json 에서 안 읽는다"


def test_the_tag_is_only_written_after_a_successful_upload():
    """⚠ 없는 리비전은 가리킬 수 없고, 실패한 업로드를 "쓸 수 있다" 로 표시하면 안 된다."""
    from conftest import code_only

    up = code_only((SRC / "scripts" / "upload_dataset.py").read_text())
    body = up.split("rc = subprocess.run", 1)[1]
    assert body.index("return rc") < body.index("tag("), \
        "업로드 실패를 확인하기 전에 태그를 단다"


def test_a_failed_tag_does_not_fail_the_upload():
    """⚠ 다 올라간 것을 사람이 다시 올리게 만들면 안 된다 — 사실만 말하고 끝낸다."""
    from conftest import code_only

    up = code_only((SRC / "scripts" / "upload_dataset.py").read_text())
    assert "def tag(" in up and "return False" in up.split("def tag(", 1)[1], \
        "태그 실패가 예외로 올라간다"


# ─────────────────────────────────────────────────────────────────────────────
# 빌리기 **전에** 막는다
# ─────────────────────────────────────────────────────────────────────────────

def _versions(monkeypatch, value):
    monkeypatch.setattr("app.services.hub_client.dataset_versions", lambda r: value)


def test_a_dataset_without_a_version_is_refused_before_renting(monkeypatch):
    """⚠ 안 막으면 기계를 만들고 스택을 깔고(실측 slim 6분 24초) 나서야 죽는다 —
    **돈을 쓴 뒤에** 안다."""
    _versions(monkeypatch, [])
    with pytest.raises(HTTPException) as e:
        asyncio.run(_require_dataset_versioned(REPO))
    assert e.value.status_code == 400
    assert "create_tag" in e.value.detail, "고치는 법을 안 말한다"


def test_a_tagged_dataset_passes(monkeypatch):
    _versions(monkeypatch, ["v3.0"])
    asyncio.run(_require_dataset_versioned(REPO))


def test_not_knowing_is_not_a_refusal(monkeypatch):
    """⚠ 조회 실패와 "표식이 없다" 는 다르다. 망이 느리다고 학습을 못 걸게 하면
    그게 더 나쁘다 — `_require_push_permission` 과 같은 규칙이다."""
    _versions(monkeypatch, None)
    asyncio.run(_require_dataset_versioned(REPO))


def test_no_dataset_no_check(monkeypatch):
    _versions(monkeypatch, [])
    asyncio.run(_require_dataset_versioned(""))


def test_both_rent_paths_check_it():
    """⚠ `/rent` 와 `/train-on` 둘 다 원격 학습을 건다. 한쪽에만 있으면 다른 쪽으로
    같은 사고가 그대로 난다 — 이 저장소가 `RentRequest` 에서 이미 겪은 모양이다."""
    from conftest import code_only

    src = code_only((SRC / "app" / "routers" / "cloud.py").read_text())
    assert src.count("_require_dataset_versioned(") == 2, \
        "임대 경로 둘 중 한쪽이 빠졌다"


def test_local_training_is_not_blocked():
    """⚠ 로컬은 `--dataset.root` 로 받아 둔 파일을 직접 가리켜 이 경로를 안 탄다.
    여기서 막으면 방금 고친 것이 도로 막힌다."""
    from conftest import code_only

    src = code_only((SRC / "app" / "routers" / "training.py").read_text())
    block = src.split("await _require_push_permission(params[", 1)[1][:400]
    assert "is_remote" in block, "원격 여부를 안 보고 막는다"
