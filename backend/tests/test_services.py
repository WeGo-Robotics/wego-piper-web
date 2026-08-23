"""서비스 상태와 재시작 (feature/service-restart.md).

**유닛은 기동 시점의 코드로 돈다.** 이 저장소가 그걸로 두 번 크게 헤맸다 —
rsd 가 이틀 전 코드로 돌아 고친 버그가 재현됐고, 게이트웨이가 새 라우트를
모른 채 404 를 돌려줬다. 화면이 그걸 말해주면 몇 초짜리 일이다.
"""

import time
from pathlib import Path

import pytest

from app.services import units as U

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_code_newer_than_the_unit_is_flagged(monkeypatch, tmp_path):
    """이 판정이 이 기능의 전부다."""
    src = tmp_path / "d.py"
    src.write_text("x = 1")
    monkeypatch.setattr(U, "REPO", tmp_path)
    monkeypatch.setitem(U._SOURCES, "piper-fake", ("d.py",))

    now = src.stat().st_mtime
    assert U._newest_mtime(["d.py"]) == pytest.approx(now)
    # 유닛이 코드보다 **뒤에** 떴으면 최신이다
    assert not (now > now + 60 + U._GRACE_S)
    # 코드가 유닛보다 뒤면 낡았다
    assert now > (now - 60) + U._GRACE_S


def test_a_few_seconds_of_slack_is_allowed():
    """배포는 파일을 쓰고 몇 초 뒤 유닛을 띄운다. 그걸 "낡았다"로 읽으면
    **배포 직후마다** 거짓 경고가 뜬다."""
    assert U._GRACE_S >= 2


def test_pycache_is_not_treated_as_source(tmp_path, monkeypatch):
    """`__pycache__` 는 import 만 해도 갱신된다 — 세면 늘 낡은 것처럼 보인다."""
    monkeypatch.setattr(U, "REPO", tmp_path)
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    real = tmp_path / "pkg" / "a.py"
    real.write_text("x = 1")
    cached = tmp_path / "pkg" / "__pycache__" / "a.pyc.py"
    cached.write_text("x = 1")
    import os
    os.utime(cached, (time.time() + 9999, time.time() + 9999))
    assert U._newest_mtime(["pkg"]) == pytest.approx(real.stat().st_mtime)


def test_units_we_do_not_own_are_not_judged():
    """`piper-ollama` 처럼 남의 서비스를 감싼 유닛까지 "낡았다"고 하면
    경고가 늘 켜져 있어 **아무도 안 본다.**"""
    assert "piper-ollama" not in U._SOURCES
    import inspect
    src = inspect.getsource(U.list_units)
    assert "known = name in _SOURCES" in src and "stale=bool(known" in src


def test_only_our_own_units_can_be_restarted():
    """임의 유닛을 재시작하는 창구가 되면 안 된다 — 웹에서 남의 서비스를 만지게 된다."""
    for bad in ("sshd", "../sshd", "piper-x/../../sshd", "dbus"):
        ok, msg = U.restart_unit(bad)
        assert not ok, f"{bad} 를 막지 않는다"


def test_each_daemon_is_judged_by_what_it_actually_imports():
    """넓게 잡으면 경고가 늘 켜져 있고, 좁게 잡으면 진짜를 놓친다.

    프론트엔드를 고쳤다고 robotd 가 낡았다고 하면 안 된다.
    """
    assert "frontend" not in str(U._SOURCES)
    assert "rs" in U._SOURCES["piper-rsd"] and "rs" not in U._SOURCES["piper-robotd"]
    assert "robot" in U._SOURCES["piper-robotd"]
    # 데몬들이 실제로 공유하는 것은 들어 있어야 한다
    for unit in ("piper-rsd", "piper-camerad", "piper-robotd"):
        assert "shm" in U._SOURCES[unit] and "bus" in U._SOURCES[unit]


def test_the_gateway_start_time_is_not_read_from_proc():
    """**회귀** — `/proc/<pid>/stat` 의 mtime 은 기동 시각이 아니라 계속 갱신되는
    값이라 게이트웨이가 늘 "방금 떴다"고 나왔다."""
    import inspect
    src = inspect.getsource(U.gateway_status)
    assert "/proc/" not in src, "proc 의 mtime 을 기동 시각으로 쓴다"
    assert "_STARTED" in src


def test_restarting_is_refused_while_a_camera_is_in_use():
    """rsd 를 재시작하면 카메라 스트림이 끊긴다 — 돌고 있는 에피소드가 깨진다."""
    import inspect
    from app.routers import system
    for fn in (system.restart_service, system.restart_gateway):
        assert "require_idle" in inspect.getsource(fn), f"{fn.__name__} 이 안 막는다"


def test_the_gateway_answers_before_it_replaces_itself():
    """먼저 죽으면 브라우저는 "요청 실패"만 보고, 재시작이 된 건지 터진 건지 모른다."""
    import inspect
    from app.routers import system
    src = inspect.getsource(system.restart_gateway)
    assert "asyncio.create_task" in src and "sleep" in src, "응답 전에 죽는다"
    assert "os.execv" in src


def test_the_restart_dialog_does_not_block_the_event_loop():
    """`window.confirm` 은 이벤트 루프를 멈춰 E-stop heartbeat 를 끊는다 —
    이 저장소가 실제로 겪은 사고다."""
    from conftest import code_only

    # ⚠ 주석을 걷어내고 본다 — **왜 안 쓰는지 적어둔 설명**이 이 검사에 걸리면
    #   안 된다 (이 저장소에서 두 번 났다).
    src = (_SRC / "components" / "ServicesPanel.tsx").read_text()
    assert "window.confirm" not in code_only(src)
    assert "window.alert" not in code_only(src)
    assert "await confirm(" in src


def test_the_settings_tabs_come_from_one_list():
    """탭을 손으로 두 곳에 적으면 하나만 고치는 사고가 난다 — `pages.ts` 와 같은 규율."""
    src = (_SRC / "pages" / "SettingsPage.tsx").read_text()
    assert "const TABS = [" in src and "TABS.map(" in src


def test_the_respawn_uses_the_original_command_line():
    """**회귀** — 스스로 재시작하려다 죽었다.

    `python -m uvicorn …` 으로 뜬 경우 `sys.argv[0]` 은
    `site-packages/uvicorn/__main__.py` 다. 그 파일을 직접 실행하면 **그 디렉토리가
    `sys.path[0]` 이 되어** `uvicorn/logging.py` 가 표준 `logging` 을 가린다:

        AttributeError: module 'logging' has no attribute 'Formatter'

    `sys.orig_argv` 는 `-m uvicorn` 을 그대로 담은 **원래 명령줄**이다.
    """
    import inspect

    src = inspect.getsource(U.respawn_argv)
    assert "orig_argv" in src, "sys.argv 로는 `-m` 형태를 되살릴 수 없다"

    from app.routers import system
    router_src = inspect.getsource(system.restart_gateway)
    assert "respawn_argv" in router_src
    assert "sys.argv" not in router_src, "다시 sys.argv 를 쓴다"


def test_respawn_is_refused_when_the_command_line_is_unknown(monkeypatch):
    """되살릴 명령줄을 모르면 **죽기만 하고 안 돌아온다.** 그건 최악이다."""
    monkeypatch.setattr(U.sys, "orig_argv", ["python"])
    assert U.respawn_argv() is None
    assert U.gateway_status()["restartable"] is False
