"""릴리즈 노트 — **가장 최근 태그가 적혀 있는가.**

⚠ 이 테스트가 있는 이유: `deploy/RELEASE-CHECKLIST.md` 의 배포 이력 표가
**다섯 버전 동안 비어 있었다**(v0.3.10~v0.4.3). 릴리즈할 때는 이미지 굽는 데
정신이 팔려 문서를 잊고, 다음 사람은 "직전에 무엇을 올렸나" 를 커밋 로그에서
다시 캔다 — 그게 이 문서를 두는 이유 자체다.

기계가 확인할 수 있는 것은 **최신 태그의 항목이 있는가** 뿐이다. 내용이 맞는지는
사람이 봐야 한다. 그래도 "통째로 빠뜨렸다" 는 잡을 수 있다.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"


def _latest_tag() -> str | None:
    try:
        out = subprocess.run(["git", "tag", "--sort=-v:refname"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    tags = [t for t in out.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return tags[0] if tags else None


def test_the_changelog_is_at_the_root():
    """루트에 있어야 사람이 찾는다 — 저장소를 열면 보이는 자리다."""
    assert CHANGELOG.is_file(), "CHANGELOG.md 가 루트에 없다"


def test_the_newest_tag_has_an_entry():
    tag = _latest_tag()
    if not tag:
        pytest.skip("git 태그를 못 읽었다")
    body = CHANGELOG.read_text()
    assert re.search(rf"^## {re.escape(tag)}\b", body, re.M), (
        f"{tag} 항목이 CHANGELOG.md 에 없다 — 릴리즈했으면 무엇이 달라졌는지 "
        f"적어야 한다. 이력이 비면 다음 사람이 커밋 로그를 다시 캔다."
    )


def test_the_readme_points_at_it():
    """찾을 수 있어야 있는 것이다."""
    assert "CHANGELOG.md" in (ROOT / "README.md").read_text(), \
        "README 가 변경 이력을 안 가리킨다"
