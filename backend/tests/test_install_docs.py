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


def test_every_chained_sudo_prescription_carries_sudo_on_each_part():
    """⚠ 실기(NUC, 2026-09-11): 스크립트가 찍은 `sudo mkdir -p /srv/piper-data && chown wego
    /srv/piper-data` 를 그대로 붙여 넣자 chown 이 "Operation not permitted" — 출력은 줄 앞에만
    `sudo` 를 붙이므로 `&&` 뒤는 일반 사용자로 돈다. 처방은 복사해 붙이면 끝나야 한다."""
    import re
    apply = (REPO / "deploy" / "apply.sh").read_text()
    for cmd in re.findall(r'NEED_SUDO\+=\("([^"]+)"\)', apply):
        for part in cmd.split("&&")[1:]:
            assert part.strip().startswith("sudo "), f"`&&` 뒤에 sudo 가 없다: {cmd}"


def test_the_install_ends_by_saying_where_to_open_the_browser():
    """설치가 끝나면 **어디로 가야 하는지** 아무도 안 알려 줬다(사용자 지적 2026-09-11).
    README 는 주소를 적고, 스크립트는 마지막 줄에 실제 IP·포트로 찍는다 — 포트는
    override 로 옮겨졌을 수 있으니(트러블슈팅 "frontend 가 포트를 못 잡는다") compose 에
    묻는다."""
    readme = (REPO / "README.md").read_text()
    assert "http://<설치한 기계의 IP>/" in readme and "hostname -I" in readme, "README 가 접속 주소를 안 적는다"
    apply = (REPO / "deploy" / "apply.sh").read_text()
    assert "docker compose port frontend 80" in apply, \
        "포트를 compose 에 안 묻는다 — override 로 옮기면 틀린 주소를 찍는다"
    assert 'say "접속"' in apply and 'echo "  브라우저에서 연다:  $url/"' in apply
    assert apply.rstrip().endswith('$url/"'), "접속 주소가 마지막 줄이 아니다 — 위에 묻힌다"


def test_a_non_default_web_port_is_one_env_var_and_survives_updates():
    """80 을 남이 쓰는 호스트(.120 의 WMS)에서 포트를 바꾸는 길이 override 파일 +
    `!override` 태그뿐이었다 — README 엔 없고 태그를 모르면 조용히 실패한다("80번이
    아닌 다른 포트를 쓰고 싶으면?", 2026-09-11). 환경변수 하나로 한다:
      · compose 는 **바깥** 포트만 보간한다(안쪽 :80 은 컨테이너 nginx)
      · apply.sh 가 배포 디렉토리 .env 에 **그 키 한 줄만** 적는다 — 사람이 둔
        PIPER_DATA_ROOT 등을 덮지 않고, 다음 업데이트에도 남는다
      · 마지막 접속 줄은 compose 에 못 물을 때 .env 의 값으로 폴백한다
    """
    compose = (REPO / "docker-compose.yml").read_text()
    assert '"${PIPER_WEB_PORT:-80}:80"' in compose, "바깥 포트가 변수가 아니다"
    assert '"80:80"' not in compose
    apply = (REPO / "deploy" / "apply.sh").read_text()
    assert 'env_put()  { touch "$SRC/.env"; sed -i "/^$1=/d" "$SRC/.env"; echo "$1=$2" >> "$SRC/.env"; }' in apply, \
        ".env 에 키만 갱신하지 않는다"
    assert '> "$SRC/.env"' not in apply.replace('>> "$SRC/.env"', ""), ".env 를 통째로 덮어쓴다"
    assert 'env_put PIPER_WEB_PORT "$PIPER_WEB_PORT"' in apply
    assert "web_port() {" in apply and 'port="${port:-$(web_port)}"' in apply, "접속 줄이 .env 값으로 폴백하지 않는다"
    readme = (REPO / "README.md").read_text()
    assert "PIPER_WEB_PORT=8081 ./piper-install.sh" in readme, "README 가 방법을 안 적는다"
    # 포트를 바꾸면 주소가 달라진다 — 그걸 안 적으면 :80 으로 열고 "안 뜬다"고 한다(사용자 지적 2026-09-11)
    assert "http://<설치한 기계의 IP>:8081/" in readme, "바뀐 포트의 주소를 안 적는다"
    assert "http://<이 기계의 IP>:8081/" in (REPO / "docs" / "qna.md").read_text()
    assert "PIPER_WEB_PORT=8081" in DOC.read_text(), "트러블슈팅이 옛 override 방식만 안다"
    # 설치 스크립트는 apply.sh 를 exec 하므로 환경변수가 그대로 넘어간다 — 그게 이 방식의 전제다
    assert 'exec "$DEST/apply.sh"' in (REPO / "deploy" / "piper-install.sh").read_text()


def test_a_host_without_a_gpu_gets_a_compose_combination_without_the_reservation():
    """⚠ 실기(NUC, 2026-09-11): compose 가 `deploy.resources.reservations.devices: nvidia` 를
    하드 요구하는데 apply.sh 는 "GPU 가 안 보인다"고 **경고만 하고 넘겼다** — 4절 `up` 이
    `could not select device driver "nvidia"` 로 죽는다. "설치 한 방"의 구멍.

    기본 compose 는 그대로(GPU 호스트·개발 무변경). nvidia 가 없으면 예약을 지우는 조각
    (`!reset`, compose 2.24+)을 배포 디렉토리 .env 의 COMPOSE_FILE 로 끼운다 — override
    파일은 손대지 않고 있으면 뒤에 붙인다(COMPOSE_FILE 을 쓰면 기본 탐색이 꺼진다). GPU
    호스트는 키를 지운다. 조각은 두 번들 경로(이미지·오프라인) 모두에 실린다.
    """
    piece = (REPO / "docker-compose.nogpu.yml").read_text()
    assert "deploy: !reset {}" in piece.split("backend:", 1)[1], "조각이 backend 의 GPU 예약을 안 지운다"
    apply = (REPO / "deploy" / "apply.sh").read_text()
    assert "HAVE_GPU=0" in apply and "HAVE_GPU=1" in apply
    sec = apply.split("3c. compose 조합", 1)[1]
    assert 'cf="docker-compose.yml:docker-compose.nogpu.yml"' in sec
    assert 'cf="$cf:docker-compose.override.yml"' in sec, "override 가 조합에서 빠진다"
    assert 'env_put COMPOSE_FILE "$cf"' in sec and "env_drop COMPOSE_FILE" in sec
    assert apply.index("override 보존") < apply.index("3c. compose 조합"), "override 복원보다 먼저 조합을 정한다"
    assert 'vge "$COMPOSE_VER" "2.24"' in apply, "compose 2.24 미만을 안 막는다 — !reset 이 조용히 무시된다"
    for f in ("stage-hostside.sh", "release.sh"):
        assert 'cp docker-compose.nogpu.yml "$OUT/"' in (REPO / "deploy" / f).read_text(), f"{f} 가 조각을 안 싣는다"
    assert "GPU 없이도 설치" in (REPO / "README.md").read_text()
    assert "## GPU 없는 기계에 설치하면?" in (REPO / "docs" / "qna.md").read_text()
    assert 'could not select device driver "nvidia"' in DOC.read_text()


def test_the_python_floor_is_3_10_everywhere_it_is_stated():
    """⚠ 실기(NUC, 2026-09-11, Ubuntu 22.04 = 파이썬 3.10): wheel 이 `>=3.11` 을 **선언**해
    pip 가 거절했다. 코드는 3.10 에서 7개 패키지의 모든 모듈이 import 되는 것을 `python:3.10`
    컨테이너로 실측했다 — 선언이 코드보다 엄했다. 하한은 3.10 이고, 그 숫자는 세 곳
    (pyproject 7개·apply.sh 의 검사·README 전제 표)이 같아야 한다 — 한 곳만 올리면 설치가
    다시 이유 없이 거절된다."""
    for p in ("bus", "shm", "robot", "cam", "rs", "so101", "sim"):
        assert 'requires-python = ">=3.10"' in (REPO / p / "pyproject.toml").read_text(), f"{p} 의 하한이 3.10 이 아니다"
    apply = (REPO / "deploy" / "apply.sh").read_text()
    assert "sys.version_info >= (3, 10)" in apply, "apply.sh 가 파이썬 하한을 안 본다"
    assert apply.index("sys.version_info >= (3, 10)") < apply.index('python3 -m venv --system-site-packages "$VENV"'), \
        "venv 를 만든 뒤에야 본다"
    assert "Python 3.10 이상" in (REPO / "README.md").read_text(), "README 전제 표에 파이썬이 없다"
    assert "requires a different Python" in DOC.read_text()


def test_the_uninstall_script_reverses_the_install_but_keeps_the_data():
    """설치가 만든 것(유닛·컨테이너·이미지·venv·배포 디렉토리)을 되돌린다(사용자 요청
    2026-09-11). 설치와 같은 규칙 둘 — 데이터는 `--purge-data` 를 명시해야 지우고, sudo 는
    직접 안 쓴다(명령을 찍어 준다). 이 호스트의 사정(override)은 지우기 **전에**
    ~/override.keep.yml 로 빼 두어 다시 깔면 apply.sh 가 되돌린다. 번들 두 경로에 실린다."""
    import os
    import re
    from conftest import code_only

    p = REPO / "deploy" / "piper-uninstall.sh"
    assert p.exists() and os.access(p, os.X_OK), "제거 스크립트가 없거나 실행 권한이 없다"
    code = code_only(p.read_text())
    assert "set -euo pipefail" in code
    for line in code.splitlines():
        s = line.strip()
        if s.startswith("echo"):
            continue
        assert not re.match(r"^sudo\s", s), f"sudo 를 직접 실행한다: {s}"
    # 자리는 apply.sh 와 같아야 한다 — 어긋나면 지우는 척만 한다
    apply = (REPO / "deploy" / "apply.sh").read_text()
    for var in ('VENV="$HOME/.venvs/piper-daemons"', 'DATA="${PIPER_DATA_ROOT:-/srv/piper-data}"'):
        assert var in code and var in apply, f"{var} 가 두 스크립트에서 다르다"
    assert 'SRC="$WORK/current"' in code and 'SRC="$HOME/piper-web-deploy/current"' in apply
    # 데이터는 명시할 때만, override 는 지우기 전에
    assert "--purge-data" in code and code.index("if [ $PURGE = 1 ]") < code.index('rm -rf "$d"')
    assert code.index("override.keep.yml") < code.index('rm -rf "$WORK"'), "override 를 빼 두기 전에 배포 디렉토리를 지운다"
    # 순서: 컨테이너 내리기 → 유닛 → 이미지
    assert code.index("docker compose down") < code.index("disable --now") < code.index("docker image rm")
    assert "systemctl --user daemon-reload" in code and "--dry-run" in code
    for f in ("stage-hostside.sh", "release.sh"):
        assert 'cp deploy/piper-uninstall.sh "$OUT/"' in (REPO / "deploy" / f).read_text(), f"{f} 가 제거 스크립트를 안 싣는다"
    assert "piper-uninstall.sh" in (REPO / "README.md").read_text(), "README 에 제거 절이 없다"
    assert "## 지우고 싶으면?" in (REPO / "docs" / "qna.md").read_text()


def test_the_readme_admits_the_first_install_takes_two_runs_and_a_relogin():
    """"원터치"라 했지만 처음 설치는 그렇지 않다 — 스크립트가 sudo 명령을 찍고 멈추고,
    그룹(docker·video·dialout)은 **다시 로그인**해야 반영되며, 드라이버를 올렸으면 재부팅이다
    (NUC, 2026-09-11 — 사용자가 짚었다). 설치 절이 그걸 숨기면 두 번째 실행에서 헤맨다.
    그리고 README 는 짧아야 한다 — 설치와 무관한 절(추론 로그·CAN 규칙 상세)은 docs 로."""
    body = (REPO / "README.md").read_text()
    section = body.split("## 설치", 1)[1].split("\n## ", 1)[0]
    # ⚠ 문구는 2026-09-21 README 개편에서 바뀌었다 — 지키는 것은 **사실**이다:
    #   스크립트가 멈추니 다시 실행해야 하고, 그룹은 다시 로그인, 드라이버는 재부팅.
    for phrase in ("다시 실행", "다시 로그인", "재부팅"):
        assert phrase in section, f"설치 절이 말하지 않는다: {phrase}"
    assert "아직 올라가지 않았다" not in body, "낡은 경고가 남았다 — 이미지도 master 도 올라가 있다"
    assert "docs/inference-logs.md" in body and (REPO / "docs" / "inference-logs.md").exists()
    assert "## 9. 팔을 쓰려면" in DOC.read_text() and "list-can-adapters.py --write-rule" not in body, \
        "CAN 규칙 상세가 README 에 남았다"
    # ⚠ **상한이 115 → 320 으로 올랐다 (2026-09-21).** 115 는 "설치하는 사람에게 필요한
    #   것만" 이던 시절의 값이다. README 의 독자가 **밖에서 판단하는 사람**으로 바뀌면서
    #   무엇을 푸는 물건인지·한 바퀴가 어떻게 도는지·왜 안전한지가 앞에 왔고, 설치는 그
    #   뒤에 그대로 남았다. 줄 수가 는 것은 그 결정의 결과이지 방치가 아니다.
    #   상한 자체는 남긴다 — 없으면 아무도 안 보는 사이 다시 부푼다.
    assert len(body.splitlines()) <= 320, f"README 가 다시 길어졌다: {len(body.splitlines())}줄"
    for n in ("robot", "camera", "collect", "graph", "study", "inference", "sim"):
        assert f"docs/images/{n}.jpg" in body and (REPO / "docs" / "images" / f"{n}.jpg").exists(), \
            f"스크린샷 {n}.jpg 가 README 에 없거나 파일이 없다"
        assert (REPO / "docs" / "images" / f"{n}.jpg").stat().st_size < 150_000, f"{n}.jpg 가 안 줄었다"


def test_the_release_rules_name_tests_that_actually_exist():
    """⚠ 릴리스 규칙 표(`deploy/RELEASE-CHECKLIST.md`)는 규칙마다 **그걸 지키는 테스트**를
    단다 — 문서에만 적힌 규칙은 다음 사람이 같은 자리에서 또 빠져나가기 때문이다
    (2026-09-15 하루에 같은 부류로 세 번: piper_sdk·piper_cam·데몬 소스).

    그 인용이 낡으면 표가 **조용히 거짓**이 된다: 이름이 바뀐 테스트를 가리키는데 읽는
    사람은 "지켜지고 있다" 고 믿는다. 규칙을 지우려면 테스트도 같이 지워야 한다."""
    import re

    doc = (REPO / "deploy" / "RELEASE-CHECKLIST.md").read_text()
    assert "## 릴리스 규칙" in doc, "릴리스 규칙 절이 사라졌다"
    rules = doc.split("## 릴리스 규칙", 1)[1].split("\n## 원터치", 1)[0]
    cited = set(re.findall(r"`(test_\w+)`", rules))
    assert len(cited) >= 8, f"규칙 표가 테스트를 거의 안 건다: {len(cited)}개"
    have = "".join(p.read_text() for p in (REPO / "backend" / "tests").glob("test_*.py"))
    missing = sorted(t for t in cited if f"def {t}(" not in have)
    assert not missing, f"규칙이 없는 테스트를 가리킨다: {missing}"


def test_the_install_grants_robotd_the_can_commands_and_nothing_more():
    """⚠ 실기(2026-09-15): 다른 기계에서 로봇 등록 [UP] 이 `set bitrate failed: sudo: a password is
    required`. robotd 는 사용자 유닛이라 CAP_NET_ADMIN 이 없고 `ip link set can…` 을 sudo 로
    부르는데, 개발 머신엔 있던 비밀번호 없는 허용이 그 기계엔 없었다 — 설치가 아무 말도 안 했다.

    잠그는 것: (1) 번들에 sudoers 템플릿이 실리고 (2) 허용은 `modprobe gs_usb` 와 `ip link set can*`
    뿐이며(`ALL` 금지) (3) apply.sh 0절이 `sudo -l` 로 실행 없이 검사해 처방을 찍고 (4) 처방은
    `visudo -cf` 로 검증한 뒤 0440 으로 놓고 (5) 제거 스크립트가 지우라고 말하고 (6) robotd 의
    오류 문구가 처방을 가리킨다."""
    from conftest import code_only
    tpl = (REPO / "deploy" / "sudoers" / "piper-can.in").read_text()
    rules = [l for l in tpl.splitlines() if l.strip() and not l.startswith("#")]
    body = " ".join(rules)
    assert "@USER@ ALL=(root) NOPASSWD: PIPER_CAN" in body, "허용 줄이 다르다"
    assert "NOPASSWD: ALL" not in body and ": ALL" not in body, "전부 허용은 안 된다"
    for cmd in ("modprobe gs_usb", "ip link set can* down", "ip link set can* up", "ip link set can* type can bitrate [0-9]*"):
        assert cmd in body, f"허용 목록에 {cmd} 가 없다"
    assert "/usr/sbin/ip" in body and "/usr/bin/ip" in body, "ip 경로가 배포판마다 달라 둘 다 적어야 한다"
    stage = (REPO / "deploy" / "stage-hostside.sh").read_text()
    assert 'cp deploy/sudoers/piper-can.in "$OUT/sudoers/"' in stage, "번들에 템플릿이 안 실린다"
    apply = code_only((REPO / "deploy" / "apply.sh").read_text())
    assert "sudo -n -l /usr/sbin/ip link set can0 type can bitrate 1000000" in apply, "실행 없이 허용 여부를 검사하지 않는다"
    assert 'sed "s/@USER@/$USER/g" "$HERE/sudoers/piper-can.in" > "$HERE/sudoers/piper-can"' in apply
    assert 'NEED_SUDO+=("visudo -cf $HERE/sudoers/piper-can && sudo install -m 0440 -o root -g root $HERE/sudoers/piper-can /etc/sudoers.d/piper-can")' in apply, \
        "처방이 visudo 검증 없이 놓거나 권한이 0440 이 아니다"
    assert "sudo rm -f /etc/sudoers.d/piper-can" in (REPO / "deploy" / "piper-uninstall.sh").read_text()
    can = (REPO / "robot" / "piper_robot" / "can.py").read_text()
    assert "password is required" in can and "sudoers.d/piper-can" in can, "robotd 오류 문구가 처방을 안 가리킨다"
    assert "/etc/sudoers.d/piper-can" in DOC.read_text()


def test_the_qna_doc_keeps_the_questions_as_asked_and_agrees_with_the_scripts():
    """질문이 **나온 말 그대로** 남는 곳(사용자: "QnA 문서에 남겨두자", 2026-09-11). 답이
    스크립트와 어긋나면 안 된다 — 접속 주소, 포트 바꾸기, 설치 뒤 바꾸기(frontend 만 다시
    만든다·녹화·추론 중엔 금지)가 있어야 하고, README 가 가리켜야 찾는다."""
    qna = REPO / "docs" / "qna.md"
    assert qna.exists(), "QnA 문서가 없다"
    text = qna.read_text()
    assert "docs/qna.md" in (REPO / "README.md").read_text(), "README 가 QnA 를 안 가리킨다"
    for phrase in ("http://<이 기계의 IP>/", "PIPER_WEB_PORT=8081 ./piper-install.sh",
                   "sed -i '/^PIPER_WEB_PORT=/d' .env", "docker compose up -d",
                   "frontend 컨테이너만", "녹화·추론 중엔", "heartbeat", "!override"):
        assert phrase in text, phrase
    # 답이 가리키는 배포 디렉토리는 apply.sh 의 SRC 그대로여야 한다
    assert 'SRC="$HOME/piper-web-deploy/current"' in (REPO / "deploy" / "apply.sh").read_text()
    assert "~/piper-web-deploy/current" in text
