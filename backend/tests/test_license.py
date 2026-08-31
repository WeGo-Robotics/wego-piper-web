"""라이선스 표기.

공개 배포에서 라이선스는 **코드만큼 실재하는 산출물**이다. 없거나 어긋나면
받은 사람이 무엇을 할 수 있는지 알 수 없고, 그건 버그와 달리 조용히 남는다.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LICENSE = REPO / "LICENSE"
NOTICES = REPO / "THIRD-PARTY-NOTICES.md"


def test_the_repository_says_what_you_may_do_with_it():
    """⚠ **예전에는 `LICENSE` 가 아예 없었다.** 라이선스 미표기는 "자유롭게 쓰라"가
    아니라 기본 저작권 — 받은 사람에게 **아무 권리도 주지 않는** 상태다.
    공개 저장소에 그대로 두면 아무도 쓸 수 없다."""
    assert LICENSE.is_file(), "LICENSE 가 없다"
    body = LICENSE.read_text()
    assert "Apache License" in body and "Version 2.0" in body
    assert "TERMS AND CONDITIONS" in body, "본문이 잘렸다"
    assert body.splitlines()[0].startswith("Copyright "), "저작권 표기가 없다"


def test_the_licence_matches_the_ecosystem_we_build_on():
    """LeRobot 이 Apache-2.0 이다. 다른 것을 고르면 그 생태계에 코드를 돌려줄 수
    없고, 우리 쪽으로 받아오는 것도 매번 검토 대상이 된다."""
    body = LICENSE.read_text()
    # Apache-2.0 을 다른 허용형과 가르는 것은 **특허 조항**이다
    assert "Grant of Patent License" in body, "Apache-2.0 전문이 아니다"


def test_every_package_declares_the_same_licence():
    """⚠ `pyproject.toml` 의 선언이 없으면 **설치된 배포판의 메타데이터가 빈다.**
    우리가 만든 고지 스캔조차 우리 패키지를 "메타없음"으로 셌다 — 남이 우리 것을
    담아 배포할 때 무엇인지 알 수 없다는 뜻이다."""
    import tomllib

    seen = 0
    for f in sorted(REPO.glob("**/pyproject.toml")):
        if ".git" in f.parts or "node_modules" in f.parts:
            continue
        data = tomllib.loads(f.read_text())
        lic = data.get("project", {}).get("license")
        assert lic == "Apache-2.0", f"{f.relative_to(REPO)}: license={lic!r}"
        seen += 1
    assert seen >= 10, f"pyproject 를 {seen}개밖에 못 찾았다 — 검사가 헛돈다"


def test_the_third_party_notices_exist_and_say_the_hard_parts():
    """⚠ 고지는 **의무**다(허용형도 저작권 고지를 요구한다). 그리고 카피레프트를
    빠뜨리면 그게 가장 비싸게 돌아온다."""
    assert NOTICES.is_file(), "THIRD-PARTY-NOTICES.md 가 없다"
    body = NOTICES.read_text()
    for must in ("LGPL", "MPL", "NVIDIA", "python-can", "pyyaml-include"):
        assert must in body, f"고지에 {must} 가 없다"


def test_the_notices_record_that_agpl_is_gone():
    """⚠ **이 저장소는 한때 AGPL 이었다** — `ultralytics` 가 검출기였다. 그것이
    없어졌다는 사실과 이유가 남아야, 나중에 누가 다시 넣기 전에 멈춘다."""
    body = NOTICES.read_text()
    assert "ultralytics" in body and "AGPL" in body, "왜 없앴는지가 안 적혀 있다"
    assert "**AGPL 은 0개다.**" in body, "AGPL 이 0 이라는 확인이 없다"
    assert "| AGPL | **0** |" in body, "요약표의 AGPL 이 0 이 아니다"
