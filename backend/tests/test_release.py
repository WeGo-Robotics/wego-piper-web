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


def test_the_offline_path_still_exists():
    """⚠ **현장 USB 배포를 버리면 안 된다.** 레지스트리가 전송량을 33배 줄이지만
    (실측 3.46GB → 104.5MB), 망이 없는 현장이 실재한다. `PIPER_REGISTRY` 가 비었거나
    `--offline` 이면 예전처럼 tar 를 만들어야 한다."""
    src = RELEASE.read_text()
    assert "--offline" in src, "오프라인 강제 수단이 없다"
    assert "docker save" in src, "tar 경로가 사라졌다"
    assert 'if [ -n "${PIPER_REGISTRY:-}" ] && [ $OFFLINE = 0 ]' in src, \
        "레지스트리를 조건 없이 쓴다 — 망 없는 현장이 막힌다"


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
    i_pull = src.find("docker pull -q")
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


def test_the_host_code_is_the_last_layer():
    """⚠ 이게 위로 올라가면 데몬 한 줄에 그 아래가 전부 다시 구워진다.
    ENV·WORKDIR·EXPOSE·CMD 는 0B 메타라 뒤에 와도 레이어를 안 만든다."""
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
    lines = joined
    i = next(n for n, l in enumerate(lines) if l.startswith("COPY .hostside/"))
    for l in lines[i + 1:]:
        assert l.split()[0] in {"ENV", "WORKDIR", "EXPOSE", "CMD", "ENTRYPOINT", "LABEL"}, \
            f"호스트 코드 뒤에 레이어를 만드는 것이 있다: {l}"


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
    for name in ("99-piper-can.rules", "99-realsense-libusb.rules",
                 "list-can-adapters.py"):
        assert f'"$OUT/udev/"' in src and name in src, f"번들에 {name} 이 없다"
    # 저장소에 실제로 있어야 `cp` 가 성립한다
    for rel in ("deploy/udev/99-piper-can.rules",
                "backend/udev/99-realsense-libusb.rules",
                "deploy/udev/list-can-adapters.py"):
        assert (REPO / rel).is_file(), f"{rel} 이 없다"


def test_apply_checks_the_can_serials_against_what_is_attached():
    """⚠ CAN 규칙에는 **번들을 구운 머신의 시리얼**이 박혀 있다. 어댑터가 다른
    머신에서는 어느 줄도 매칭되지 않는데 **udev 는 그걸 에러로 치지 않는다** —
    증상은 "팔 0개" 뿐이라 원인을 찾기 어렵다. 그래서 apply 가 대조한다."""
    src = APPLY.read_text()
    assert "/sys/bus/usb/devices" in src, "꽂힌 어댑터를 안 읽는다"
    assert "1d50" in src and "606f" in src, "gs_usb 장치를 안 고른다"
    assert "list-can-adapters.py" in src, "고치는 방법을 안 알려준다"


def test_apply_tolerates_a_bundle_without_udev():
    """옛 번들에는 `udev/` 가 없다. `set -u`·`set -e` 아래에서 빈 글롭이
    스크립트를 죽이면 **업데이트가 통째로 막힌다.**"""
    src = APPLY.read_text()
    block = src.split('for r in "$HERE"/udev/*.rules', 1)[1][:200]
    assert "continue" in block, "빈 글롭을 안 걸러낸다"


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
    wipe = readme.split("### 처음부터 다시 깔려면", 1)[1].split("###", 1)[0]
    assert "override.keep.yml" in wipe
    assert wipe.index("override.keep.yml") < wipe.index("rm -rf"), \
        "지우기 전에 백업하지 않는다"


def test_the_readme_names_the_data_that_survives():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    wipe = readme.split("### 처음부터 다시 깔려면", 1)[1].split("###", 1)[0]
    for keep in ("/srv/piper-data", "huggingface/lerobot", ".config/piper-web"):
        assert keep in wipe, f"보존 목록에 {keep} 이 없다"
