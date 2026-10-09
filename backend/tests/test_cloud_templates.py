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


# ── 거절 사유가 버려지지 않는다 ─────────────────────────────────────────────
#
# ⚠ vastai 1.7 `create template` 은 서버가 거절해도 종료코드 0 으로 끝내고 사유를 stdout 에만
#   찍는다(HTTP 오류는 `The response is not valid JSON.` 으로 뭉개진다). 예전 코드는 그
#   stdout 을 버리고 "만들었지만 목록에 없습니다" 라고만 말했다 — 팀 계정에서 템플릿이 안
#   만들어지는데 이유를 볼 수 없었다.

def _provider(monkeypatch, *, printed: str, listed: list):
    from app.services.cloud.providers import vast

    p = vast.VastProvider("k")
    monkeypatch.setattr(vast.VastProvider, "_text", lambda self, args, **kw: printed)
    monkeypatch.setattr(vast.VastProvider, "templates", lambda self, **kw: listed)
    return p


def test_a_rejected_create_reports_what_the_cli_said(monkeypatch):
    import pytest

    p = _provider(monkeypatch, printed="The response is not valid JSON.\n", listed=[])
    with pytest.raises(RuntimeError) as e:
        p.create_template(T.SPECS[0], 698077)
    msg = str(e.value)
    assert "The response is not valid JSON." in msg, "CLI 가 한 말을 버린다"
    assert T.SPECS[0].name in msg
    assert "목록에 없습니다" not in msg, "거절을 '만들었지만' 이라고 말한다"
    assert len(msg) < 200, "라우터가 200자에서 자른다 — 사유가 잘린다"


def test_a_success_false_message_comes_through(monkeypatch):
    import pytest

    p = _provider(monkeypatch, printed="Template name already in use\n", listed=[])
    with pytest.raises(RuntimeError, match="Template name already in use"):
        p.create_template(T.SPECS[1], 698077)


def test_created_but_not_listed_keeps_its_own_message(monkeypatch):
    import pytest

    p = _provider(monkeypatch, printed="New Template: {'hash_id': 'abc'}\n", listed=[])
    with pytest.raises(RuntimeError, match="만들었지만 목록에 없습니다"):
        p.create_template(T.SPECS[0], 698077)


def test_success_is_decided_by_the_list_not_the_printout(monkeypatch):
    class _Row:
        name = T.SPECS[0].name
        hash_id = "h123"

    # 출력이 비어도 목록에 있으면 만들어진 것이다
    p = _provider(monkeypatch, printed="", listed=[_Row()])
    assert p.create_template(T.SPECS[0], 698077) == "h123"


# ── 같은 내용은 계정을 넘어 남의 것에 붙는다 ────────────────────────────────
#
# ⚠ 실측(2026-10-10): `create template` 이 `Existing Template Found: 754853. User
#   relationship added.` 를 `success: false` 로 돌려줬다. 그 템플릿의 `creator_id` 는 **내
#   계정이 아니었고**(711347 ↔ 698077), `search templates private=true` 는 "내 템플릿만" 이라
#   그걸 안 돌려줘 화면은 계속 "템플릿 없음" 이었다. 지워도 소용없다 — 다시 만들면 또 붙는다.
#   `desc` 에 계정 id 를 붙이자 `Template Created Successfully` 였다(판정은 `desc` 까지 본다).

def test_the_description_differs_per_account_so_it_cannot_attach_to_anothers():
    a = T.desc_for(T.SPECS[0], 698077)
    b = T.desc_for(T.SPECS[0], 711347)
    assert a != b, "계정이 달라도 같은 내용이라 남의 템플릿에 붙는다"
    assert T.SPECS[0].desc in a, "원래 설명을 지운다"


def test_the_same_account_gets_the_same_description_every_time():
    """⚠ 다시 눌러도 같은 문자열이어야 자기 템플릿에 맞는다 — 시각·난수를 넣으면 눌 때마다
    새 템플릿이 생겨 같은 이름이 늘어난다."""
    assert T.desc_for(T.SPECS[1], 698077) == T.desc_for(T.SPECS[1], 698077)


def test_without_an_account_id_nothing_is_made():
    """⚠ 표식 없이 보내면 남의 것에 붙어 처음 증상으로 돌아간다 — 모르면 만들지 않는다."""
    import pytest

    for bad in (None, 0):
        with pytest.raises(ValueError):
            T.desc_for(T.SPECS[0], bad)


def test_the_account_marker_is_what_the_cli_is_sent(monkeypatch):
    from app.services.cloud.providers import vast

    sent = []
    monkeypatch.setattr(vast.VastProvider, "_text",
                        lambda self, args, **kw: sent.append(args) or "")
    monkeypatch.setattr(vast.VastProvider, "templates", lambda self, **kw: [])
    import pytest

    with pytest.raises(RuntimeError):
        vast.VastProvider("k").create_template(T.SPECS[0], 698077)
    args = sent[0]
    assert args[args.index("--desc") + 1] == T.desc_for(T.SPECS[0], 698077)


def test_an_existing_template_of_someone_else_is_said_plainly(monkeypatch):
    """⚠ 이 문장은 '거절' 이 아니다 — 남의 템플릿에 붙은 것이고 그래서 목록에 없다."""
    import pytest

    p = _provider(monkeypatch,
                  printed="Existing Template Found: 754853. User relationship added.\n",
                  listed=[])
    with pytest.raises(RuntimeError) as e:
        p.create_template(T.SPECS[0], 698077)
    msg = str(e.value)
    assert "754853" in msg and T.SPECS[0].name in msg
    assert "목록에는 보이지 않습니다" in msg
    assert "받아 주지 않았습니다" not in msg, "거절이라고 말한다"
    assert len(msg) < 200
