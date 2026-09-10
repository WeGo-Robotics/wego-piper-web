"""이미지 받기 진행률 (docs/install-troubleshooting.md, deploy/pull-progress.py).

`docker pull` 은 터미널이 아니면 막대를 안 그리고 apply.sh 는 `-q` 였다 — 첫 설치의
베이스 7GB 를 몇 분 말없이 기다리게 했다. 도커 API 스트림을 직접 합산한다.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "pull-progress.py"


def _mod():
    spec = importlib.util.spec_from_file_location("pull_progress", SCRIPT)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_layer_events_add_up_to_one_honest_line():
    """레이어별 바이트를 합산한다. 크기를 아직 모르는 레이어가 있으면 퍼센트를 지어내지
    않고 "전체 크기 확인 중"이라 말한다. 이미 있던 레이어는 받을 양에서 뺀다."""
    m = _mod()
    p = m.Progress("backend")
    p.feed({"status": "Pulling fs layer", "id": "a"})
    p.feed({"status": "Pulling fs layer", "id": "b"})
    p.feed({"status": "Already exists", "id": "c"})
    p.feed({"status": "Downloading", "id": "a", "progressDetail": {"current": 50, "total": 100}})
    assert "받음" in p.line() and "안 알려 줌" in p.line() and "%" not in p.line()
    p.feed({"status": "Downloading", "id": "b", "progressDetail": {"current": 0, "total": 300}})
    s = p.summary()
    assert s == {"layers": 2, "done": 0, "cur": 50, "known": 400, "unknown": 0, "ext": 0, "exists": 1}
    assert "(12%)" in p.line() and "0/2 레이어" in p.line()
    p.feed({"status": "Download complete", "id": "a"})
    p.feed({"status": "Pull complete", "id": "a"})
    assert p.summary()["done"] == 1 and p.summary()["cur"] == 100
    p.feed({"status": "Downloading", "id": "b", "progressDetail": {"current": 300, "total": 300}})
    p.feed({"status": "Pull complete", "id": "b"})
    assert "(100%)" in p.line() and "남은" not in p.line()
    assert "받음" in p.finish_line() and "이미 있던 레이어 1개" in p.finish_line()
    p.feed({"error": "manifest unknown"})
    assert p.error == "manifest unknown"


def test_an_image_that_is_already_here_says_so_and_a_port_is_not_a_tag():
    m = _mod()
    p = m.Progress("x")
    for lid in ("a", "b", "c"):
        p.feed({"status": "Already exists", "id": lid})
    assert p.finish_line() == "x: 이미 있음 (레이어 3개)"
    assert m.split_image("piper-build:5000/piper-web-backend:v0.4.6") == ("piper-build:5000/piper-web-backend", "v0.4.6")
    assert m.split_image("piper-build:5000/piper-web-backend") == ("piper-build:5000/piper-web-backend", "latest")
    assert m.split_image("alpine:3.20") == ("alpine", "3.20")
    assert m.fmt_bytes(3.48 * 1024 ** 3).endswith("GB") and m.fmt_secs(272) == "4분 32초"


def test_the_installer_carries_the_same_script_and_apply_no_longer_pulls_quietly():
    """piper-install.sh 는 사용자가 받는 유일한 파일이라 옆 파일에 기댈 수 없다 — 같은
    내용을 인라인한다. 둘이 어긋나면 한쪽만 고쳐진다. python3 이 없으면 docker pull 로."""
    inst = (REPO / "deploy" / "piper-install.sh").read_text()
    inl = inst.split("<<'PULL_PROGRESS_PY'\n", 1)[1].split("PULL_PROGRESS_PY\n", 1)[0]
    assert inl == SCRIPT.read_text(), "piper-install.sh 의 인라인 사본이 deploy/pull-progress.py 와 다르다"
    assert 'if command -v python3 >/dev/null; then' in inst and '    docker pull "$1"' in inst
    apply = (REPO / "deploy" / "apply.sh").read_text()
    assert "docker pull -q" not in apply
    assert 'python3 "$HERE/pull-progress.py"' in apply and 'else docker pull "$registry' in apply
    assert 'cp deploy/pull-progress.py "$OUT/"' in (REPO / "deploy" / "stage-hostside.sh").read_text()
    src = SCRIPT.read_text()
    assert 'os.execvp("docker", ["docker", "pull", ref])' in src, "소켓이 안 열리면 docker pull 로 물러나야 한다"
    assert "import requests" not in src and "import docker" not in src, "표준 라이브러리만"


def test_the_tag_line_is_not_a_layer_and_an_up_to_date_image_is_said_so():
    """도커 29 는 `Pulling from <repo>` 에 id 로 **태그**를 싣는다 — 레이어로 세면 "0/1
    레이어"가 영원히 남는다(실측). 이미 있는 이미지는 레이어 이벤트 없이 `Status:
    Image is up to date` 만 온다."""
    m = _mod()
    p = m.Progress("f")
    p.feed({"status": "Pulling from piper-web-frontend", "id": "v0.4.6"})
    p.feed({"status": "Digest: sha256:abc"})
    p.feed({"status": "Status: Image is up to date for x"})
    assert p.summary()["layers"] == 0 and p.finish_line() == "f: 이미 있음"
    q = m.Progress("a")
    q.feed({"status": "Pulling from library/alpine", "id": "3.19"})
    q.feed({"status": "Pulling fs layer", "id": "L1"})
    q.feed({"status": "Download complete", "id": "L1", "progressDetail": {"hidecounts": True}})
    q.feed({"status": "Extracting", "id": "L1", "progressDetail": {"current": 1_500_000, "units": "B"}})
    assert "풀기 1.4 MB" in q.line()
    q.feed({"status": "Pull complete", "id": "L1"})
    assert q.summary() == {"layers": 1, "done": 1, "cur": 0, "known": 0, "unknown": 1, "ext": 1_500_000, "exists": 0}
    assert q.finish_line().startswith("a: 받음 — 풀기 1.4 MB")
