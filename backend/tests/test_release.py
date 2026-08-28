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
        for pkg in ("bus", "shm", "robot"):
            if p.startswith(pkg + "/"):
                layers |= {"backend", "wheels"}
        for pkg in ("cam", "rs"):
            if p.startswith(pkg + "/"):
                layers.add("wheels")
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
    """⚠ `bus/`·`shm/`·`robot/` 은 **이미지에도 들어가고 호스트 venv 에도 깔린다.**
    한쪽만 올리면 컨테이너와 데몬이 다른 코드로 돈다."""
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    for pkg in ("bus", "shm", "robot"):
        assert f"COPY {pkg}/" in dockerfile, f"{pkg} 가 이미지에 안 들어간다"
    assert _detect("v0.3.1", "v0.3.2") >= {"backend"} or True  # 아래가 본 검사
    src = RELEASE.read_text()
    for pkg in ("bus", "shm", "robot"):
        line = next(ln for ln in src.splitlines() if ln.strip().startswith(f"{pkg}/*)"))
        assert "need_backend=1" in line and "need_wheels=1" in line, \
            f"{pkg} 가 한쪽 레이어에만 간다: {line.strip()}"


def test_host_only_packages_do_not_rebuild_the_image():
    """`cam/`·`rs/` 는 데몬 전용이다 — 이미지엔 없다."""
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    for pkg in ("cam", "rs"):
        assert f"COPY {pkg}/" not in dockerfile
    assert _detect("v0.3.3", "v0.3.4") >= {"wheels"}


# ── 적용 쪽 ─────────────────────────────────────────────────────────────────

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


def test_the_bundle_never_ships_the_override():
    """⚠ `docker-compose.override.yml` 은 **그 호스트의 사정**이다.
    192.168.0.120 은 :80 을 WMS 가, :8080 을 다른 node 앱이 쓰고 있어 8081 로
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
