"""설치 절차가 실제와 맞는가.

## ⚠ README 가 3년쯤 뒤처져 있었다

    cd backend && pip install -e ".[dev]"

이것만 적혀 있었는데, `backend/pyproject.toml` 은 로컬 패키지 **어느 것에도
의존하지 않는다.** 그대로 따르면 `piper_bus`·`piper_shm`·`piper_robot` 이
하나도 안 깔리고, Redis 도 `.env` 도 udev 도 나오지 않는다.

여기 테스트는 문서·스크립트·`pyproject` 셋이 서로 어긋나는 것을 잡는다.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy" / "install.sh"
README = REPO / "README.md"

#: 설치 스크립트가 순서대로 넣는 로컬 패키지
PKGS = ["bus", "shm", "robot", "cam", "rs", "phase", "act_aux",
        "vendor/wego_piper", "vendor/lerobot_robot_piper",
        "vendor/lerobot_robot_pipershm", "vendor/lerobot_camera_pipershm",
        "backend"]


def _toml(p: str) -> dict:
    return tomllib.loads((REPO / p / "pyproject.toml").read_text())


def test_every_local_package_is_in_the_installer():
    """새 패키지를 만들고 설치 목록에 안 넣으면 **새 머신에서만** 죽는다."""
    found = {str(p.parent.relative_to(REPO))
             for p in REPO.glob("*/pyproject.toml")}
    found |= {str(p.parent.relative_to(REPO))
              for p in REPO.glob("vendor/*/pyproject.toml")}
    found.discard("frontend")
    missing = found - set(PKGS)
    assert not missing, f"설치 목록에 없는 패키지: {sorted(missing)}"


def test_the_installer_lists_them_in_dependency_order():
    """⚠ `piper-robot` 은 `piper-shm`·`piper-bus` 를 쓴다. 순서가 틀리면
    editable 설치가 의존을 PyPI 에서 찾다 실패한다."""
    src = INSTALL.read_text()
    order = [p for p in PKGS if re.search(rf"\b{re.escape(p)}\b", src)]
    pos = {p: i for i, p in enumerate(order)}
    name_to_dir = {_toml(p)["project"]["name"].replace("_", "-"): p for p in PKGS}
    for p in PKGS:
        for dep in _toml(p)["project"].get("dependencies", []):
            d = re.split(r"[><=\[]", dep)[0].strip().replace("_", "-")
            if d in name_to_dir and name_to_dir[d] != p:
                assert pos[name_to_dir[d]] < pos[p], \
                    f"{p} 가 {name_to_dir[d]} 보다 먼저 설치된다"


def test_declared_dependencies_match_what_is_imported():
    """⚠ 실제로 있었던 구멍: `phase` 가 `piper_robot` 을 최상위에서 import 하는데
    선언에 없었다 — 깨끗한 환경에서 설치는 되고 import 에서 죽는다."""
    import ast

    for pkg, mod in (("phase", "piper_phase"), ("robot", "piper_robot"),
                     ("rs", "piper_rs"), ("cam", "piper_cam")):
        declared = {re.split(r"[><=\[]", d)[0].strip().replace("_", "-")
                    for d in _toml(pkg)["project"].get("dependencies", [])}
        for f in (REPO / pkg / mod).glob("*.py"):
            tree = ast.parse(f.read_text())
            for node in tree.body:          # **최상위만** — 가드된 import 는 선택적이다
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                elif isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                for m in mods:
                    if m.startswith("piper_") and m != mod:
                        assert m.replace("_", "-") in declared, \
                            f"{pkg} 가 {m} 을 최상위 import 하는데 선언에 없다 ({f.name})"


def test_the_readme_documents_exactly_one_install_path():
    """⚠ 예전 README 는 설치를 **세 가지**로 설명했다 — 저장소에서 직접, 도커
    수동, 번들 배포. 받는 사람은 어느 것이 자기 경우인지 모르고, 셋 중 하나만
    낡아도 그걸 따라 하다 막힌다. 지금은 **스크립트 하나**뿐이다.

    ⚠ 그리고 그 하나는 "backend 만 깔면 된다" 같은 **짧은 거짓말**이면 안 된다.
    스크립트가 전제 확인부터 데몬·컨테이너까지 실제로 다 해야 한다.
    """
    body = README.read_text()
    section = body.split("## 설치", 1)[1].split("\n## ", 1)[0]
    assert "piper-install.sh" in section, "설치 스크립트를 안 가리킨다"
    # 없어진 경로들을 다시 설명하기 시작하면 걸린다
    for gone in ("deploy/install.sh", "deploy/setup.sh", "docker compose up",
                 "pip install -e"):
        assert gone not in section, f"설치 절이 다른 경로도 설명한다: {gone}"
    assert "docs/INSTALL.md" not in body, "지운 문서를 가리킨다"


def test_the_readme_names_redis_and_the_env_file():
    """둘 다 없으면 아무것도 안 도는데 예전 README 에는 한 줄도 없었다."""
    src = README.read_text()
    assert "Redis" in src
    # ⚠ `deploy/env.example` 은 더 이상 README 가 말하지 않는다. 사용자는 그 파일을
    #   만질 일이 없고(`apply.sh` 가 놓는다), 배포자용 내용은 체크리스트로 갔다.
    assert "deploy/env.example" not in src, "사용자 문서에 배포자용 파일이 남았다"


def test_the_env_example_exists_and_carries_no_secret():
    """⚠ 실제 `.env` 에는 API 토큰이 있다 — 예시로 새어 나가면 안 된다."""
    ex = (REPO / "deploy" / "env.example").read_text()
    assert "PIPER_ROBOT_TRANSPORT=shm" in ex
    m = re.search(r"^PIPER_API_TOKEN=(.+)$", ex, re.M)
    assert m is None, f"예시에 토큰이 들어 있다: {m.group(1)[:8]}…"


def test_the_env_example_says_the_default_is_unsafe():
    """`direct` 가 코드 기본값이고, 그러면 안전층이 통째로 빠진다."""
    ex = (REPO / "deploy" / "env.example").read_text()
    assert "direct" in ex and "안전" in ex


def test_the_installer_never_runs_sudo():
    """⚠ 스크립트가 몰래 sudo 를 쓰면 무엇이 바뀌었는지 아무도 모른다 —
    명령을 **찍어 주고** 사람이 실행한다."""
    from conftest import code_only

    body = code_only(INSTALL.read_text())
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or '"' in stripped.split("sudo")[0]:
            continue
        assert not re.match(r"^\s*sudo\s", stripped), f"sudo 를 직접 실행한다: {stripped}"


# ── 도커 ────────────────────────────────────────────────────────────────────

COMPOSE = REPO / "docker-compose.yml"


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(COMPOSE.read_text())


def _docker_section() -> str:
    """⚠ 예전에는 `## Docker 배포` 절만 잘라 봤다. README 가 "스크립트 하나" 로
    줄면서 그 절이 없어졌다 — 이제 문서 전체가 그만큼 짧으므로 전체를 본다.
    지키려는 것은 "README 가 실제 구성과 어긋나지 않는다" 이지 절 이름이 아니다.
    """
    return README.read_text()


def test_the_readme_volume_table_matches_the_compose_file():
    """⚠ **실제로 어긋나 있었다.** README 표는 `~/.cache/huggingface` 등 네 개를
    적었는데, 컴포즈는 그것들을 **일부러 안 마운트한다**(호스트 절대경로가
    컨테이너로 새는 것을 막으려고). 그대로 따르면 없는 볼륨을 찾게 된다.
    """
    mounts = {v.split(":")[-1] for v in _compose()["services"]["backend"]["volumes"]}
    assert mounts == {"/data", "/run/redis"}, f"컴포즈 마운트가 바뀌었다: {mounts}"

    table = [ln for ln in _docker_section().splitlines() if ln.startswith("|")]
    named = " ".join(table)
    for gone in ("/root/.cache/huggingface", "/app/backend/data", "/app/backend/outputs"):
        assert gone not in named, f"README 가 없는 볼륨을 적는다: {gone}"


def test_the_readme_does_not_claim_host_network():
    """⚠ 컴포즈는 `network_mode: host` 를 뺐다 — 브리지 + 서비스 이름을 쓴다.
    README 마지막 줄이 아직 "(host network)" 라고 적고 있었다."""
    compose = COMPOSE.read_text()
    assert "network_mode: host" not in compose.replace("`network_mode: host`", "")
    assert "host network" not in _docker_section()


def test_the_readme_says_the_daemons_run_on_the_host():
    """⚠ 이게 빠지면 웹은 뜨는데 **카메라도 팔도 안 보인다.** 컨테이너는 장치를
    하나도 안 열기 때문이다."""
    sec = _docker_section()
    # ⚠ `install-daemons.sh` 는 내부 도구라 README 에서 뺐다. 지켜야 할 사실은
    #   **"하드웨어 데몬은 컨테이너가 아니라 호스트에서 돈다"** 는 것뿐이다 —
    #   이걸 모르면 "왜 컨테이너만 재시작해도 팔이 안 붙나"에서 막힌다.
    assert "호스트 systemd" in sec, "데몬이 어디서 도는지 안 적혀 있다"
    assert "robotd" in sec and "rsd" in sec


def test_the_readme_says_redis_needs_a_unix_socket():
    """기본 `redis.conf` 는 `unixsocket` 이 주석 처리돼 있다 — 켜지 않으면
    컨테이너가 버스에 못 붙는다."""
    sec = _docker_section()
    # ⚠ `unixsocket` 은 **redis.conf 의 설정 키**라 사용자가 직접 칠 일이 없다 —
    #   `apply.sh` 가 소켓이 없으면 그 sed 명령을 찍어 준다. README 가 지켜야 할
    #   사실은 "버스가 TCP 가 아니라 유닉스 소켓으로 간다" 는 것이다.
    assert "유닉스 소켓" in sec, "버스가 소켓으로 간다는 사실이 없다"
    url = _compose()["services"]["backend"]["environment"]
    assert any("unix:///run/redis" in e for e in url), "컴포즈가 소켓을 안 쓴다"


def test_the_container_opens_no_devices():
    """⚠ `privileged` 나 `/dev` 마운트가 돌아왔다면 **무언가가 아직 장치를
    직접 열고 있다는 뜻이다.** 그걸 찾아야지 권한을 되돌리면 안 된다."""
    be = _compose()["services"]["backend"]
    assert not be.get("privileged")
    assert not any(v.startswith("/dev/") for v in be["volumes"] if v != "/run/redis:/run/redis")
    assert be.get("ipc") == "host", "shm 세그먼트를 못 본다"


def test_the_transports_match_the_missing_privileges():
    """⚠ 권한을 뺀 것과 한 묶음이다. `direct` 면 컨테이너가 장치를 열려다 죽는다."""
    env = _compose()["services"]["backend"]["environment"]
    assert "PIPER_CAMERA_TRANSPORT=shm" in env
    assert "PIPER_ROBOT_TRANSPORT=shm" in env
