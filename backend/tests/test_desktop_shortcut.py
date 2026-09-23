"""바탕화면 바로가기 — 설치가 만들고 제거가 지운다 (feature/field-deployment.md §5).

⚠ 실제 HOME 은 건드리지 않는다 — 전부 임시 HOME·XDG 디렉토리에서 돈다. gio 가 세션 버스를 못
찾으면 스크립트는 경고만 하고 계속 가야 한다(그게 SSH 설치의 정상 경로다).
⚠ 아이콘 스크립트는 **클릭 시점**에 돈다 — 브라우저 대신 `PIPER_BROWSER` 로 받은 URL 을 파일에
적는 가짜 열기 명령을 준다. 진짜 브라우저가 뜨면 안 된다.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
INSTALL = DEPLOY / "install-shortcuts.sh"
LAUNCHER = DEPLOY / "piper-studio.sh"
DOCTOR = DEPLOY / "piper-doctor.sh"
ICON_SRC = REPO / "frontend" / "public" / "favicon.svg"
SHIPPED = ("deploy/install-shortcuts.sh", "deploy/piper-studio.sh", "deploy/piper-doctor.sh",
           "frontend/public/favicon.svg")


def _home(tmp_path: Path, desktop: bool) -> dict:
    """임시 HOME. `current/` 에는 아이콘이 부를 두 스크립트를 apply.sh 가 하듯 복사해 둔다."""
    home = tmp_path / "home"; home.mkdir()
    src = tmp_path / "current"; src.mkdir()
    for s in (LAUNCHER, DOCTOR):
        (src / s.name).write_bytes(s.read_bytes()); (src / s.name).chmod(0o755)
    cfg = home / ".config"; cfg.mkdir()
    if desktop:
        (home / "Desktop").mkdir()
        (cfg / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Desktop"\n')
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(cfg),
           "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
           "PIPER_SRC": str(src), "PIPER_ICON": str(ICON_SRC), "PIPER_WORK": str(tmp_path / "deploy")}
    env.pop("PIPER_BROWSER", None)
    return env


def _opener(tmp_path: Path) -> tuple[Path, Path]:
    """URL 하나를 받아 파일에 적는 가짜 브라우저."""
    seen = tmp_path / "opened.txt"
    sh = tmp_path / "opener.sh"
    sh.write_text(f'#!/bin/bash\nprintf "%s" "$1" > "{seen}"\n'); sh.chmod(0o755)
    return sh, seen


def _run(script: Path, env: dict, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=timeout)


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ── 만들기 ───────────────────────────────────────────────────────────────────

def test_the_scripts_parse():
    for s in (INSTALL, LAUNCHER, DOCTOR):
        subprocess.run(["bash", "-n", str(s)], check=True)


def test_install_makes_the_menu_entries_and_the_icon(tmp_path):
    env = _home(tmp_path, desktop=False)
    r = _run(INSTALL, env)
    assert r.returncode == 0, r.stdout + r.stderr
    apps = Path(env["XDG_DATA_HOME"]) / "applications"
    icon = Path(env["XDG_DATA_HOME"]) / "icons" / "piper-studio.svg"
    for name in ("piper-studio", "piper-studio-doctor"):
        body = (apps / f"{name}.desktop").read_text()
        assert "[Desktop Entry]" in body and "Type=Application" in body
        assert env["PIPER_SRC"] in body, "Exec 가 current/ 를 안 가리킨다"
        assert "Name[ko]=" in body, "한글 이름이 없다"
        assert f"Icon={icon}" in body
    assert icon.read_bytes() == ICON_SRC.read_bytes(), "아이콘은 프론트 favicon **그대로**여야 한다 — 원본이 하나"


def test_headless_install_leaves_the_home_directory_alone(tmp_path):
    """⚠ `xdg-user-dir DESKTOP` 은 바탕화면이 없으면 $HOME 을 돌려준다 — 거기에 .desktop 을
    두면 홈에 파일이 굴러다닌다. SSH 만 쓰는 NUC 이 그 경우다."""
    env = _home(tmp_path, desktop=False)
    r = _run(INSTALL, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not list(Path(env["HOME"]).glob("*.desktop")), "바탕화면이 없으면 홈에 .desktop 을 흘리면 안 된다"
    assert "앱 메뉴 항목만" in r.stdout


def test_desktop_copies_are_executable(tmp_path):
    """GNOME 은 실행 비트 없는 .desktop 을 잠근다."""
    env = _home(tmp_path, desktop=True)
    r = _run(INSTALL, env)
    assert r.returncode == 0, r.stdout + r.stderr
    for name in ("piper-studio", "piper-studio-doctor"):
        f = Path(env["HOME"]) / "Desktop" / f"{name}.desktop"
        assert f.exists() and os.access(f, os.X_OK), f"{f} 가 없거나 실행 비트가 없다"


def test_install_twice_is_the_same_as_once(tmp_path):
    env = _home(tmp_path, desktop=True)
    assert _run(INSTALL, env).returncode == 0
    before = _files(Path(env["HOME"]))
    r = _run(INSTALL, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _files(Path(env["HOME"])) == before, "두 번째 실행이 다른 것을 만들었다"


def test_check_reports_without_creating(tmp_path):
    env = _home(tmp_path, desktop=True)
    r = _run(INSTALL, env, "--check")
    assert r.returncode == 0 and "없음" in r.stdout, r.stdout + r.stderr
    assert not (Path(env["XDG_DATA_HOME"]) / "applications").exists(), "--check 가 무언가를 만들었다"


def test_remove_deletes_everything_install_made(tmp_path):
    env = _home(tmp_path, desktop=True)
    assert _run(INSTALL, env).returncode == 0
    r = _run(INSTALL, env, "--remove")
    assert r.returncode == 0, r.stdout + r.stderr
    left = [str(p) for p in Path(env["HOME"]).rglob("piper-studio*")]
    assert not left, left


# ── 아이콘을 눌렀을 때 ───────────────────────────────────────────────────────

class _Quiet(BaseHTTPRequestHandler):
    def do_GET(self):        # noqa: N802
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):  # noqa: D102
        pass


def test_the_launcher_opens_the_web_when_the_front_answers(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), _Quiet)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = _home(tmp_path, desktop=False)
        (Path(env["PIPER_SRC"]) / ".env").write_text(f"PIPER_WEB_PORT={port}\n")
        opener, seen = _opener(tmp_path); env["PIPER_BROWSER"] = str(opener)
        r = _run(Path(env["PIPER_SRC"]) / "piper-studio.sh", env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert seen.read_text() == f"http://localhost:{port}/", "포트를 .env 에서 못 읽었거나 주소가 틀리다"
    finally:
        srv.shutdown()


def test_the_launcher_opens_the_doctor_when_nothing_answers(tmp_path):
    """⚠ "연결할 수 없음" 흰 화면 대신 보고서 — 프론트 컨테이너가 안 뜬 층은 웹이 말해 줄 수 없다."""
    env = _home(tmp_path, desktop=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]      # 닫히면 아무도 안 듣는다
    (Path(env["PIPER_SRC"]) / ".env").write_text(f"PIPER_WEB_PORT={port}\n")
    opener, seen = _opener(tmp_path); env["PIPER_BROWSER"] = str(opener)
    r = _run(Path(env["PIPER_SRC"]) / "piper-studio.sh", env, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    url = seen.read_text()
    assert url.startswith("file://") and url.endswith("/piper-web/doctor.html"), url
    body = Path(url[len("file://"):]).read_text()
    assert "Piper Studio 진단" in body and "<pre>" in body


def test_the_doctor_never_dies_on_a_bare_machine(tmp_path):
    """진단이 죽으면 아무것도 못 배운다 — 설치된 적 없는 기계에서도 끝까지 가서 0 으로 끝난다."""
    env = _home(tmp_path, desktop=False)
    r = _run(DOCTOR, env, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "설치된 적이 없다" in r.stdout and "[7] 접속 주소" in r.stdout, r.stdout


def test_the_doctor_html_escapes_what_it_quotes(tmp_path):
    env = _home(tmp_path, desktop=False)
    r = _run(DOCTOR, env, "--html", timeout=180)
    assert r.returncode == 0 and r.stdout.startswith("<!doctype html>"), r.stderr
    pre = r.stdout.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert not re.search(r"<(?!/?pre)", pre), "보고서 본문에 이스케이프 안 된 꺾쇠가 있다"


# ── 배선 — 번들에 실리고, 설치가 부르고, 제거가 지운다 ──────────────────────────

def test_the_bundle_ships_the_shortcuts_and_apply_installs_them():
    from conftest import code_only

    apply = code_only((DEPLOY / "apply.sh").read_text())
    assert 'bash "$SRC/install-shortcuts.sh"' in apply, "apply.sh 가 바로가기를 안 만든다"
    assert '"$HERE/install-shortcuts.sh" --check' in apply, "--check 가 바로가기를 안 본다"
    assert '[ ! -f "$HERE/install-shortcuts.sh" ]' in apply, "옛 번들(파일 없음)에서 죽는다"
    for path in ("stage-hostside.sh", "release.sh"):
        src = (DEPLOY / path).read_text()
        for f in SHIPPED:
            assert re.search(rf"^cp {re.escape(f)}\s", src, re.M), f"{path} 가 {f} 를 안 싣는다"
    uninstall = code_only((DEPLOY / "piper-uninstall.sh").read_text())
    for f in ("piper-studio.desktop", "piper-studio-doctor.desktop", "icons/piper-studio.svg"):
        assert f in uninstall, f"제거가 {f} 를 안 지운다"


def test_the_tab_title_is_the_product_name():
    """탭·북마크·바로가기 이름이 전부 `frontend` 였다."""
    assert "<title>Piper Studio</title>" in (REPO / "frontend" / "index.html").read_text()
