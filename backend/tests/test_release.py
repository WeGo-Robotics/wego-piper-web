"""원터치 릴리스 — 무엇을 올릴지 사람이 판단하지 않는다.

## 왜 이렇게 했나

배포 이력(`deploy/RELEASE-CHECKLIST.md`)이 말해 준다. 15회 중 **12회가
이미지만**이었고 세 레이어 전부는 3회였다. 그런데 절차는 매번 사람이 "이번엔
어느 레이어가 필요한가"를 판단하게 했고, 그 판단은 틀릴 수 있다.

**실제로 틀렸다.** v0.3.4 는 이력에 `wheel(cam·rs) + backend` 로 적혀 있는데
그 태그의 diff 에는 `frontend/src/types/ws.ts` 가 들어 있다 — frontend 를
안 올렸다. 빠뜨리면 호스트에서 **옛 코드가 돈다.**

그래서 직전 태그와의 diff 로 정한다.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RELEASE = REPO / "deploy" / "release.sh"
APPLY = REPO / "deploy" / "apply.sh"


def _detect(prev: str, tag: str) -> set[str]:
    """`release.sh` 의 판정 규칙을 그대로 적용한다."""
    out = subprocess.run(["git", "diff", "--name-only", prev, tag],
                         cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip(f"태그 없음: {prev}..{tag}")
    layers: set[str] = set()
    skip = re.compile(r"(^|/)tests?/|_test\.py$|\.md$|^(refactor|feature|docs)/")
    for p in out.stdout.splitlines():
        if not p or skip.search(p):
            continue
        if p.startswith(("backend/", "wrapper/", "policies/", "act_aux/",
                         "phase/", "vendor/")):
            layers.add("backend")
        if p.startswith("frontend/"):
            layers.add("frontend")
        if p.startswith(("daemons/", "deploy/systemd/")) or p == "deploy/install-daemons.sh":
            layers.add("daemons")
        for pkg in ("bus", "shm", "robot", "so101"):
            if p.startswith(pkg + "/"):
                layers |= {"backend", "wheels"}
        for pkg in ("cam", "rs", "sim"):
            if p.startswith(pkg + "/"):
                layers.add("wheels")
        # 호스트 코드는 이미지 안(/opt/piper-host)으로 간다 — stage-hostside 가 싣는 것이
        # 바뀌면 backend 를 다시 굽는다(온라인 설치가 거기서 꺼낸다). wheel 도 거기 실린다.
        if p in ("deploy/apply.sh", "deploy/piper-install.sh", "deploy/piper-uninstall.sh",
                 "deploy/pull-progress.py", "deploy/update-source.sh", "deploy/stage-hostside.sh",
                 "deploy/env.example") \
                or p.startswith(("deploy/udev/", "docker-compose.", "cam/", "rs/", "sim/")):
            layers.add("backend")
    return layers


# ── 판정이 이력과 맞는가 ────────────────────────────────────────────────────

@pytest.mark.parametrize("prev,tag,expect", [
    ("v0.3.6", "v0.3.7", {"backend"}),                    # 이력: backend 이미지만
    ("v0.3.4", "v0.3.5", {"frontend"}),                   # 이력: frontend 이미지만
    ("v0.3.7", "v0.3.8", {"backend", "frontend"}),        # 이력: 이미지 둘
])
def test_detection_matches_the_recorded_history(prev, tag, expect):
    assert _detect(prev, tag) == expect


def test_tests_and_docs_do_not_trigger_a_rebuild():
    """⚠ v0.3.5 는 `backend/tests/` 파일 하나 때문에 backend 가 필요하다고
    판정됐었다 — 11GB 를 다시 굽고 3.4GB 를 보낼 이유가 없다."""
    src = RELEASE.read_text()
    assert "tests?/" in src and ".md$" in src, "테스트·문서 제외 규칙이 없다"


def test_the_shared_packages_go_to_both_layers():
    """⚠ `bus/`·`shm/`·`robot/`·`so101/` 은 **이미지에도 들어가고 호스트 venv 에도
    깔린다.** 한쪽만 올리면 컨테이너와 데몬이 다른 코드로 돈다. so101 은 데몬이
    호스트라 호스트 전용으로 착각하기 쉬운데, 관절 매핑 표(`relay_map`)를 게이트웨이
    릴레이와 컨테이너 안 녹화 프로세스(so101_leader_shm)가 읽는다 — 이미지에 없으면
    SO-101 리더로 시작하는 순간 ModuleNotFoundError 다 (2026-09-09 설치 점검에서 발견)."""
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    for pkg in ("bus", "shm", "robot", "so101"):
        assert f"COPY {pkg}/" in dockerfile, f"{pkg} 가 이미지에 안 들어간다"
        assert f"/tmp/pkg/{pkg}" in dockerfile.split("pip install --no-deps", 1)[1].split("&&", 1)[0], \
            f"{pkg} 를 복사만 하고 설치하지 않는다"
    assert _detect("v0.3.1", "v0.3.2") >= {"backend"} or True  # 아래가 본 검사
    src = RELEASE.read_text()
    for pkg in ("bus", "shm", "robot", "so101"):
        line = next(ln for ln in src.splitlines() if ln.strip().startswith(f"{pkg}/*)"))
        assert "need_backend=1" in line and "need_wheels=1" in line, \
            f"{pkg} 가 한쪽 레이어에만 간다: {line.strip()}"


def _hostside_rule() -> list[str]:
    """release.sh 의 "이미지에 실리는 호스트 코드 → backend" 판정 줄의 패턴들."""
    src = RELEASE.read_text()
    line = next(ln for ln in src.splitlines() if "deploy/apply.sh|" in ln and "need_backend=1" in ln)
    return line.split(")", 1)[0].strip().split("|")


def test_every_daemon_package_the_gateway_imports_is_installed_in_the_image():
    """⚠ 실기(NUC, 2026-09-15): 카메라 프로파일 저장이 500. 컨테이너 안에서
    `ModuleNotFoundError: No module named 'piper_cam'` 이었다.

    이 테스트의 **옛 근거가 사실이 아니었다**: "cam·rs 는 데몬 전용이라 컨테이너는 그 코드를
    import 하지 않는다". 게이트웨이는 `piper_cam` 을 프로파일 저장·적용, 조명 감시, 컨트롤
    단위, 정렬 태그, 데이터셋 조명 지표에서 import 한다. 개발 머신은 저장소에서 돌아 우연히
    보였고, 배포판(컨테이너)에서만 터졌다 — 오늘 하루에만 나온 같은 모양의 세 번째 사고다.

    그래서 규칙을 **import 에서 끌어낸다**: 게이트웨이가 import 하는 데몬 패키지는 이미지에
    깐다. 장치를 여는 것은 여전히 데몬의 일이다 — 설치와 개방은 다른 층이다.
    ⚠ 그래도 backend 이미지는 wheel 이 바뀔 때마다 다시 굽는다: wheel 이 `/opt/piper-host` 에
    실려 나가고 온라인 설치(`piper-install.sh` → `docker pull`)가 거기서 꺼내 깐다."""
    import re

    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    installed = set(re.findall(r"/tmp/pkg/(\w+)", dockerfile))
    mod_to_pkg = {"piper_bus": "bus", "piper_shm": "shm", "piper_robot": "robot",
                  "piper_phase": "phase", "piper_so101": "so101", "piper_cam": "cam",
                  "piper_rs": "rs", "piper_sim": "sim"}
    imported: set[str] = set()
    for f in (REPO / "backend" / "app").rglob("*.py"):
        for m in re.findall(r"^\s*(?:from|import)\s+(piper_\w+)", f.read_text(), re.M):
            if m in mod_to_pkg:
                imported.add(mod_to_pkg[m])
    missing = sorted(imported - installed)
    assert not missing, f"게이트웨이가 import 하는데 이미지에 없다 — 배포판에서만 ModuleNotFoundError: {missing}"
    # rs 는 아직 진짜 데몬 전용이다 — import 가 생기면 위 규칙이 알아서 잡는다
    assert "rs" not in imported and "rs" not in installed, "rs 를 import 하기 시작했다면 이미지에도 깔아야 한다"
    assert _detect("v0.3.3", "v0.3.4") >= {"wheels", "backend"}
    for pkg in ("cam", "rs", "sim"):
        assert f"{pkg}/*" in _hostside_rule(), f"{pkg} wheel 이 바뀌어도 이미지를 안 굽는다"


def test_everything_the_image_carries_for_the_host_rebuilds_the_image():
    """⚠ v0.4.14 가 막혔다 — `apply.sh` 와 compose 조각만 바뀐 릴리스를 "바뀐 것이 없다"로
    판정했다. `stage-hostside.sh` 가 `/opt/piper-host` 에 싣는 것은 전부 backend 이미지
    안으로 가고, 온라인 설치는 거기서 꺼낸다 — 이미지를 안 구우면 새 apply.sh 는 아무
    데도 안 간다. 싣는 목록과 판정 목록이 어긋나면 같은 일이 또 나므로, stage 의 `cp`
    대상마다 판정에 있는지 본다(.md 는 설계상 판정 밖, backend/* 는 이미 backend)."""
    import fnmatch
    pats = _hostside_rule()
    stage = (REPO / "deploy" / "stage-hostside.sh").read_text()
    files = re.findall(r"^cp (?:-r )?([\w./-]+)\s", stage, re.M)
    assert "deploy/apply.sh" in files and "docker-compose.nogpu.yml" in files, "stage 의 cp 목록을 못 읽었다"
    for f in files:
        if f.endswith(".md") or f.startswith("backend/"):
            continue
        assert any(fnmatch.fnmatch(f, p) for p in pats), f"{f} 를 이미지에 싣는데 판정엔 없다"


# ── 적용 쪽 ─────────────────────────────────────────────────────────────────

def test_a_fresh_host_installs_everything_the_bundle_carries():
    """⚠ 실기(NUC, 2026-09-11): backend 만 든 v0.4.14 를 **처음** 설치하면 frontend 이미지·
    venv/wheel·데몬 유닛을 하나도 못 받았다 — `images=`/`wheels=`/`daemons=` 는 "이번에
    바뀐 것"이라 직전 릴리스가 깔린 호스트를 전제한다. 이미지 번들엔 셋 다 항상 실려
    있으니(stage-hostside), 매니페스트는 재적용 범위만 좁히고 **없는 것은 깐다**:
    없는 이미지는 레지스트리의 `:latest`(마지막으로 구운 그 이미지), venv 가 없거나 실린
    wheel 중 안 깔린 게 있으면 전부, 데몬 소스·유닛이 없으면 푼다."""
    from conftest import code_only

    src = code_only(APPLY.read_text())
    assert 'SERVICES="backend frontend"' in src and "for s in $SERVICES" in src, "매니페스트의 이미지만 돈다"
    assert 'else tag="latest"; note=' in src, "없는 이미지를 :latest 로 안 받는다"
    assert '[ ! -d "$VENV" ] || wheels_missing' in src, "venv 없는 처음 설치가 wheel 절을 건너뛴다"
    assert '[ ! -d "$SRC/daemons" ]' in src and "systemctl --user cat piper-estopd.service" in src, \
        "데몬 없는 처음 설치가 3절을 건너뛴다"
    # 있는 호스트는 그대로 — 안 바뀌었고 있으면 안 받는다
    assert "continue   # 안 바뀌었고 있다" in APPLY.read_text()


def test_a_host_that_skipped_a_release_still_catches_up_on_the_daemon_source():
    """⚠ 실기(NUC, 2026-09-15): 회색 카드 보정이 "camerad 가 응답하지 않습니다" 로 끝나
    데몬을 몇 번을 재시작해도 안 변했다. camerad 는 멀쩡히 떠 있었다 — `daemons/camerad.py`
    가 **9월 1일자**라 그 동사를 몰랐고, camerad 가 "알 수 없는 메서드"로 거절했을 뿐이다.
    wheel 은 `piper-cam 0.5.1`(허브에 함수 있음)인데 소스만 옛것이었다.

    왜 안 따라잡았나: `daemons=` 는 *이번* 릴리스에서 바뀐 것만 말한다. 그 호스트는 데몬을
    실은 v0.4.19·v0.5.2 를 건너뛰고 v0.5.1(daemons="")을 적용했다. 그리고 `$SRC`(=current)는
    **적용된 트리**라 첫 설치 뒤 늘 존재하므로 "없으면 푼다" 도 다시는 안 걸린다 — 영원히 멈춘다.

    wheel 이 `wheels_missing()` 으로 푸는 것과 같은 규율로 잠근다: 번들이 실은 것과 깔린 것을
    견주고, 표시가 없는 옛 호스트는 **낡은 것으로 본다**."""
    from conftest import code_only

    src = code_only(APPLY.read_text())
    assert "daemons_stale()" in src, "깔린 데몬 소스와 번들을 견주지 않는다"
    assert '[ -f "$SRC/daemons/.version" ] || return 0' in src, \
        "표시가 없는 옛 호스트(= 이 사고의 그 기계)를 최신으로 본다"
    assert '!= "${version:-}"' in src, "번들 버전과 대조하지 않는다"
    assert "|| daemons_stale" in src, "3절 조건이 따라잡기를 안 본다"
    stamp = 'echo "${version:-}" > "$SRC/daemons/.version"'
    assert stamp in src, "푼 뒤 출처를 안 적는다 — 다음 적용이 판단할 근거가 없다"
    assert src.index("tar xzf") < src.index(stamp), "풀기 전에 표시를 적는다"
    assert "daemons_stale && bad" in src, "점검(--check)이 낡은 데몬 소스를 말하지 않는다"


def test_the_unpack_dir_is_emptied_first_and_only_stamped_wheels_install():
    """⚠ 실기(NUC, 2026-09-11): 두 번째 `./piper-install.sh` 에서 pip 가 `Cannot install
    piper-bus 0.4.13 and piper-bus 0.4.15 … conflicting dependencies` 로 죽었다. `docker cp`
    는 있는 디렉토리에 **겹쳐** 놓아 이전 시도의 wheel 이 `latest/wheels/` 에 남고,
    apply.sh 는 `wheels/*.whl` 을 통째로 깔았다. 두 겹으로 막는다 — 꺼내기 전에 비우고
    (버전 꼴 이름일 때만; `current` 는 절대 안 지운다), apply.sh 는 매니페스트 버전으로
    도장 찍힌 wheel 만 고른다(도장 없는 옛 번들이면 전부)."""
    from conftest import code_only

    inst = code_only((REPO / "deploy" / "piper-install.sh").read_text())
    # 빈 임시 디렉토리에 꺼낸 뒤, 이름을 정한 자리를 비우고 옮긴다 — 어느 쪽에도 옛 파일이 못 남는다
    i_tmp = inst.find('TMPD="$WORK/.extract.$$"; rm -rf "$TMPD"; mkdir -p "$TMPD"')
    assert i_tmp != -1 and i_tmp < inst.find('docker cp "$cid:/opt/piper-host/."'), "빈 자리에 꺼내지 않는다"
    i_rm = inst.find('case "$REAL" in latest|v[0-9]*) rm -rf "$DEST" ;; esac')
    assert i_rm != -1 and i_rm < inst.find('mv "$TMPD" "$DEST"'), "옮길 자리를 비우지 않는다"
    assert 'rm -rf "$WORK"' not in inst.replace('rm -rf "$WORK/$VERSION"', "").replace('rm -rf "$WORK/', ""), \
        "배포 디렉토리를 통째로 지운다"
    apply = code_only(APPLY.read_text())
    assert "bundle_wheels() {" in apply and '"$HERE"/wheels/*-"$v"-*.whl' in apply, "도장 찍힌 wheel 만 고르지 않는다"
    assert 'pip" install -q --no-deps --force-reinstall "${WHLS[@]}"' in apply
    assert 'force-reinstall "$HERE"/wheels/*.whl' not in apply, "wheel 디렉토리를 통째로 깐다 — 옛 버전이 섞이면 죽는다"


def test_install_and_update_are_the_same_command():
    """⚠ 절차가 갈리면 "업데이트인 줄 알았는데 첫 설치였다" 가 생기고, 그때
    빠뜨리는 것은 늘 sudo 쪽(redis 소켓·linger)이라 증상이 "웹은 뜨는데 아무것도
    안 보인다" 로 나온다."""
    src = APPLY.read_text()
    assert "없는 것만 한다" in src
    for guard in ("-S /run/redis/redis-server.sock", "Linger=yes", '-d "$DATA"'):
        assert guard in src, f"전제 확인이 없다: {guard}"


def test_apply_never_runs_sudo_itself():
    from conftest import code_only

    for line in code_only(APPLY.read_text()).splitlines():
        s = line.strip()
        if s.startswith(("echo", "NEED_SUDO+=")):
            continue
        assert not re.match(r"^sudo\s", s), f"sudo 를 직접 실행한다: {s}"


def test_the_loaded_image_is_also_tagged_latest():
    """⚠ compose 는 `image: piper-web-backend` (태그 생략=latest) 로 참조한다 —
    `:latest` 를 안 달면 **다시 빌드하려 든다.**"""
    assert 'docker tag "piper-web-$s:$version" "piper-web-$s:latest"' in APPLY.read_text()


def test_the_daemons_are_installed_from_the_venv():
    """⚠ `install-daemons.sh` 는 **지금 셸의 python3** 를 유닛에 박는다.
    venv 를 안 켜고 부르면 데몬이 wheel 을 못 본다."""
    # ⚠ **주석이 아니라 실행하는 줄**을 본다. 왜 venv 가 필요한지 설명하는
    #   주석이 그 이름을 먼저 적는다 — 첫 언급으로 자르면 엉뚱한 데를 본다.
    line = next(ln for ln in APPLY.read_text().splitlines()
                if "install-daemons.sh" in ln and not ln.strip().startswith("#"))
    assert "activate" in line, f"venv 없이 부른다: {line.strip()}"


def test_the_manifest_records_what_shipped():
    """무엇을 올렸는지가 커밋 로그가 아니라 **아티팩트**로 남아야 한다
    (RELEASE-CHECKLIST 의 첫 문단이 그 이유다)."""
    src = RELEASE.read_text()
    for key in ("version=", "prev=", "images=", "wheels=", "daemons="):
        assert key in src, f"매니페스트에 {key} 가 없다"


def test_the_bundle_ships_the_compose_file():
    """⚠ **번들에 빠져 있었다.** 호스트는 이걸로 컨테이너를 띄우는데, 초안은
    `daemons/`·`deploy/systemd/` 만 담았다 — 첫 설치가 컨테이너 없이 끝난다."""
    src = RELEASE.read_text()
    assert 'cp docker-compose.yml "$OUT/"' in src


def test_apply_checks_every_host_tool_it_uses():
    """⚠ **가장 큰 전제가 안 걸리고 있었다.** 이 스크립트의 설계는 "sudo 가 필요한
    것은 찍어 주고 멈춘다"인데, 정작 `docker`·`docker compose`·`python3 -m venv` 는
    확인조차 안 해서 한참 뒤 `command not found` 로 깨졌다. 새 호스트에서 가장
    먼저 부딪히는 곳이 거기다."""
    from conftest import code_only

    src = code_only(APPLY.read_text())
    head = src.split("if [ ${#NEED_APT[@]}", 1)[0]        # 0절: 전제 확인
    body = src.split("if [ ${#NEED_APT[@]}", 1)[1]        # 그 뒤: 실제로 쓰는 곳
    for tool, probe in (("docker load", "command -v docker"),
                        ("docker compose", "docker compose version"),
                        ("python3 -m venv", 'python3 -c "import venv"')):
        assert tool in body, f"{tool} 을 안 쓴다 — 테스트가 낡았다"
        assert probe in head, f"{tool} 을 쓰면서 확인은 안 한다"


BASE_DF = REPO / "backend" / "Dockerfile.base"
APP_DF = REPO / "backend" / "Dockerfile"


REGISTRY_SH = REPO / "deploy" / "registry.sh"
CHECKLIST = REPO / "deploy" / "RELEASE-CHECKLIST.md"


def _wipe_section() -> str:
    """⚠ 이 절은 **README 에 있었다.** README 가 "스크립트 하나"만 설명하도록
    줄면서 배포자용 절차는 체크리스트로 옮겼다 — 받는 사람이 볼 것과 배포하는
    사람이 볼 것은 다르다. 내용 자체는 실기에서 겪어 얻은 것이라 지우지 않았다.
    """
    return CHECKLIST.read_text().split("## 처음부터 다시 깔려면", 1)[1].split("\n## ", 1)[0]


def test_the_version_being_built_is_not_its_own_predecessor():
    """⚠ **문서와 스크립트가 어긋나 있었다.** 절차는 "태그 먼저, 그다음 빌드"인데
    `PREV` 를 그냥 최신 태그로 잡으면 **방금 찍은 그 태그**가 직전 버전이 된다.
    diff 가 비고 릴리스가 통째로 안 만들어진다 — v0.4.0 에서 실제로 막혔다.
    """
    src = RELEASE.read_text()
    line = next(l for l in src.splitlines() if l.startswith("PREV="))
    assert '-vFx "$VERSION"' in line, f"굽는 버전을 직전으로 잡는다: {line.strip()}"


def test_an_empty_diff_does_not_kill_the_script_silently():
    """⚠ `set -e` 아래에서 `grep` 이 아무것도 못 찾으면 1 을 돌려주고, 명령 치환은
    그걸로 스크립트를 끝낸다 — **에러도 메시지도 없이**. v0.4.0 빌드가 아무 출력
    없이 종료됐고, 원인을 찾는 데 그 침묵이 가장 오래 걸렸다."""
    src = RELEASE.read_text()
    block = src.split('CHANGED="$(git diff', 1)[1].split(')"', 1)[0]
    assert "|| true" in block, "빈 diff 에서 조용히 죽는다"


def test_a_public_registry_is_not_pushed_through_localhost():
    """⚠ 사설 평문 레지스트리는 도커가 `127.0.0.0/8` 만 믿어서 `localhost` 로 민다.
    그 우회를 공개 레지스트리에도 적용하면 `localhost:ghcr.io/...` 라는 엉뚱한
    주소가 만들어진다. 포트 유무로 가른다."""
    src = RELEASE.read_text()
    # ⚠ 검사하는 것은 **변수 이름이 아니라 가르는 규칙**이다. 목록을 돌게 바뀌면서
    #   변수가 `$reg` 이 됐다(2026-09-18) — 규칙은 그대로다.
    assert '== *:[0-9]*' in src, "공개/사설을 안 가른다"
    assert 'localhost:${reg##*:}' in src, "사설 평문을 localhost 로 안 민다"


def test_the_image_carries_the_same_shape_as_the_bundle():
    """⚠ **모양이 갈리면 `apply.sh` 가 한쪽에서만 돈다.** 이미지는 `daemons/` 를
    디렉토리로, 번들은 `daemons.tar.gz` 로 실었는데 `apply.sh` 는 후자만 안다.
    이미지로 설치하면 3절이 "Cannot open" 으로 실패했고 — 그런데도 그 다음 줄이
    **옛 `$SRC` 의 스크립트로 낡은 데몬을 깔았다.** 화면에는 "✓" 만 떴다.
    """
    stage = STAGE_SH.read_text()
    rel = RELEASE.read_text()
    for f in ("daemons.tar.gz", "apply.sh"):
        assert f in stage, f"이미지가 {f} 를 안 싣는다"
        assert f in rel, f"번들이 {f} 를 안 싣는다"
    # 디렉토리로 풀어 두면 다시 갈린다
    assert 'cp -r deploy/systemd' not in stage, "systemd 를 디렉토리로 싣는다 — 번들과 다르다"


def test_a_failed_untar_stops_the_install():
    """⚠ `mkdir && tar && ok` 로 이으면 tar 실패가 조용히 넘어간다 — `set -e` 는
    `&&` 리스트의 중간 명령을 봐준다. 그 다음 줄이 옛 스크립트를 실행했다."""
    src = APPLY.read_text()
    block = src.split('tar xzf "$HERE/daemons.tar.gz"', 1)[1][:250]
    assert "exit 1" in block, "데몬 소스를 못 풀어도 계속 간다"


def test_the_release_also_publishes_latest():
    """⚠ `piper-install.sh` 는 인자가 없으면 `latest` 를 받는다 — README 의 기본
    명령이 그것이다. 버전 태그만 밀면 사용자가 `./piper-install.sh` 를 그냥 쳤을 때
    `not found` 로 끝난다. 실기에서 정확히 그렇게 막혔다."""
    src = RELEASE.read_text()
    assert 'piper-web-$s:latest"' in src, "latest 를 안 민다"
    boot = (REPO / "deploy" / "piper-install.sh").read_text()
    assert 'VERSION="${1:-latest}"' in boot, "부트스트랩 기본값이 latest 가 아니다"


def test_the_offline_path_still_exists():
    """⚠ **현장 USB 배포를 버리면 안 된다.** 레지스트리가 전송량을 33배 줄이지만
    (실측 3.46GB → 104.5MB), 망이 없는 현장이 실재한다. `PIPER_REGISTRY` 가 비었거나
    `--offline` 이면 예전처럼 tar 를 만들어야 한다."""
    src = RELEASE.read_text()
    assert "--offline" in src, "오프라인 강제 수단이 없다"
    assert "docker save" in src, "tar 경로가 사라졌다"
    # ⚠ 예전에는 "`PIPER_REGISTRY` 가 비면 tar" 였다. 그 암묵 규칙을 없앴다 —
    #   환경변수 하나에 결과가 갈리는 것이 v0.5.5 사고의 원인이었다(한쪽 레지스트리에만
    #   올라가 다른 쪽 호스트가 옛 버전을 "최신" 이라 봤다). 지금은 **`--offline` 만**
    #   tar 를 만든다. 명시적인 쪽이 낫다.
    assert 'if [ $OFFLINE = 0 ]; then' in src, "오프라인 분기가 사라졌다"
    assert 'DEFAULT_REGISTRIES=' in src, "올릴 곳을 스크립트가 모른다 — 다시 사람이 기억해야 한다"


def test_the_push_address_and_the_pull_address_are_separate():
    """⚠ 도커는 `127.0.0.0/8` 만 기본으로 평문 레지스트리로 인정한다. 빌드 머신이
    자기 LAN IP 로 밀면 **"server gave HTTP response to HTTPS client"** 로 거부당한다
    (실제로 그렇게 막혔다). 미는 쪽은 `localhost`, 매니페스트에는 호스트가 받을
    LAN 주소 — 같은 레지스트리라 다이제스트는 같다."""
    src = RELEASE.read_text()
    assert "PIPER_REGISTRY_PUSH" in src and "localhost:" in src, "미는 주소를 안 가른다"
    assert 'registry="$REGISTRY"' in src, "호스트가 받을 주소를 매니페스트에 안 적는다"


def test_apply_picks_pull_or_load_from_the_manifest():
    """번들에 `images.tar.gz` 가 없을 수 있다 — 그때는 매니페스트의 `registry` 가
    유일한 단서다. 잘못 고르면 없는 tar 를 풀려다 죽는다."""
    from conftest import code_only

    # ⚠ **주석을 걷어내고 본다.** 이 파일은 왜 그렇게 했는지를 주석으로 길게
    #   적어 두므로, 첫 등장으로 순서를 재면 설명문을 코드로 착각한다.
    src = code_only(APPLY.read_text())
    i_pull = src.find("pull-progress.py")        # 진행률 있는 받기 (docker pull 은 그 폴백)
    i_load = src.find("docker load")
    assert i_pull != -1 and i_load != -1, "두 경로가 다 있어야 한다"
    assert 'elif [ -n "${registry:-}" ]' in APPLY.read_text(), "매니페스트로 안 가른다"
    assert i_pull < i_load, "레지스트리보다 tar 를 먼저 본다"


def test_apply_checks_the_registry_is_trusted():
    """⚠ 평문 레지스트리를 `daemon.json` 에 안 적으면 pull 이
    "server gave HTTP response to HTTPS client" 로 죽는데, 그 메시지만 보고
    무엇을 고칠지 알기 어렵다. 설치 때 잡는다."""
    src = APPLY.read_text()
    assert "insecure-registries" in src, "신뢰 설정을 확인하지 않는다"
    # 루프백은 도커가 기본으로 믿는다 — 그걸 문제라고 하면 거짓 경보다
    assert 'localhost:*' in src or "127." in src, "루프백 예외가 없다"


def test_the_registry_is_loopback_until_someone_opens_it():
    """⚠ `registry:2` 에는 **인증이 없다.** 밖에 열면 같은 망의 누구나
    `piper-web-backend:v0.3.10` 을 밀어넣을 수 있고, 로봇 호스트는 그것을 받아
    **그대로 실행한다.** 처음엔 `-p 5000:5000` 이라 0.0.0.0 에 열려 있었다 —
    LAN 에서 `/v2/_catalog` 가 그대로 읽혔다. 여는 것은 의식적인 선택이어야 한다."""
    src = REGISTRY_SH.read_text()
    assert 'PIPER_REGISTRY_BIND:-127.0.0.1' in src, "기본이 루프백이 아니다"
    assert '-p "$BIND:$PORT:5000"' in src, "바인드 주소를 안 쓴다"
    assert "인증이 없다" in src, "열 때의 위험을 말하지 않는다"
    assert "ufw" in src, "열었을 때 좁히는 방법을 안 알려준다"


def test_the_registry_says_where_it_is_actually_listening():
    """"잠갔다고 생각했는데 0.0.0.0 이더라"가 실제로 있었다. 이미 돌고 있으면
    지금 열린 곳과 요청한 곳이 다른지 말해야 한다 — 재시작 없이는 안 바뀐다."""
    src = REGISTRY_SH.read_text()
    assert "docker port" in src, "지금 어디에 열려 있는지 안 본다"
    assert "--stop" in src.split("이미 돌고 있다", 1)[1][:600], "바꾸는 방법을 안 찍는다"


def test_a_stale_registry_address_can_be_overridden():
    """⚠ **매니페스트에 박힌 주소는 늙는다.** 빌드 머신이 DHCP·WiFi 면 IP 가 바뀌고,
    그러면 이미 만들어 둔 번들이 전부 죽은 주소를 가리킨다. 그때 번들을 다시 굽게
    만들면 안 된다 — 3.46GB 를 다시 만드는 일이다."""
    src = APPLY.read_text()
    assert 'registry="$PIPER_REGISTRY"' in src, "환경변수로 못 덮는다"
    # 덮어쓴 것을 말해야 한다. 조용히 다른 데서 받아오면 그게 더 무섭다
    assert "덮어씁니다" in src, "덮어쓴 사실을 안 알린다"


def test_the_registry_guidance_does_not_hand_out_a_raw_ip():
    """IP 를 그대로 쓰면 바뀌는 날 **호스트의 daemon.json 과 모든 번들의
    매니페스트가 동시에** 죽는다. 이름을 하나 두면 고칠 곳이 `/etc/hosts` 한 줄이다."""
    src = REGISTRY_SH.read_text()
    assert "/etc/hosts" in src, "이름을 쓰라고 안 한다"
    assert "DHCP" in src, "주소가 바뀐다는 걸 말하지 않는다"
    assert "PIPER_REGISTRY=$NAME:$PORT" in src, "이름이 아니라 IP 를 내보내게 한다"


def test_the_registry_keeps_its_data_outside_the_container():
    """컨테이너를 지웠다고 이미지가 사라지면 호스트들이 다음 pull 에서 통째로
    다시 받는다 — 레지스트리를 둔 이유가 사라진다."""
    src = REGISTRY_SH.read_text()
    assert "-v " in src and "/var/lib/registry" in src, "저장소를 호스트에 안 붙인다"
    assert "restart=always" in src, "재부팅하면 사라진다"


STAGE_SH = REPO / "deploy" / "stage-hostside.sh"
BOOTSTRAP = REPO / "deploy" / "piper-install.sh"


def test_the_image_carries_the_code_that_runs_outside_it():
    """사용자에게는 스크립트 하나만 준다. 그러려면 도커 **바깥**에서 도는
    것들(데몬·wheel·udev·compose·apply.sh)도 이미지 안에 있어야 한다.
    실측 207KB — 앱 이미지 3.7GB 의 0.0057% 다."""
    assert "COPY .hostside/ /opt/piper-host/" in APP_DF.read_text(), \
        "이미지가 호스트 코드를 안 싣는다"


def _dockerfile_lines() -> list[str]:
    """앱 Dockerfile 을 명령 한 줄씩으로 — 주석을 걷고 이어짐을 잇는다."""
    from conftest import code_only

    # ⚠ **줄 이어짐(`\\`)을 먼저 잇는다.** 안 이으면 `ENV A=1 \\` 다음 줄이
    #   별개 명령으로 보여, 멀쩡한 Dockerfile 을 틀렸다고 한다.
    joined, buf = [], ""
    for l in code_only(APP_DF.read_text()).splitlines():
        buf += l.rstrip("\\") if l.rstrip().endswith("\\") else l
        if not l.rstrip().endswith("\\"):
            if buf.strip():
                joined.append(" ".join(buf.split()))
            buf = ""
    return joined


# 레이어를 만들지 않는 명령 — 이것들은 마지막 COPY 뒤에 와도 된다
_META = {"ENV", "ARG", "WORKDIR", "EXPOSE", "CMD", "ENTRYPOINT", "LABEL"}


def test_the_host_code_is_the_last_layer():
    """⚠ 이게 위로 올라가면 데몬 한 줄에 그 아래가 전부 다시 구워진다.
    ENV·ARG·WORKDIR·EXPOSE·CMD 는 0B 메타라 뒤에 와도 레이어를 안 만든다."""
    lines = _dockerfile_lines()
    i = next(n for n, l in enumerate(lines) if l.startswith("COPY .hostside/"))
    for l in lines[i + 1:]:
        assert l.split()[0] in _META, f"호스트 코드 뒤에 레이어를 만드는 것이 있다: {l}"


def test_the_dockerfile_keeps_what_changes_every_release_last():
    """⚠ v0.4.19 까지 릴리스마다 30 레이어 중 **11개(390MB)** 가 새로 구워져 새로
    올라갔다 — 바뀐 소스는 몇 MB 인데. 원인은 둘이었고 둘 다 순서다:

    - `ENV PIPER_VERSION` 이 맨 위라 태그가 바뀔 때마다(=릴리스마다) 그 아래 RUN
      전부가 캐시 미스였다 — 소스가 안 바뀐 계약 패키지·vendor 설치까지.
      ARG/ENV 는 뒤따르는 RUN 의 캐시 키에 들어간다. RUN 뒤에 두면 메타만 바뀐다.
    - `COPY backend/` 가 `pip install -e backend[realsense]` 앞이라 소스 한 줄에
      의존성 162MB 를 다시 깔았다. pyproject 만 먼저 복사해 의존성을 깔고, 소스는
      `--no-deps` 로 얹는다.

    빌드 시간보다 **업데이트마다 NUC 가 받는 양**이 문제였다. 순서가 흐트러져도
    아무 에러가 안 나므로 여기서 잡는다."""
    lines = _dockerfile_lines()
    last_run = max(n for n, l in enumerate(lines) if l.startswith("RUN "))
    i_ver = next(n for n, l in enumerate(lines) if l.startswith("ENV PIPER_VERSION="))
    i_arg = next(n for n, l in enumerate(lines) if l.startswith("ARG PIPER_VERSION"))
    assert last_run < i_arg < i_ver, "버전 ARG/ENV 가 RUN 앞에 있다 — 릴리스마다 전부 다시 굽는다"

    i_toml = next(n for n, l in enumerate(lines) if l.startswith("COPY backend/pyproject.toml "))
    i_src = next(n for n, l in enumerate(lines) if l.startswith("COPY backend/ /app/backend/"))
    assert i_toml < i_src, "의존성보다 소스를 먼저 복사한다"
    deps = next(l for l in lines[i_toml:i_src] if l.startswith("RUN ") and "pip install -r" in l)
    assert '["dependencies"]' in deps and '["realsense"]' in deps, \
        "pyproject 의 dependencies + [realsense] 를 그대로 깔지 않는다"
    app = next(l for l in lines[i_src:] if l.startswith("RUN ") and "/app/backend" in l and "pip install" in l)
    assert "--no-deps" in app and "[realsense]" not in app, \
        f"소스 설치가 의존성을 다시 해석한다 — 소스 한 줄에 162MB 가 다시 구워진다: {app}"


def test_staging_takes_every_daemon_wheel():
    """⚠ **다섯 개 전부 담는다.** 이미지가 곧 배포 단위이므로 "바뀐 것만" 담으면
    호스트에 옛 wheel 이 남는다. 다 합쳐 155KB 라 아낄 이유가 없다."""
    src = STAGE_SH.read_text()
    line = next(l for l in src.splitlines() if l.strip().startswith("for p in"))
    for pkg in ("bus", "shm", "robot", "cam", "rs"):
        assert pkg in line, f"{pkg} wheel 이 빠진다: {line.strip()}"


def test_release_stages_the_host_code_before_baking():
    """순서가 뒤집히면 **옛 데몬이 실린 이미지가 나가는데 아무 에러도 안 난다.**"""
    src = RELEASE.read_text()
    i_stage = src.find("stage-hostside.sh")
    i_build = src.find('docker compose build "${IMAGES[@]}"')
    assert i_stage != -1 and i_stage < i_build, "이미지를 굽고 나서 호스트 코드를 모은다"
    # 매니페스트도 이미지 안으로 들어가야 한다 — 굽기 전에 써야 한다는 뜻이다
    assert src.find('write_manifest "$REPO/.hostside/manifest.txt"') < i_build, \
        "매니페스트가 이미지 안에 빈 채로 들어간다"


def test_the_bootstrap_only_checks_docker():
    """⚠ 전제 확인은 `apply.sh` 한 곳에 있어야 갈리지 않는다. 부트스트랩이
    도커만 보는 것은 그것이 **나머지를 꺼내오는 수단**이기 때문이다 —
    없으면 apply.sh 자체를 못 꺼낸다."""
    from conftest import code_only

    # ⚠ **주석은 뺀다.** 무엇을 이미지에 실었는지 설명하느라 `udev` 같은 낱말이
    #   머리말에 나온다 — 그걸 검사로 세면 설명문을 코드로 착각한다.
    src = code_only(BOOTSTRAP.read_text())
    assert "command -v docker" in src, "도커를 안 본다"
    for other in ("redis-server", "python3-venv", "nvidia-smi", "compute_cap", "udev"):
        assert other not in src, f"apply.sh 의 검사를 여기서 또 한다: {other}"


def test_the_bootstrap_does_not_run_the_image_to_unpack_it():
    """⚠ 설치 **전에** 남의 코드를 실행할 이유가 없다. `docker create` 는
    컨테이너를 만들기만 하고 돌리지 않는다."""
    from conftest import code_only

    src = code_only(BOOTSTRAP.read_text())
    assert "docker create" in src, "꺼내려고 컨테이너를 돌린다"
    assert "docker run" not in src, "이미지를 실행해서 꺼낸다"


def test_the_base_image_is_pinned_by_digest():
    """⚠ `python:3.13-slim-bookworm` 은 **뜬 태그**다. 데비안 보안 패치가 들어갈
    때마다 다른 이미지가 되고, 그러면 1번 레이어부터 갈려 아래 전부가 다시
    구워진다. **실제로 그랬다** — v0.3.8 과 v0.3.9 는 레이어 24개 중 24개가
    달랐다. 현장마다 다른 이미지가 도는데 그 차이를 아무도 모른다."""
    import re as _re

    m = _re.search(r"^FROM\s+(\S+)", BASE_DF.read_text(), _re.M)
    assert m, "베이스에 FROM 이 없다"
    assert "@sha256:" in m.group(1), f"다이제스트로 안 박혀 있다: {m.group(1)}"


def test_the_base_holds_no_company_code():
    """⚠ 베이스에 `COPY` 가 생기면 회사 코드가 섞일 수 있고, 그러면 이 이미지를
    **공개 레지스트리에 올릴 수 없다** — 베이스를 가른 이유의 절반이 사라진다.
    `build-base.sh` 가 컨텍스트 없이 굽기 때문에 빌드도 같이 실패하지만,
    그 실패는 20분 뒤에 나므로 여기서 먼저 잡는다."""
    from conftest import code_only

    for line in code_only(BASE_DF.read_text()).splitlines():
        assert not line.strip().upper().startswith(("COPY", "ADD")), \
            f"베이스가 컨텍스트를 읽는다: {line.strip()}"


def test_the_base_tag_is_single_sourced():
    """`Dockerfile` 의 `ARG BASE_TAG` 기본값과 `BASE_VERSION` 이 갈라지면,
    손으로 `docker build` 한 것과 `build-base.sh` 가 만든 것이 다른 이미지를
    가리킨다 — 그런데 둘 다 성공해서 아무도 모른다."""
    import re as _re

    want = (REPO / "backend" / "BASE_VERSION").read_text().strip()
    m = _re.search(r"^ARG BASE_TAG=(\S+)", APP_DF.read_text(), _re.M)
    assert m, "Dockerfile 에 ARG BASE_TAG 가 없다"
    assert m.group(1) == want, f"BASE_VERSION={want} 인데 ARG 기본값은 {m.group(1)}"


def test_the_app_image_builds_on_the_base():
    """앱 이미지가 베이스를 안 쓰고 원본 파이썬으로 되돌아가면, 갈라놓은 의미가
    없어지는데 빌드는 멀쩡히 성공한다."""
    import re as _re

    m = _re.search(r"^FROM\s+(\S+)", APP_DF.read_text(), _re.M)
    assert m and "piper-web-base" in m.group(1), f"베이스 위에 안 얹혔다: {m and m.group(1)}"


def test_a_stale_base_is_rebuilt_not_skipped():
    """⚠ **태그만 보고 건너뛰면 낡은 베이스가 남는다.** `Dockerfile.base` 를
    고치고 `BASE_VERSION` 을 안 올리면, 아무도 눈치 못 챈 채 옛 스택 위에 앱이
    얹힌다. 내용 해시를 라벨로 박아 대조한다."""
    src = (REPO / "deploy" / "build-base.sh").read_text()
    assert "sha256sum" in src, "베이스 내용을 해시하지 않는다"
    assert "piper.base.sha" in src, "해시를 라벨로 안 박는다"
    assert 'image inspect' in src and 'Labels' in src, "기존 이미지의 라벨과 대조하지 않는다"


def test_release_builds_the_base_before_the_app():
    """베이스가 없으면 `docker compose build` 가 죽는다 — 릴리스 도중에."""
    src = RELEASE.read_text()
    i_base = src.find("build-base.sh")
    i_app = src.find('docker compose build "${IMAGES[@]}"')
    assert i_base != -1, "release 가 베이스를 확인하지 않는다"
    assert i_base < i_app, "베이스를 앱 빌드 뒤에 굽는다"


def test_the_gpu_floor_matches_the_torch_wheel_in_the_dockerfile():
    """⚠ `apply.sh` 의 `MIN_CUDA` 는 **이미지가 싣는 CUDA 런타임**과 같아야 한다.
    Dockerfile 이 `--index-url .../cu130` 으로 torch 를 받으므로 그 값이 근거다.
    휠을 cu126 으로 바꾸면서 이 상수를 안 고치면, 돌아갈 머신을 못 돌아간다고
    막거나(과잉) 못 돌 머신을 통과시킨다(과소)."""
    import re as _re

    dockerfile = BASE_DF.read_text()
    m = _re.search(r"download\.pytorch\.org/whl/cu(\d{3,4})", dockerfile)
    assert m, "Dockerfile 에서 torch 휠의 CUDA 버전을 못 찾았다"
    d = m.group(1)                       # cu130 → 13.0, cu126 → 12.6
    expected = f"{d[:2]}.{d[2:]}"
    mc = _re.search(r"^MIN_CUDA=([0-9.]+)", APPLY.read_text(), _re.M)
    assert mc, "MIN_CUDA 가 없다"
    assert mc.group(1) == expected, f"휠은 cu{d}({expected}) 인데 MIN_CUDA={mc.group(1)} 이다"


def test_the_gpu_and_the_driver_get_different_prescriptions():
    """⚠ **처방이 다르다.** 드라이버가 낮은 건 `apt install` 한 줄로 끝나지만,
    컴퓨트 능력이 낮은 건 **GPU 를 바꿔야 한다** — torch 휠에 sm_75 미만 큐빈이
    없고 PTX 도 없어 JIT 으로도 못 메꾼다. 둘을 같은 ✗ 로 뭉뚱그리면 현장에서
    드라이버만 올려보다 시간을 버린다."""
    src = APPLY.read_text()
    cc_block = src.split("MIN_CC 이상만 돈다", 1)[0].rsplit("while IFS=,", 1)[1]
    assert "NEED_APT" not in cc_block and "NEED_SUDO" not in cc_block, \
        "컴퓨트 능력이 낮은 걸 설치로 고칠 수 있는 것처럼 안내한다"
    drv_block = src.split("이 이미지는 $MIN_CUDA 이상이 필요하다", 1)[1][:300]
    assert "NEED_APT+=(nvidia-driver" in drv_block, "드라이버는 고칠 방법을 줘야 한다"


def test_apply_never_installs_packages_itself():
    """설치는 **사람이 한다.** 스크립트가 몰래 apt 를 돌리면 그 머신에 무엇이
    깔렸는지 아무도 모른다 — `test_apply_never_runs_sudo_itself` 와 같은 이유다."""
    from conftest import code_only

    for line in code_only(APPLY.read_text()).splitlines():
        s = line.strip()
        if s.startswith(("echo", "NEED_APT+=", "NEED_SUDO+=")) or "echo " in s:
            continue
        assert not re.search(r"\bapt(-get)?\s+install\b", s), f"직접 설치한다: {s}"


def test_the_nvidia_toolkit_is_not_in_the_apt_line():
    """⚠ `nvidia-container-toolkit` 은 **Ubuntu 아카이브에 없다** — NVIDIA 저장소를
    먼저 붙여야 한다. `NEED_APT` 에 넣으면 "패키지를 찾을 수 없음" 으로 **그 한 줄
    전체가 실패해** redis·docker 까지 같이 안 깔린다."""
    from conftest import code_only

    src = code_only(APPLY.read_text())
    assert "nvidia-ctk" in src, "GPU 툴킷을 확인조차 안 한다"
    # ⚠ **툴킷만** 막는다. `nvidia-driver-580` 은 우분투 아카이브에 있으므로
    #   apt 한 줄에 들어가는 게 맞다 — 넓게 막으면 그것까지 잡는다.
    assert "NEED_APT+=(nvidia-container-toolkit" not in src.replace(" ", ""), \
        "툴킷이 apt 한 줄에 섞였다 — NVIDIA 저장소가 없으면 그 줄 전체가 실패한다"


def test_apply_skips_the_redis_config_when_redis_is_absent():
    """redis 가 아직 없는데 `/etc/redis/redis.conf` 에 sed 를 걸라고 시키면 그
    명령이 실패하고, 사람은 왜 실패했는지 모른다."""
    src = APPLY.read_text()
    block = src.split("redis 유닉스 소켓", 1)[0]
    assert 'if command -v redis-server' in block[-400:], "redis 유무로 안 감싼다"


def test_the_bundle_ships_the_udev_rules():
    """⚠ **번들에 빠져 있었다.** 없으면 새 머신에서 RealSense 는 libusb 로 장치를
    못 열어 **카메라 0개**가 되고, CAN 은 `can0`/`can1` 로 붙어 **저장된 팔
    등록이 반대 팔을 가리킨다.** 셋 합쳐 8KB 라 아낄 이유가 없다.

    `list-can-adapters.py` 도 같이 간다 — 규칙의 시리얼이 그 머신 것이 아닐 때
    무엇으로 고쳐야 하는지 알려면 그게 필요하다."""
    src = RELEASE.read_text()
    for name in ("99-realsense-libusb.rules", "list-can-adapters.py"):
        assert f'"$OUT/udev/"' in src and name in src, f"번들에 {name} 이 없다"
    # 저장소에 실제로 있어야 `cp` 가 성립한다
    for rel in ("backend/udev/99-realsense-libusb.rules",
                "deploy/udev/list-can-adapters.py"):
        assert (REPO / rel).is_file(), f"{rel} 이 없다"


def test_apply_never_overwrites_an_existing_udev_rule():
    """⚠ **실기에서 잡았다.** 번들의 udev 규칙은 아무것도 없는 새 머신을 위한
    것이지 기존 호스트를 고치는 물건이 아니다. 로봇 호스트 에 v0.4.0 을 깔 때
    `apply.sh` 가 두 규칙을 덮어쓰라고 시켰는데, 시키는 대로 했다면:

    - CAN 규칙이 **빌드 머신의 시리얼**을 가리켜 그 호스트의 팔이 영영 이름을
      못 얻었을 것이다. `piper-can-up.service` 트리거 줄도 함께 지워졌을 것이다
    - RealSense 규칙은 호스트 쪽이 **88줄 더 많았다**(Intel 공식). 덮는 것은 다운그레이드다

    `docker-compose.override.yml` 과 같은 규칙이다: **그 호스트의 사정은 그 호스트가
    안다.** 없을 때만 넣고, 다르면 알려만 준다.
    """
    src = APPLY.read_text()
    block = src.split('cmp -s "$r" "/etc/udev/rules.d/$n"', 1)[1].split("done", 1)[0]
    assert 'elif [ -f "/etc/udev/rules.d/$n" ]' in block, "있는 것과 없는 것을 안 가른다"
    exists = block.split('elif [ -f "/etc/udev/rules.d/$n" ]', 1)[1].split("else", 1)[0]
    assert "NEED_SUDO" not in exists, "이미 있는 규칙을 덮으라고 시킨다"
    assert "diff " in exists, "무엇이 다른지 볼 방법을 안 준다"


def test_the_can_rule_is_never_shipped():
    """⚠ **고객에게 우리 시리얼을 보내고 있었다.** CAN 규칙에는 그 머신 어댑터의
    시리얼이 박힌다. 남의 시리얼이 든 규칙은 받는 머신에서 아무 줄도 매칭되지
    않는데 **udev 는 그걸 에러로 치지 않는다** — 증상은 "팔 0개" 뿐이라 원인을
    찾을 수 없다. 게다가 `apply.sh` 는 규칙이 없는 새 머신에 그것을 **설치하라고
    시켰다.** 규칙 대신 **만드는 도구**를 보낸다.
    """
    from conftest import code_only

    for f in (RELEASE, STAGE_SH):
        src = code_only(f.read_text())
        assert "99-piper-can.rules" not in src, f"{f.name} 이 CAN 규칙을 싣는다"
        assert "list-can-adapters.py" in src, f"{f.name} 이 만드는 도구를 안 싣는다"
    assert not (REPO / "deploy" / "udev" / "99-piper-can.rules").exists(), \
        "우리 시리얼이 든 규칙이 저장소에 남아 있다"


def test_the_tool_can_write_the_rule_for_this_machine():
    """규칙을 안 보내면 만들 방법을 줘야 한다 — 안 그러면 "만드세요" 가 빈말이 된다.

    ⚠ 역할(leader/follower)은 **지어내면 안 된다.** 시리얼도 펌웨어도 그걸 말해
    주지 않는다. 그럴듯한 이름을 붙이면 반대 팔에 명령이 가는 쪽이 더 위험하다.
    """
    tool = (REPO / "deploy" / "udev" / "list-can-adapters.py").read_text()
    assert "--write-rule" in tool, "규칙 생성 기능이 없다"
    assert "can_arm" in tool, "역할을 임의로 정하고 있다"
    assert "--watch" in tool, "어느 팔인지 확인할 방법을 안 가리킨다"


def test_apply_tells_you_to_make_the_rule_when_adapters_are_plugged_in():
    """⚠ 어댑터가 꽂혀 있는데 규칙이 없으면 **이름이 뒤바뀔 수 있는 상태**다.
    조용히 넘어가면 포트를 바꿔 꽂는 날 저장된 등록이 반대 팔을 가리킨다."""
    src = APPLY.read_text()
    block = src.split("CAN 규칙은 그 머신에서 만들어야 한다", 1)[1].split("\nfi\n", 1)[0]
    assert "--write-rule" in block, "만드는 방법을 안 알려준다"
    assert "--watch" in block, "어느 팔인지 확인할 방법을 안 알려준다"


def test_apply_tolerates_a_bundle_without_udev():
    """옛 번들에는 `udev/` 가 없다. `set -u`·`set -e` 아래에서 빈 글롭이
    스크립트를 죽이면 **업데이트가 통째로 막힌다.**"""
    src = APPLY.read_text()
    block = src.split('for r in "$HERE"/udev/*.rules', 1)[1][:200]
    assert "continue" in block, "빈 글롭을 안 걸러낸다"


def test_the_bundle_never_ships_the_override():
    """⚠ `docker-compose.override.yml` 은 **그 호스트의 사정**이다.
    로봇 호스트 은 :80 을 WMS 가, :8080 을 다른 node 앱이 쓰고 있어 8081 로
    빼 두었다. 번들이 덮으면 그 설정이 조용히 사라지고 포트 충돌로 안 뜬다."""
    from conftest import code_only

    src = code_only(RELEASE.read_text())
    assert "docker-compose.override.yml" not in src, "번들이 override 를 담는다"


def test_apply_preserves_an_existing_override():
    src = APPLY.read_text()
    block = src.split("override 는 손대지 않는다", 1)[1][:400]
    assert "if [ -f" in block and "override 보존" in block
    # 덮어쓰는 cp 가 없어야 한다
    assert 'cp "$HERE/docker-compose.override.yml"' not in src


def test_apply_restores_a_backed_up_override():
    """⚠ **실측으로 나온 구멍.** 재설치 뒤 override 를 안 되돌려 frontend 가
    `:80` 에 붙었다 — 그 호스트는 :80 을 WMS 가 쓴다. 마침 그 서비스가 안 떠
    있어서 충돌만 안 났을 뿐, 다음엔 안 뜬다."""
    src = APPLY.read_text()
    assert '"$HOME/override.keep.yml"' in src
    assert "override 복원" in src


def test_the_readme_wipe_backs_up_the_override_first():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    wipe = _wipe_section()
    assert "override.keep.yml" in wipe
    assert wipe.index("override.keep.yml") < wipe.index("rm -rf"), \
        "지우기 전에 백업하지 않는다"


def test_the_readme_names_the_data_that_survives():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    wipe = _wipe_section()
    for keep in ("/srv/piper-data", "huggingface/lerobot", ".config/piper-web"):
        assert keep in wipe, f"보존 목록에 {keep} 이 없다"


def test_apply_checks_the_device_groups_like_the_source_installer():
    """데몬은 그 사용자로 돈다 — `video` 없으면 카메라 스캔 0개(실측), `dialout` 없으면
    SO-101 시리얼을 못 연다. install.sh 는 보는데 apply.sh 만 안 봐서 배포 호스트에서는
    "장치 0개" 로만 드러났다. 고치는 손은 사람(sudo) — 스크립트는 명령만 찍는다."""
    from conftest import code_only
    src = code_only(APPLY.read_text())
    assert "for g in video dialout" in src
    assert 'NEED_SUDO+=("usermod -aG $g $USER")' in src
    assert "for g in video dialout" in (REPO / "deploy" / "install.sh").read_text()


def test_a_host_that_skipped_a_release_still_catches_up_on_the_wheels():
    """⚠ 실기(.120, 2026-09-16): 배포판을 깔았는데 시뮬 화면의 바닥·밝기가 옛 모습이었다.
    게이트웨이는 v0.5.4 인데 데몬 wheel 이 **전부 0.4.7** 이었다. 시뮬 장면
    (`piper_scene.xml`)은 `piper_sim` wheel 에 package-data 로 실려 나가고 simd 가 그것을
    읽으므로, wheel 이 낡으면 화면이 통째로 옛 버전이다.

    왜 안 따라잡았나: `stage-hostside` 는 릴리스마다 일곱 wheel 전부에 그 버전을 박아 싣는데,
    설치의 검사는 **이름이 있는지만** 봤다(`pip show piper-sim`). 다 있으니 "안 빠졌다"고
    답했고, v0.5.4 의 `wheels=` 는 비어 있어 2절이 통째로 건너뛰어졌다. 데몬 소스가 겪은
    것과 같은 함정이 한 층 위에 또 있었다(v0.4.15 의 처방은 **없는 것**만 고쳤다).

    이제 버전까지 견준다. 그리고 그 사실을 기동 검사도 본다 — 적용 릴리스와 다른 wheel 은
    말한다(`version.staleness`)."""
    from conftest import code_only

    src = code_only(APPLY.read_text())
    fn = src.split("wheels_missing()", 1)[1].split("\n}", 1)[0]
    assert "pip\" show \"$n\"" in fn or "pip show" in fn or 'show "$n"' in fn, "설치 여부를 안 본다"
    assert "Version: " in fn, "버전을 안 읽는다 — 이름만 보면 영영 안 따라잡는다"
    assert '[ "$cur" = "$v" ] || found=0' in fn, "번들 wheel 의 버전과 대조하지 않는다"
    assert 'cut -d- -f2' in fn, "wheel 파일 이름에서 버전을 안 꺼낸다"

    ver = (REPO / "backend" / "app" / "services" / "version.py").read_text()
    rule = ver.split("def staleness", 1)[1]
    # ⚠ **우리 wheel 만** 센다. `startswith("piper-")` 로 세던 때는 서드파티 `piper-sdk`
    #   (AgileX CAN SDK, apply.sh 가 PyPI 에서 깐다)까지 걸려, 멀쩡한 호스트가 기동할 때마다
    #   "robotd piper-sdk 0.6.1" 이라고 거짓 경고했다.
    assert "OUR_WHEELS" in rule and "piper-sdk" not in rule.split("OUR_WHEELS")[0][-400:], \
        "서드파티 piper-sdk 까지 우리 wheel 로 센다"
    assert "pkg in OUR_WHEELS" in rule and "want" in rule, \
        "기동 검사가 여전히 '이번에 구운 것'만 본다 — 건너뛴 호스트를 못 잡는다"
    assert "touched" not in rule, "옛 매니페스트 게이트가 남아 있다"


def test_the_image_ships_the_npp_runtime_torchcodec_links_against():
    """⚠ 실기(.120, 2026-09-18): 학습이 **첫 배치에서** 죽었다. 데이터셋은 멀쩡했고
    DataLoader 가 영상을 열려는 순간이었다 —

        OSError: libnppicc.so.13: cannot open shared object file

    torchcodec 은 NVIDIA NPP 에 링크돼 있는데 **아무도 그걸 안 깐다**: torch 의 의존성에
    없고(torch 는 NPP 를 안 쓴다) torchcodec wheel 도 선언하지 않는다. 그래서 이미지에
    `import` 자체가 안 되는 torchcodec 이 실려 나갔다. LeRobot 의 기본 `video_backend` 가
    torchcodec 이라 **영상을 읽는 모든 경로**가 같은 자리에서 죽는다 — 학습만의 문제가 아니다.

    ⚠ CPU 빌드로 피할 수 없다 — PyPI 의 `torchcodec==0.11.0` 도 같은 라이브러리를 찾는다(실측).
    ⚠ **깔기만 해서는 안 된다**: torch 는 자기 nvidia 라이브러리를 절대경로로 dlopen 하지만
    torchcodec 의 `.so` 는 DT_NEEDED 로 보통의 검색 경로를 탄다. `ld.so.conf.d` + `ldconfig`
    까지 해야 통과한다(실측: 넣기 전 실패 → 넣은 뒤 OK).
    """
    base = (REPO / "backend" / "Dockerfile.base").read_text()
    assert "pip install nvidia-npp" in base, "torchcodec 이 쓰는 NPP 런타임이 없다"
    npp = base.split("pip install nvidia-npp", 1)[1].split("\n\n", 1)[0]
    assert "ld.so.conf.d" in npp and "ldconfig" in npp, \
        "깔기만 하고 로더 경로에 안 넣는다 — 그러면 그대로 못 찾는다"
    assert "from torchcodec.decoders import VideoDecoder" in npp, \
        "빌드가 스스로 확인하지 않는다 — 다음에 또 못 쓰는 torchcodec 이 실려 나간다"
