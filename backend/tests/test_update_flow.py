"""업데이트 — 확인 · 받기 · 적용 · 되돌리기 (feature/version-update.md §3–4).

게이트웨이는 절차의 마지막에 자기 자신이 갈아치워지므로 직접 못 돌린다 — 호스트의
unitd 가 일시 유닛으로. 받기는 실행하지 않는다. sudo 는 자동화하지 않는다.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _unitd():
    spec = importlib.util.spec_from_file_location("unitd", REPO / "daemons" / "unitd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_only_version_tags_count_and_a_dirty_source_build_compares_by_its_tag():
    """`latest` 나 손으로 민 이름은 버전이 아니다. 소스 기계의 `v0.4.5-1-gabc-dirty` 는
    v0.4.5 로 견준다 — 자기보다 새 태그가 있을 때만 "새 버전"."""
    from app.services import version as V
    assert V.newest_tag(["latest", "v0.4.5", "v0.4.10", "v0.3.9", "hosttest"]) == "v0.4.10"
    assert V.newest_tag(["latest"]) is None
    assert V.version_key("v0.4.10") > V.version_key("v0.4.9")
    cur = V.version_key("v0.4.5-1-g7a6c7c0-dirty".split("-", 1)[0])
    assert V.version_key("v0.4.6") > cur and not V.version_key("v0.4.5") > cur


def test_the_check_uses_the_registry_on_deploy_hosts_and_git_tags_on_source_machines(monkeypatch):
    """정본의 출처가 길을 정한다: 매니페스트(배포) → 레지스트리 tags, git → 원격 태그.
    실패는 error 로 말하고 available 은 False — 지어내지 않는다."""
    from app.services import version as V
    V._check_cache.update(at=0.0, result=None)
    monkeypatch.setattr(V, "running_version", lambda: {"version": "v0.4.5", "source": "manifest", "registry": "piper-build:5000"})
    monkeypatch.setattr(V, "_registry_tags", lambda reg, **k: ["latest", "v0.4.5", "v0.4.6"])
    r = V.check_update(force=True)
    assert r["mode"] == "image" and r["latest"] == "v0.4.6" and r["available"]
    monkeypatch.setattr(V, "running_version", lambda: {"version": "v0.4.6-2-gabc", "source": "git"})
    monkeypatch.setattr(V, "_remote_git_tags", lambda **k: ["v0.4.5", "v0.4.6"])
    r = V.check_update(force=True)
    assert r["mode"] == "source" and not r["available"]
    monkeypatch.setattr(V, "_remote_git_tags", lambda **k: (_ for _ in ()).throw(RuntimeError("no network")))
    r = V.check_update(force=True)
    assert r["error"] == "no network" and not r["available"]


def test_unitd_refuses_odd_versions_and_stages_and_wants_a_pulled_bundle_before_apply(monkeypatch, tmp_path):
    """받기 없이 적용은 없다. 버전은 vX.Y.Z 만 — 남의 이름으로 스크립트를 부르지 않는다."""
    u = _unitd()
    monkeypatch.setenv("PIPER_WORK", str(tmp_path))
    hub = u.UnitHub()
    monkeypatch.setattr(hub, "update_status", lambda: {"active": False})
    for bad in ("0.4.6", "v0.4", "latest", "../x"):
        with pytest.raises(ValueError, match="버전 모양"):
            hub.update(bad, "pull")
    with pytest.raises(ValueError, match="모르는 단계"):
        hub.update("v0.4.6", "install")
    with pytest.raises(RuntimeError, match="아직 받지 않았습니다"):
        hub.update("v0.4.6", "apply", "image")
    assert hub.update("v0.4.6", "pull", "source")["skipped"]
    monkeypatch.setattr(hub, "update_status", lambda: {"active": True})
    with pytest.raises(RuntimeError, match="이미 돌고"):
        hub.update("v0.4.6", "pull", "image")


def test_the_transient_unit_runs_the_bundled_scripts(monkeypatch, tmp_path):
    """받기 = `piper-install.sh <ver> --pull-only`, 적용 = 받아 둔 `<WORK>/<ver>/apply.sh`,
    소스 기계 = `update-source.sh <ver>`. systemd-run 이 소유자라 게이트웨이가 죽어도 산다."""
    u = _unitd()
    monkeypatch.setenv("PIPER_WORK", str(tmp_path))
    (tmp_path / "v0.4.6").mkdir(); (tmp_path / "v0.4.6" / "apply.sh").write_text("#!/bin/sh\n")
    (tmp_path / "v0.4.5").mkdir(); (tmp_path / "v0.4.5" / "manifest.txt").write_text('registry="piper-build:5000"\n')
    hub = u.UnitHub()
    monkeypatch.setattr(hub, "update_status", lambda: {"active": False})
    calls: list[list[str]] = []

    class R:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(u.subprocess, "run", lambda cmd, **k: (calls.append(list(cmd)), R())[1])
    monkeypatch.setattr(u, "_systemctl", lambda *a, **k: R())
    hub.update("v0.4.6", "pull", "image")
    run = calls[-1]
    assert run[:4] == ["systemd-run", "--user", "--unit", "piper-update"]
    assert "--remain-after-exit" in run and "--collect" not in run, "끝난 유닛이 사라지면 끝났는지 알 길이 없다"
    assert run[-3:] == [str(u.REPO / "deploy" / "piper-install.sh"), "v0.4.6", "--pull-only"]
    assert "PIPER_IMAGE=piper-build:5000/piper-web-backend" in run
    hub.update("v0.4.6", "apply", "image")
    assert calls[-1][-1] == str(tmp_path / "v0.4.6" / "apply.sh")
    hub.update("v0.4.6", "apply", "source")
    assert calls[-1][-2:] == [str(u.REPO / "deploy" / "update-source.sh"), "v0.4.6"]
    import json
    assert json.loads((tmp_path / ".update.json").read_text())["stage"] == "apply"


def test_sudo_lines_are_lifted_out_of_the_log_and_the_changelog_section_is_cut():
    u = _unitd()
    log = "1. 전제\n  ✗ 그룹 video 없음\n  아래를 먼저 실행하세요:\n    sudo usermod -aG video sw\n    sudo apt install -y redis-server\n"
    assert u.parse_need_sudo(log) == ["sudo usermod -aG video sw", "sudo apt install -y redis-server"]
    text = "# 변경 이력\n\n## v0.4.6 — x\n\n- 하나\n\n## v0.4.5 — y\n\n- 둘\n"
    assert u.changelog_section(text, "v0.4.6") == "## v0.4.6 — x\n\n- 하나"
    assert u.changelog_section(text, "v0.4.7") == ""


def test_apply_is_refused_while_anything_runs_and_pull_never_executes_anything():
    """마지막 단계가 게이트웨이와 데몬을 갈아치운다 — 녹화·추론·학습·수동 조작 중이면
    막는다. 받기는 `--pull-only`: 꺼내기까지만, apply.sh 를 부르지 않는다."""
    router = (REPO / "backend" / "app" / "routers" / "system.py").read_text()
    apply = router.split("async def update_apply", 1)[1].split("\n@router", 1)[0]
    assert "ex.running()" in apply and "409" in apply
    pull = router.split("async def update_pull", 1)[1].split("\n@router", 1)[0]
    assert "ex.running()" not in pull
    inst = (REPO / "deploy" / "piper-install.sh").read_text()
    assert "--pull-only" in inst and 'if [ $PULL_ONLY = 1 ]; then' in inst
    body = inst.split("if [ $PULL_ONLY = 1 ]; then", 1)[1].split("fi", 1)[0]
    assert "exit 0" in body and "apply.sh" not in body.replace("# 적용하려면", "").split("echo", 1)[0]


def test_the_bundle_carries_what_the_web_update_needs():
    """호스트의 unitd 가 쓰는 받기 스크립트와, 받은 뒤 보여 줄 변경 이력."""
    stage = (REPO / "deploy" / "stage-hostside.sh").read_text()
    assert 'cp deploy/piper-install.sh "$OUT/"' in stage and 'cp CHANGELOG.md "$OUT/"' in stage
    rel = (REPO / "deploy" / "release.sh").read_text()
    assert "deploy/piper-install.sh deploy/update-source.sh" in rel, "daemons.tar.gz 에 받기 스크립트가 없다"
    src = (REPO / "deploy" / "update-source.sh").read_text()
    assert "git fetch" in src and "deploy/install.sh" in src and "systemctl --user restart" in src
    assert "piper-estopd.service) continue" in src, "estopd 를 마지막에 재시작해야 한다"


def test_the_card_expects_to_lose_the_gateway_and_never_automates_sudo():
    src = (REPO / "frontend" / "src" / "components" / "VersionCard.tsx").read_text()
    for needle in ("/system/update/check", "/system/update/pull", "/system/update/apply",
                   "/system/update/status", "/system/update/notes", "waitForGateway",
                   "need_sudo", "window.location.reload()", "이 버전으로"):
        assert needle in src, needle
    assert "window.confirm(" not in src
