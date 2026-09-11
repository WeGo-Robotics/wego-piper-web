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


def test_the_install_ends_by_saying_where_to_open_the_browser():
    """설치가 끝나면 **어디로 가야 하는지** 아무도 안 알려 줬다(사용자 지적 2026-09-11).
    README 는 주소를 적고, 스크립트는 마지막 줄에 실제 IP·포트로 찍는다 — 포트는
    override 로 옮겨졌을 수 있으니(트러블슈팅 "frontend 가 포트를 못 잡는다") compose 에
    묻는다."""
    readme = (REPO / "README.md").read_text()
    assert "http://<이 기계의 IP>/" in readme and "hostname -I" in readme, "README 가 접속 주소를 안 적는다"
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
    assert "sed -i '/^PIPER_WEB_PORT=/d' \"$SRC/.env\"" in apply \
        and 'echo "PIPER_WEB_PORT=$PIPER_WEB_PORT" >> "$SRC/.env"' in apply, ".env 에 키만 갱신하지 않는다"
    assert '> "$SRC/.env"' not in apply.replace('>> "$SRC/.env"', ""), ".env 를 통째로 덮어쓴다"
    assert "web_port() {" in apply and 'port="${port:-$(web_port)}"' in apply, "접속 줄이 .env 값으로 폴백하지 않는다"
    assert "PIPER_WEB_PORT=8081 ./piper-install.sh" in (REPO / "README.md").read_text(), "README 가 방법을 안 적는다"
    assert "PIPER_WEB_PORT=8081" in DOC.read_text(), "트러블슈팅이 옛 override 방식만 안다"
    # 설치 스크립트는 apply.sh 를 exec 하므로 환경변수가 그대로 넘어간다 — 그게 이 방식의 전제다
    assert 'exec "$DEST/apply.sh"' in (REPO / "deploy" / "piper-install.sh").read_text()


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
