"""설치 트러블슈팅 문서 — 스크립트가 찍는 말과 어긋나지 않는다.

받는 사람은 `apply.sh` 가 찍은 한 줄을 들고 문서를 연다. 문서가 그 말을 모르면
소용이 없다 — 스크립트의 처방(NEED_SUDO)이 문서에도 있어야 한다.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "install-troubleshooting.md"


def test_the_readme_and_checklist_point_at_the_troubleshooting_doc():
    assert DOC.exists()
    assert "docs/install-troubleshooting.md" in (REPO / "README.md").read_text()
    assert "install-troubleshooting.md" in (REPO / "deploy" / "RELEASE-CHECKLIST.md").read_text()


def test_every_sudo_prescription_apply_prints_is_in_the_doc():
    """apply.sh 가 `NEED_SUDO+=("…")` 로 찍는 명령의 첫 낱말(usermod·loginctl·mkdir·
    systemctl·sed·cp·udevadm)이 문서에 처방으로 있어야 한다."""
    import re
    apply = (REPO / "deploy" / "apply.sh").read_text()
    doc = DOC.read_text()
    heads = {m.split()[0] for m in re.findall(r'NEED_SUDO\+=\("([^"]+)"\)', apply)}
    missing = [h for h in heads if h not in doc]
    assert not missing, f"apply.sh 가 찍는 처방이 문서에 없다: {missing}"
    for phrase in ("다시 로그인", "insecure-registries", "enable-linger", "piper-build",
                   "video", "dialout", "libegl1", "piper-unitd", "piper-update", "!override"):
        assert phrase in doc, phrase
