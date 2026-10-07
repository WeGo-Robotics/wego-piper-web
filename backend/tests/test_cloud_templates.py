"""학습 템플릿 — **계정마다 자기 것을 만든다.**

⚠ Vast 템플릿은 만든 계정의 것이고 조회도 `search templates private=true` 다. 그래서 다른
계정으로 들어가면 `piper-train full cu126` 이 **아예 안 보인다**(사용자 보고 2026-10-07).
지금까지 화면은 "템플릿 없음" 빨간불만 띄우고 **만드는 길을 안 줬다.**

⚠ 이미지는 막는 요인이 아니다 — `ghcr.io/wego-robotics/piper-train` 은 익명 pull 이
된다(실측 2026-10-07). 템플릿만 만들면 다른 계정에서도 그대로 돈다.
"""

from pathlib import Path

from app.services.cloud import templates as T


class _T:
    def __init__(self, name):
        self.name = name


def test_we_ship_both_variants():
    names = [s.name for s in T.SPECS]
    assert names == ["piper-train full cu126", "piper-train slim cu126"]


def test_existing_templates_are_not_made_again():
    """⚠ 같은 이름이 둘이면 사람이 어느 것을 고를지 알 수 없고 `hash_id` 도 갈린다."""
    assert T.missing([_T("piper-train full cu126")]) == [T.SPECS[1]]
    assert T.missing([_T(s.name) for s in T.SPECS]) == []
    assert T.missing([]) == list(T.SPECS)


def test_the_tag_rolls_instead_of_being_pinned_to_a_date():
    """⚠ 살아 있는 템플릿은 날짜 태그(`full-0.5.0-cu126-20260918`)를 들고 있다. 그걸
    코드에 박으면 이미지를 다시 구울 때마다 **새 계정만 옛 이미지를 받는다** — 코드가
    조용히 낡는 모양이다. `build-train.sh` 가 구울 때마다 굴러가는 태그를 같이 민다."""
    import re

    for spec in T.SPECS:
        assert spec.tag in ("full-cu126", "slim-cu126"), spec.tag
        # ⚠ `cu126` 에도 숫자는 있다 — 찾는 것은 **날짜**(YYYYMMDD)와 박힌 버전이다.
        assert not re.search(r"\d{8}", spec.tag), f"날짜가 박힌 태그다: {spec.tag}"
        assert not re.search(r"\d+\.\d+\.\d+", spec.tag), f"버전이 박힌 태그다: {spec.tag}"


def test_the_search_filter_is_one_string_not_hand_built_json():
    """⚠ `--search_params` 한 줄이 저장된 `extra_filters` JSON 이 된다 — 버리는 템플릿으로
    두 번 예행해 글자까지 같은 것을 확인했다. 손으로 JSON 을 만들면 갈린다."""
    assert "{" not in T.SEARCH_PARAMS, "JSON 을 손으로 만든다"
    for key in ("verified=true", "rentable=true", 'gpu_name="RTX 4090"',
                "cuda_max_good>=12.4"):
        assert key in T.SEARCH_PARAMS, key


def test_the_provider_sends_every_field_the_template_needs():
    """⚠ 디스크를 안 주면 기본값으로 떠서 이미지 전개가 안 들어가고 pull 이 실패한다.
    `--ssh --direct` 가 빠지면 접속 모드가 달라진다."""
    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[1]
                     / "app/services/cloud/providers/vast.py").read_text())
    body = src.split("def create_template", 1)[1].split("\n    def ", 1)[0]
    for flag in ("--name", "--image", "--image_tag", "--onstart-cmd",
                 "--disk_space", "--ssh", "--direct", "--search_params"):
        assert flag in body, f"{flag} 를 안 보낸다"


def test_one_failure_does_not_stop_the_other():
    """둘 중 하나만 있어도 학습은 걸 수 있다 — 첫 실패에서 멈추면 그 기회를 버린다."""
    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[1]
                     / "app/routers/cloud.py").read_text())
    body = src.split("async def create_cloud_templates", 1)[1].split("\nasync def ", 1)[0]
    assert "failed.append" in body and "continue" not in body.split("failed.append")[0][-80:], \
        "실패를 모으지 않고 던진다"
    assert "for spec in todo" in body


def test_the_printed_output_is_not_parsed_as_json():
    """⚠ 실측(2026-10-07): `create template` 은 `--raw` 를 줘도 JSON 이 아니라
    `New Template: {파이썬 repr}` 을 찍는다. 그걸 `_raw` 로 읽으려다 **템플릿은
    만들어졌는데 실패로 보고**했다 — 사람이 다시 누르면 같은 이름이 둘이 된다.

    만들어졌는지는 **목록에 물어본다.** 그게 어차피 `--template_hash` 가 믿는 자리이고,
    출력 형식이 바뀌어도 안 깨진다.
    """
    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[1]
                     / "app/services/cloud/providers/vast.py").read_text())
    body = src.split("def create_template", 1)[1].split("\n    def ", 1)[0]
    assert "self._raw(" not in body, "JSON 이 아닌 출력을 JSON 으로 읽는다"
    assert "self.templates(refresh=True)" in body, "만들어졌는지 목록에 안 물어본다"
