"""Hub 검색 — 모델과 데이터셋이 **같은 규칙**을 쓴다.

⚠ 갈라져 있어서 한쪽만 죽었다. 모델 쪽에는 `except TypeError` 폴백이 있었고
데이터셋 쪽에는 없었다. 그래서 라이브러리가 인자를 바꿨을 때 **데이터셋 검색만
500 으로 죽었고, 화면에는 "결과 없음" 으로만 보였다.**
"""

import inspect
from pathlib import Path

from huggingface_hub import HfApi

SRC = Path(__file__).resolve().parents[1] / "app" / "services" / "hub_client.py"


def test_the_sort_arguments_exist_in_the_installed_library():
    """⚠ **설치된 라이브러리에 실제로 있는 인자만 쓴다.** `direction` 은
    huggingface_hub 1.x 에서 없어졌는데 코드가 계속 넘기고 있었다."""
    from app.services import hub_client

    for fn in (HfApi.list_models, HfApi.list_datasets):
        params = inspect.signature(fn).parameters
        assert "sort" in params, f"{fn.__name__} 에 sort 가 없다 — 코드를 맞춰야 한다"
        assert "direction" not in params, (
            f"{fn.__name__} 이 direction 을 다시 받는다 — 정렬 방향을 확인할 것")
    assert hub_client._SORT in ("downloads", "likes", "last_modified",
                                "created_at", "trending_score")


def test_models_and_datasets_search_the_same_way():
    """⚠ 한쪽에만 폴백이 있으면, 라이브러리가 바뀔 때 **한쪽만 조용히 다르게**
    동작한다. 실제로 모델은 폴백으로 살았는데 그 폴백이 `sort` 를 통째로 버려
    정렬이 사라졌고, 데이터셋은 아예 죽었다."""
    from conftest import python_code_only

    # ⚠ **주석·docstring 을 걷어내고 본다.** 이 파일은 "예전에 `except TypeError`
    #   폴백이 있었다" 는 내력을 적어 두는데, 그걸 코드로 세면 설명문 때문에
    #   검사가 실패한다.
    src = python_code_only(SRC.read_text())
    assert "direction=" not in src, "없어진 인자를 아직 넘긴다"
    assert src.count("sort=_SORT") == 2, "모델·데이터셋이 같은 정렬을 안 쓴다"
    assert "except TypeError" not in src, \
        "인자 오류를 삼킨다 — 조용히 다르게 동작하느니 죽는 편이 낫다"


def test_search_actually_returns_rows():
    """⚠ 네트워크가 필요하다. 못 나가면 건너뛴다 — 이 검사가 CI 를 막으면 안 된다."""
    import pytest

    from app.services.hub_client import _search_datasets, _search_models

    try:
        ds = _search_datasets(limit=3)
        ms = _search_models(limit=3)
    except Exception as e:
        pytest.skip(f"Hub 에 못 나감: {e}")
    assert ds, "데이터셋 검색이 비었다"
    assert ms, "모델 검색이 비었다"
    # 다운로드 내림차순 — 폴백이 정렬을 버리면 여기서 걸린다
    dl = [d["downloads"] for d in ds if d["downloads"] is not None]
    assert dl == sorted(dl, reverse=True), f"정렬이 안 걸렸다: {dl}"
