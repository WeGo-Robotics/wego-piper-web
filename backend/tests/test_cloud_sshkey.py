"""게이트웨이 SSH 키 — **Piper Studio 가 직접 만든다** (feature/vast-training.md §9-1).

⚠ 사람의 키를 빌려 쓰면 배포판에선 아예 안 된다 — 컨테이너에 `~/.ssh` 가 없다.
⚠ `ssh-keygen` 으로 만들면 이미지에 그 바이너리가 없어(실측) 배포판에서만 깨진다.
   "소스에선 되고 컨테이너에선 안 되는" 것이 이 저장소가 반복해 겪은 모양이다.
"""

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def sshkey(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services.cloud import sshkey as mod

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    return mod


def test_reading_does_not_create_anything(sshkey, tmp_path):
    """⚠ 읽기가 파일을 만들면 놀란다 — 화면이 상태를 물어봤을 뿐인데 키가 생긴다."""
    got = sshkey.info()
    assert got["exists"] is False and got["public_key"] is None
    assert not (tmp_path / "ssh").exists()


def test_creating_is_idempotent(sshkey):
    """⚠ 이미 있는 키를 갈아엎으면 Vast 에 등록해 둔 것이 조용히 무효가 되고,
    그 사실은 다음에 빌릴 때야 드러난다."""
    first = sshkey.ensure()
    assert first["created"] is True
    again = sshkey.ensure()
    assert again["created"] is False
    assert again["public_key"] == first["public_key"]


def test_the_key_files_are_not_readable_by_others(sshkey):
    sshkey.ensure()
    assert stat.S_IMODE(sshkey.private_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(sshkey.key_dir().stat().st_mode) == 0o700


def test_the_private_key_never_leaves(sshkey):
    """⚠ 밖으로 나가는 것은 공개키와 지문뿐이다."""
    body = json.dumps([sshkey.ensure(), sshkey.info()], ensure_ascii=False)
    assert "PRIVATE KEY" not in body
    assert sshkey.private_path().read_text() not in body


def test_the_fingerprint_matches_openssh(sshkey):
    """지문이 OpenSSH 와 같아야 Vast 목록과 대조가 된다 — 등록 확인의 근거다."""
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen 이 없는 기계")
    ours = sshkey.ensure()["fingerprint"]
    out = subprocess.run(["ssh-keygen", "-lf", str(sshkey.public_path())],
                         capture_output=True, text=True, timeout=30)
    assert ours in out.stdout, f"{ours} ∉ {out.stdout.strip()}"


def test_it_does_not_shell_out_to_ssh_keygen():
    """⚠ 이미지에 `openssh-client` 가 없다 — 서브프로세스로 만들면 배포판에서 깨진다."""
    src = (Path(__file__).resolve().parents[1] / "app/services/cloud/sshkey.py").read_text()
    from conftest import python_code_only

    body = python_code_only(src)
    assert "ssh-keygen" not in body and "subprocess" not in body


def test_a_missing_cli_is_not_the_users_fault():
    """⚠ 컨테이너 배포판은 사람이 CLI 를 깔 방법이 없다. 고칠 것은 이미지이지
    사용자가 아니라고 말해야 한다."""
    from app.routers.cloud import NO_CLI

    assert "이미지" in NO_CLI


# ── 설정 → 클라우드 탭 ───────────────────────────────────────────────────────

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_the_cloud_tab_is_reachable():
    """⚠ 탭 목록에 넣고 렌더를 빠뜨리면 빈 화면이 된다 — 둘 다 본다."""
    page = (_SRC / "pages" / "SettingsPage.tsx").read_text()
    assert "{ id: 'cloud', label: '클라우드' }" in page, "탭 목록에 없다"
    assert "{tab === 'cloud' && <CloudPanel />}" in page, "탭을 골라도 아무것도 안 그린다"


def test_the_panel_never_shows_private_key_material():
    """⚠ 화면이 만질 수 있는 것은 공개키와 지문뿐이다."""
    src = (_SRC / "components" / "CloudPanel.tsx").read_text()
    assert "private_key" not in src and "PRIVATE" not in src
    assert "public_key" in src and "fingerprint" in src


def test_the_panel_reports_verification_not_the_attempt():
    """⚠ "등록했다" 로 말하면 계정에 안 들어갔을 때를 못 잡는다 — 지문 확인 결과로 말한다."""
    src = (_SRC / "components" / "CloudPanel.tsx").read_text()
    assert "r.registered" in src, "등록 성공을 확인 결과로 판단하지 않는다"
