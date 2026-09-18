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


def test_registry_tags_builds_the_right_url_for_private_and_public_registries(monkeypatch):
    """사설(`host:port`)은 http·인증 없이 그대로. 공개(`ghcr.io/<조직>`)는 https 에
    `/v2/<조직>/<이미지>/...` 순서로, 401 을 만나면 WWW-Authenticate 챌린지를 따라
    토큰을 받아 재시도한다. 예전 코드는 `http://ghcr.io/wego-robotics/v2/.../tags/list`
    처럼 `/v2/` 를 조직 이름 뒤에 붙여 늘 404 였다."""
    import json
    import urllib.error
    from contextlib import contextmanager

    from app.services import version as V

    @contextmanager
    def _resp(payload):
        class R:
            def read(self): return json.dumps(payload).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        yield R()

    calls = []

    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        if url == "http://piper-build:5000/v2/piper-web-backend/tags/list":
            return _resp({"tags": ["v0.4.5"]}).__enter__()
        if url == "https://ghcr.io/v2/wego-robotics/piper-web-backend/tags/list":
            if not req.get_header("Authorization"):
                raise urllib.error.HTTPError(
                    url, 401, "unauthorized",
                    {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                                         'scope="repository:wego-robotics/piper-web-backend:pull"'},
                    None)
            assert req.get_header("Authorization") == "Bearer tok123"
            return _resp({"tags": ["v0.4.6"]}).__enter__()
        if url.startswith("https://ghcr.io/token?"):
            return _resp({"token": "tok123"}).__enter__()
        raise AssertionError(f"예상 못 한 URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert V._registry_tags("piper-build:5000") == ["v0.4.5"]
    assert V._registry_tags("ghcr.io/wego-robotics") == ["v0.4.6"]


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
    """받기 = 가장 최근에 받아 둔 버전 디렉토리의 `piper-install.sh <ver> --pull-only`,
    적용 = 받아 둔 `<WORK>/<ver>/apply.sh`, 소스 기계 = `update-source.sh <ver>`.
    systemd-run 이 소유자라 게이트웨이가 죽어도 산다.

    ⚠ **`current/` 로는 못 찾는다.** `apply.sh` 는 `daemons.tar.gz` 만 그 안에
    풀어 두고 `piper-install.sh` 자체는 안 온다 — 버전 디렉토리에만 있다.
    실기: .120 에서 웹 [받기] 를 처음 눌러 봤을 때, `unitd.py` 가 (자기 `__file__`
    로 잡는) `current/` 밑을 찾다가 늘 "이 번들이 낡았다" 로 잘못 보고했다."""
    u = _unitd()
    monkeypatch.setenv("PIPER_WORK", str(tmp_path))
    (tmp_path / "v0.4.6").mkdir()
    (tmp_path / "v0.4.6" / "apply.sh").write_text("#!/bin/sh\n")
    (tmp_path / "v0.4.6" / "piper-install.sh").write_text("#!/bin/sh\n")
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
    # 최근 받아 둔 v0.4.6 자기 자신의 piper-install.sh 를 쓴다 — v0.4.5 도 있지만
    # 더 낡았고, current/ 는 후보에도 없다.
    assert run[-3:] == [str(tmp_path / "v0.4.6" / "piper-install.sh"), "v0.4.6", "--pull-only"]
    assert "PIPER_IMAGE=piper-build:5000/piper-web-backend" in run
    hub.update("v0.4.6", "apply", "image")
    assert calls[-1][-1] == str(tmp_path / "v0.4.6" / "apply.sh")
    hub.update("v0.4.6", "apply", "source")
    assert calls[-1][-2:] == [str(u.REPO / "deploy" / "update-source.sh"), "v0.4.6"]
    import json
    assert json.loads((tmp_path / ".update.json").read_text())["stage"] == "apply"


def test_pull_falls_back_to_the_checkout_script_with_nothing_pulled_yet(monkeypatch, tmp_path):
    """받아 둔 버전이 하나도 없으면(신규 호스트가 아니라, 저장소 체크아웃에서
    unitd 를 직접 돌려 보는 개발 상황) `REPO/deploy/piper-install.sh` 로 되돌아간다."""
    u = _unitd()
    monkeypatch.setenv("PIPER_WORK", str(tmp_path))
    hub = u.UnitHub()
    monkeypatch.setattr(hub, "update_status", lambda: {"active": False})
    calls: list[list[str]] = []

    class R:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(u.subprocess, "run", lambda cmd, **k: (calls.append(list(cmd)), R())[1])
    monkeypatch.setattr(u, "_systemctl", lambda *a, **k: R())
    hub.update("v0.4.6", "pull", "image")
    assert calls[-1][-3:] == [str(u.REPO / "deploy" / "piper-install.sh"), "v0.4.6", "--pull-only"]


def test_pull_picks_the_numerically_latest_bundle_not_the_last_glob_hit(monkeypatch, tmp_path):
    """v0.4.9 다음은 v0.4.10 — 문자열로 정렬하면 v0.4.9 가 더 커 보인다."""
    u = _unitd()
    monkeypatch.setenv("PIPER_WORK", str(tmp_path))
    for v in ("v0.4.2", "v0.4.10", "v0.4.9"):
        (tmp_path / v).mkdir()
        (tmp_path / v / "piper-install.sh").write_text("#!/bin/sh\n")
    assert u.UnitHub()._latest_bundle() == tmp_path / "v0.4.10"


def test_a_legacy_latest_dir_counts_as_a_bundle_by_its_manifest_version(monkeypatch, tmp_path):
    """⚠ 실기(NUC, 2026-09-11): README 대로 버전 없이 깔면 v0.4.17 까지의 piper-install.sh 가
    번들을 `latest/` 실제 디렉토리에 풀었는데 unitd 는 `v*/` 만 봐서 웹 [업데이트]가 "받기
    스크립트가 없습니다"로 거절했다. `latest/` 는 매니페스트의 version= 으로 읽고, 링크
    `latest`(새 설치)는 가리키는 디렉토리가 이미 목록에 있으니 두 번 세지 않는다."""
    import shutil

    u = _unitd()
    monkeypatch.setenv("PIPER_WORK", str(tmp_path))
    (tmp_path / "v0.4.16").mkdir()
    (tmp_path / "v0.4.16" / "manifest.txt").write_text('version="v0.4.16"\nregistry="piper-build:5000"\n')
    (tmp_path / "latest").mkdir()
    (tmp_path / "latest" / "manifest.txt").write_text('version="v0.4.17"\nregistry="ghcr.io/wego-robotics"\n')
    (tmp_path / "latest" / "piper-install.sh").write_text("#!/bin/sh\n")
    hub = u.UnitHub()
    assert [v for v, _ in hub._bundles()] == ["v0.4.16", "v0.4.17"]
    assert hub._latest_bundle() == tmp_path / "latest"
    assert hub._registry() == "ghcr.io/wego-robotics"
    assert hub._bundle_dir("v0.4.17") == tmp_path / "latest"
    assert [e["version"] for e in hub.host_info()["deploy"]["versions"]] == ["v0.4.16", "v0.4.17"]
    # 새 설치: latest 는 링크 — 두 번 세지 않는다
    shutil.rmtree(tmp_path / "latest")
    (tmp_path / "v0.4.18").mkdir()
    (tmp_path / "v0.4.18" / "manifest.txt").write_text('version="v0.4.18"\n')
    (tmp_path / "latest").symlink_to("v0.4.18")
    assert [v for v, _ in hub._bundles()] == ["v0.4.16", "v0.4.18"]
    src = (REPO / "daemons" / "unitd.py").read_text()
    assert 'raise RuntimeError(f"받아 둔 번들이 없습니다' in src, "문구가 아직 엉뚱하다"
    assert 'raise RuntimeError(f"받기 스크립트가 없습니다' not in src, "옛 문구(이 번들이 낡았다)가 남았다"


def test_the_installer_lands_the_bundle_in_a_version_dir_and_links_latest():
    """`latest` 로 받아도 매니페스트의 version= 으로 버전 이름 디렉토리에 두고 `latest` 는
    링크로 — unitd 의 `v*` 탐색과 웹 [업데이트]가 그 이름을 전제한다."""
    from conftest import code_only

    inst = code_only((REPO / "deploy" / "piper-install.sh").read_text())
    assert r'''sed -n 's/^version="\(v[0-9][^"]*\)"$/\1/p' "$TMPD/manifest.txt"''' in inst, "매니페스트 버전을 안 읽는다"
    assert 'DEST="$WORK/$REAL"' in inst and 'ln -sfn "$REAL" "$WORK/$VERSION"' in inst
    assert 'if [ -d "$WORK/$VERSION" ] && [ ! -L "$WORK/$VERSION" ]; then rm -rf "$WORK/$VERSION"; fi' in inst, \
        "옛 latest 실제 디렉토리를 링크로 못 바꾼다"


def test_sudo_lines_are_lifted_out_of_the_log_and_the_changelog_section_is_cut():
    u = _unitd()
    log = "1. 전제\n  ✗ 그룹 video 없음\n  아래를 먼저 실행하세요:\n    sudo usermod -aG video sw\n    sudo apt install -y redis-server\n"
    assert u.parse_need_sudo(log) == ["sudo usermod -aG video sw", "sudo apt install -y redis-server"]
    # ⚠ **`&&` 로 이어진 처방도 통째로 올라와야 한다.** CAN sudoers 줄이 그 모양이다(v0.5.1) —
    #   앞부분만 집어 주면 사람이 반쪽짜리 명령을 복사해 실행하고, 파일은 안 놓인 채
    #   [UP] 에서만 "a password is required" 로 막힌다. 화면은 이 문자열을 그대로 그린다.
    chained = ("  아래를 먼저 실행하세요 (이 스크립트는 sudo 를 직접 쓰지 않습니다):\n"
               "    sudo visudo -cf /b/sudoers/piper-can && sudo install -m 0440 -o root -g root"
               " /b/sudoers/piper-can /etc/sudoers.d/piper-can\n")
    assert u.parse_need_sudo(chained) == [
        "sudo visudo -cf /b/sudoers/piper-can && sudo install -m 0440 -o root -g root"
        " /b/sudoers/piper-can /etc/sudoers.d/piper-can"], "이어진 명령이 잘린다"
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


def test_a_restart_checks_that_the_daemons_match_the_release_and_says_so():
    """⚠ 실기(NUC, 2026-09-15): wheel 은 0.5.1 로 최신인데 `daemons/camerad.py` 가 9월 1일자였다.
    회색 카드 보정이 "camerad 가 응답하지 않습니다" 로 죽었고 데몬을 재시작해도 안 변했다.
    그 불일치를 **아무도 말하지 않았다** — 판정이 버전 카드(브라우저)에만 있었고, 그것도
    wheel 만 봤다. 화면을 안 연 사람은 알 길이 없었다.

    넷을 잠근다: ① unitd 가 데몬 **소스**의 스탬프를 보고하고 ② 백엔드가 한 곳에서 판정하고
    ③ 게이트웨이가 **기동할 때** 그걸 보고 무엇을 해야 하는지까지 로그로 남기고 ④ 카드는
    자기 규칙 대신 그 결과를 읽는다. 표시가 없는 옛 호스트는 낡은 것으로 본다 — 이 사고의
    그 기계가 정확히 그 경우다.

    ⚠ 실기(.120, 2026-09-16): wheel 판정은 처음엔 **이번 릴리스가 다시 구운 것**(매니페스트
    `wheels=`)만 견줬다. 그 호스트는 게이트웨이가 v0.5.4 인데 wheel 이 전부 0.4.7 이었고
    (시뮬 장면이 옛 바닥·옛 탑뷰였다) `wheels=` 가 비어 있어 아무 말도 안 나왔다. 번들은
    릴리스마다 일곱 wheel 전부에 그 버전을 박아 실으니(stage-hostside.sh) 제대로 적용된
    호스트는 전부 같다 — **적용 릴리스와** 견줘야 건너뛴 릴리스가 잡힌다."""
    unitd_src = (REPO / "daemons" / "unitd.py").read_text()
    assert '"current" / "daemons" / ".version"' in unitd_src, "apply.sh 가 적는 자리에서 스탬프를 안 읽는다"
    assert '"daemons_version"' in unitd_src, "데몬 소스의 출처를 보고하지 않는다"

    ver = (REPO / "backend" / "app" / "services" / "version.py").read_text()
    assert "def staleness(" in ver and 'info["staleness"] = staleness(info)' in ver, \
        "판정이 collect() 로 안 나온다 — API 도 기동 검사도 같은 사실을 못 본다"
    assert "stamp != applied" in ver, "데몬 소스 스탬프를 적용 릴리스와 대조하지 않는다"
    assert "표시 없음" in ver, "스탬프가 없는 옛 호스트를 최신으로 본다"
    from conftest import code_only
    assert 'ver not in ("0.1.0", want)' in code_only(ver), \
        "wheel 을 적용 릴리스와 안 견준다 — 건너뛴 릴리스를 영영 못 잡는다"
    assert "touched" not in code_only(ver), \
        "매니페스트가 이번에 다시 구운 것만 견주는 옛 규칙이 남아 있다"

    main = (REPO / "backend" / "app" / "main.py").read_text()
    assert "_version.staleness()" in main, "기동할 때 확인하지 않는다"
    assert "piper-install.sh" in main, "소스가 낡았을 때 무엇을 해야 하는지 말하지 않는다"

    card = (REPO / "frontend" / "src" / "components" / "VersionCard.tsx").read_text()
    assert "info.staleness?.wheels ??" in card, "카드가 여전히 자기 규칙으로만 판정한다"
    assert "sourceStale" in card, "데몬 소스 불일치를 화면에 안 그린다"


def test_the_card_expects_to_lose_the_gateway_and_never_automates_sudo():
    src = (REPO / "frontend" / "src" / "components" / "VersionCard.tsx").read_text()
    for needle in ("/system/update/check", "/system/update/pull", "/system/update/apply",
                   "/system/update/status", "/system/update/notes", "waitForGateway",
                   "need_sudo", "window.location.reload()", "이 버전으로"):
        assert needle in src, needle
    assert "window.confirm(" not in src


def test_a_third_party_package_is_not_mistaken_for_one_of_our_wheels():
    """⚠ 실기(2026-09-17): 멀쩡한 호스트가 기동할 때마다 "데몬 wheel 이 이번 릴리스와
    다릅니다: robotd piper-sdk 0.6.1" 이라고 경고했다. `piper-` 로 시작하는 것을 전부
    우리 wheel 로 셌기 때문인데, `piper_sdk` 는 AgileX 의 CAN SDK 다 — apply.sh 가 PyPI 에서
    0.6.1 로 깔고 **릴리스 버전을 따라갈 이유가 없다.**

    거짓 경고는 진짜 경고를 죽인다. 이름이 아니라 **우리가 굽는 일곱 개 목록**으로 센다."""
    from app.services import version as V

    assert V.OUR_WHEELS == {"piper-bus", "piper-shm", "piper-robot", "piper-cam",
                            "piper-rs", "piper-so101", "piper-sim"}, \
        "stage-hostside.sh 가 굽는 목록과 다르다"
    info = {
        "gateway": {"version": "v0.5.4"},
        "deploy": {"current": "v0.5.4", "daemons_version": "v0.5.4"},
        "daemons": {
            "robotd": {"piper-robot": "0.5.4", "piper-shm": "0.5.4", "piper-sdk": "0.6.1",
                       "python-can": "4.6.1"},
            "simd": {"piper-sim": "0.5.4", "mujoco": "3.12.0"},
        },
    }
    assert V.staleness(info)["ok"], "서드파티 패키지를 우리 wheel 로 세어 거짓 경고한다"
    info["daemons"]["simd"]["piper-sim"] = "0.4.7"          # 진짜 낡은 것은 여전히 잡는다
    assert V.staleness(info)["wheels"] == ["simd piper-sim 0.4.7"]


def test_advice_is_not_mistaken_for_a_prescription():
    """⚠ **실측(2026-09-18)**: .44 가 업데이트할 때마다 "전제가 빠져 있어 멈췄습니다" 를
    띄웠다. 그런데 `apply.sh` 는 멀쩡히 끝나고 있었다(종료 코드 0).

    원인은 파서였다. 로그 전체에서 `sudo ` 로 시작하는 줄을 전부 긁었는데, `apply.sh`
    에는 **멈추는 것과 상관없는 조언**도 `sudo` 로 적혀 있다 — CAN 이름 규칙이 없을 때의
    안내가 그렇다. 그 줄은 `NEED_SUDO` 에 안 들어가고 스크립트를 멈추지도 않는다.

    ⚠ .120 에서는 안 났다. 거기엔 그 규칙 파일이 있어서 안내 자체가 안 찍혔다 — 같은
    버전인데 한 대만 그런 이유가 그것이다. "한 대에서만 난다" 가 곧 "그 머신의 상태를
    보는 줄이 어딘가 있다" 는 뜻이었다.
    """
    u = _unitd()
    advice_only = (
        "1. 전제\n"
        "  ✗ CAN 이름 규칙이 없다 — 어댑터는 꽂혀 있다\n"
        "       이 머신의 규칙을 만드세요:\n"
        "         python3 /b/udev/list-can-adapters.py --write-rule \\\n"
        "           | sudo tee /etc/udev/rules.d/99-piper-can.rules\n"
        "         sudo udevadm control --reload-rules   # 그 뒤 어댑터를 다시 꽂는다\n"
        "4. 끝났습니다\n")
    assert u.parse_need_sudo(advice_only) == [], "조언을 처방으로 읽는다"

    # 진짜 처방이 있으면 그건 잡는다 — 마커 뒤의 것만
    with_block = advice_only + (
        "  아래를 먼저 실행하세요 (이 스크립트는 sudo 를 직접 쓰지 않습니다):\n"
        "    sudo cp /b/udev/99-realsense-libusb.rules /etc/udev/rules.d/\n")
    assert u.parse_need_sudo(with_block) == [
        "sudo cp /b/udev/99-realsense-libusb.rules /etc/udev/rules.d/"]


def test_the_can_advice_does_not_stop_the_apply():
    """⚠ 조언이 쓸모없다는 뜻은 아니다 — 규칙이 없으면 포트를 바꿔 꽂는 순간 두 팔의
    이름이 뒤바뀐다. 다만 그건 **업데이트를 막는 전제가 아니다.** `apply.sh` 의 그 갈래가
    `NEED_SUDO` 를 건드리지 않는다는 것이 그 선언이고, 파서가 그 선언을 존중해야 한다."""
    # ⚠ 여기서는 `code_only` 를 **안 쓴다.** 그 헬퍼는 `/* */` 를 주석으로 지우는데,
    #   셸의 glob(`udev/*.rules` … `devices/*/idVendor`)이 그 모양이라 그 사이가 통째로
    #   사라진다 — 검사하려던 줄이 같이 지워졌다.
    src = (REPO / "deploy" / "apply.sh").read_text()
    can = src[src.index('CAN_RULE="'):src.index("if [ ${#NEED_APT[@]}")]
    assert "NEED_SUDO" not in can, "CAN 갈래가 업데이트를 막고 있다"
    assert "udevadm control --reload-rules" in can, "안내 자체는 있어야 한다"
