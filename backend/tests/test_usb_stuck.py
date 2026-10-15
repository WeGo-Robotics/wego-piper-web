"""USB 가 커널 안에서 멈춘 상태를 알아보고 **재부팅하라고 말한다** (piper_robot/usbstuck.py).

2026-10-14: `uvcvideo` 가 교착(`uvc_ctrl_del_event` ↔ `uvc_ctrl_status_event_work` ↔
`uvc_disconnect`)해 `usb_hub_wq` 까지 멈췄다. 화면은 `lsusb` "timed out" 만 보여 줬고,
사람이 보게 되는 길은 "USB 컨트롤러 복구" 였는데 — 그 요청은 같은 곳에서 멈춘다.

이 파일은 `/proc` 을 **가짜 트리**로 만들어 쓴다. 실제 장치도, 실제 `lsusb` 도 안 건드린다.
"""

import time

import pytest

from piper_robot import usbstuck as U


def _proc(tmp_path, tasks):
    """`{(pid, tid): (comm, state, wchan)}` → 가짜 /proc."""
    for (pid, tid), (comm, state, wchan) in tasks.items():
        d = tmp_path / str(pid) / "task" / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        # /proc/<pid>/stat: pid (comm) state ppid ...
        (d / "stat").write_text(f"{tid} ({comm}) {state} 1 0 0 0\n")
        (d / "wchan").write_text(wchan)
    return str(tmp_path)


# ── 읽기 ────────────────────────────────────────────────────────────────

def test_only_uninterruptible_sleepers_are_collected(tmp_path):
    root = _proc(tmp_path, {
        (10, 10): ("python3", "S", "futex_do_wait"),
        (11, 11): ("rsd", "D", "uvc_ctrl_del_event"),
        (12, 12): ("bash", "R", "0"),
    })
    got = U.scan_blocked(root)
    assert list(got) == [11]
    assert got[11] == {"pid": 11, "tid": 11, "comm": "rsd", "wchan": "uvc_ctrl_del_event"}


def test_a_comm_with_spaces_and_parentheses_does_not_break_the_state(tmp_path):
    """`comm` 은 괄호 안이고 공백·괄호를 품는다 — 마지막 `)` 로 가르지 않으면 상태를 잘못 읽는다."""
    root = _proc(tmp_path, {(20, 20): ("Web Content (x)", "D", "usb_start_wait_urb")})
    assert U.scan_blocked(root)[20]["comm"] == "Web Content (x)"


def test_a_thread_that_ended_while_reading_is_skipped(tmp_path):
    d = tmp_path / "30" / "task" / "30"
    d.mkdir(parents=True)                       # stat 파일이 없다
    assert U.scan_blocked(str(tmp_path)) == {}


# ── USB 경로인가 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("task", [
    {"comm": "python3", "wchan": "uvc_ctrl_del_event"},
    {"comm": "chrome", "wchan": "uvc_status_get"},
    {"comm": "python3", "wchan": "usb_start_wait_urb"},
    {"comm": "kworker/0:0+usb_hub_wq", "wchan": "0"},
    {"comm": "kworker/1:3", "wchan": "xhci_handle_command_timeout"},
])
def test_usb_and_uvc_waits_are_recognised(task):
    assert U.is_usb_path(task)


@pytest.mark.parametrize("task", [
    {"comm": "kworker/0:2+events", "wchan": "0"},
    {"comm": "jbd2/nvme0n1p2-8", "wchan": "jbd2_journal_commit_transaction"},
    {"comm": "python3", "wchan": "io_schedule"},
])
def test_ordinary_disk_waits_are_not_usb(task):
    assert not U.is_usb_path(task)


# ── 연속 관찰 ───────────────────────────────────────────────────────────

def test_one_sighting_is_only_a_suspicion(tmp_path):
    """정상 I/O 에서도 D 는 잠깐 생긴다 — 한 번 본 것으로 "멈췄다"고 하면 거짓 경보다."""
    root = _proc(tmp_path, {(11, 11): ("rsd", "D", "uvc_ctrl_del_event")})
    t = U.StuckTracker(lambda: U.scan_blocked(root))
    assert t.observe() == []
    assert [x["tid"] for x in t.observe()] == [11]


def test_a_wait_that_cleared_between_looks_is_not_reported(tmp_path):
    root = _proc(tmp_path, {(11, 11): ("rsd", "D", "uvc_ctrl_del_event")})
    t = U.StuckTracker(lambda: U.scan_blocked(root))
    t.observe()
    (tmp_path / "11" / "task" / "11" / "stat").write_text("11 (rsd) S 1 0 0 0\n")
    assert t.observe() == []


def test_unrelated_blocked_threads_never_count(tmp_path):
    root = _proc(tmp_path, {(40, 40): ("rsync", "D", "io_schedule")})
    t = U.StuckTracker(lambda: U.scan_blocked(root))
    t.observe()
    assert t.observe() == []


def test_the_real_incident_shape_is_recognised(tmp_path):
    """2026-10-14 에 실제로 본 모양 — 세 스레드가 서로의 mutex 를 물고 있다."""
    root = _proc(tmp_path, {
        (2006012, 2058260): ("python3", "D", "uvc_ctrl_del_event"),
        (2057144, 2057144): ("kworker/0:0+usb_hub_wq", "D", "0"),
        (2061000, 2061000): ("chrome", "D", "uvc_status_get"),
        (2019987, 2019987): ("kworker/0:2+events", "D", "0"),
    })
    got = U.stuck_now(gap_s=0, scan=lambda: U.scan_blocked(root), sleep=lambda s: None)
    assert {t["comm"] for t in got} == {"python3", "kworker/0:0+usb_hub_wq", "chrome"}


def test_the_advice_says_reboot_first_and_not_to_press_recover(tmp_path):
    rep = U.report([{"pid": 1, "tid": 2, "comm": "python3", "wchan": "uvc_ctrl_del_event"}])
    assert rep["kernel_stuck"] is True
    assert "재부팅" in rep["advice"]
    assert "복구는 누르지 마세요" in rep["advice"]
    assert "uvc_ctrl_del_event" in rep["advice"], "무엇이 멈췄는지 근거를 같이 보여야 한다"


def test_nothing_stuck_means_no_advice():
    rep = U.report([])
    assert rep == {"kernel_stuck": False, "stuck": [], "advice": None, "kernel_log": []}


# ── 진단·복구 경로 ──────────────────────────────────────────────────────

def test_diagnosis_does_not_run_lsusb_on_a_stuck_bus(monkeypatch):
    """멈춘 USB 에서 `lsusb` 는 타임아웃까지 걸리고, 누를 때마다 멈춘 프로세스가 하나씩 쌓인다."""
    from piper_robot import can

    stuck = [{"pid": 1, "tid": 2, "comm": "python3", "wchan": "uvc_ctrl_del_event"}]
    monkeypatch.setattr(U, "stuck_now", lambda *a, **k: stuck)
    monkeypatch.setattr(U, "kernel_hints", lambda *a, **k: [])
    monkeypatch.setattr(can, "_run_cmd", lambda *a, **k: pytest.fail("lsusb 를 돌렸다"))
    monkeypatch.setattr(can, "list_xhci_controllers", lambda: ["0000:00:0d.0"])

    info = can.get_usb_info()
    assert info["kernel_stuck"] is True and "재부팅" in info["advice"]
    assert "건너뜀" in info["flat"] and "건너뜀" in info["tree"]
    assert info["controllers"] == ["0000:00:0d.0"]


def test_diagnosis_runs_lsusb_when_the_bus_is_fine(monkeypatch):
    from piper_robot import can

    calls = []
    monkeypatch.setattr(U, "stuck_now", lambda *a, **k: [])
    monkeypatch.setattr(can, "_run_cmd", lambda cmd, *a, **k: calls.append(cmd) or (0, "out", ""))
    monkeypatch.setattr(can, "list_xhci_controllers", lambda: [])
    info = can.get_usb_info()
    assert calls == [["lsusb"], ["lsusb", "-t"]]
    assert info["kernel_stuck"] is False and info["flat"] == "out"


def test_recovery_is_refused_on_a_stuck_bus_without_touching_sysfs(monkeypatch):
    """⚠ 재바인딩 요청이 같은 mutex 에 걸려 D 상태로 들어가면 부른 robotd 의 RPC 스레드가 멈춘다.
    안전 데몬이다 — 시도하지 않고 재부팅하라고 말한다."""
    from piper_robot import can

    monkeypatch.setattr(U, "stuck_now", lambda *a, **k: [
        {"pid": 1, "tid": 2, "comm": "python3", "wchan": "uvc_ctrl_del_event"}])
    monkeypatch.setattr(can, "_sysfs_write",
                        lambda *a, **k: pytest.fail("멈춘 USB 에 unbind 를 썼다"))
    ok, msg, done = can.recover_usb_controllers(["0000:00:0d.0"])
    assert ok is False and done == [] and "재부팅" in msg


def test_a_command_that_times_out_is_not_waited_for():
    """`subprocess.run(timeout=)` 은 죽인 뒤 `wait()` 로 기다린다 — D 상태면 영영 안 돌아온다.
    그러면 robotd 의 RPC 스레드가 진단 버튼 하나로 멈춘다. 죽이고 바로 돌아온다."""
    from piper_robot import can

    t0 = time.monotonic()
    rc, out, err = can._run_cmd(["sleep", "30"], timeout=0.3)
    assert time.monotonic() - t0 < 5
    assert rc == -1 and "timed out" in err


def test_run_cmd_still_returns_output_and_accepts_input():
    from piper_robot import can

    assert can._run_cmd(["echo", "hi"]) == (0, "hi", "")
    rc, out, _ = can._run_cmd(["cat"], input_text="piped")
    assert (rc, out) == (0, "piped")
    rc, _, err = can._run_cmd(["/no/such/binary"])
    assert rc == -1 and err


def test_the_daemon_exposes_the_light_verb():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "daemons" / "robotd.py").read_text()
    assert '"usb_stuck"' in src.split("_METHODS = {", 1)[1].split("}", 1)[0]


# ── 상시 경보 ───────────────────────────────────────────────────────────

def test_the_watch_raises_a_reboot_alert_and_clears_it(monkeypatch):
    """다른 경보("영상이 끊겼습니다")는 이 상태의 **증상**이다 — 원인을 직접 말해야 한다."""
    from app.services import device_watch as dw
    from app.services import robot_manager as rm

    answer = {"rep": {"kernel_stuck": True, "advice": "USB 가 커널 안에서 멈췄습니다 — 재부팅하세요"}}
    monkeypatch.setattr(rm, "robotd_available", lambda: True)
    monkeypatch.setattr(rm, "get_usb_stuck", lambda: answer["rep"])

    w = dw.DeviceWatch()
    out = w._usb()
    assert [(a.kind, a.reason) for a in out] == [("usb", "kernel_stuck")]
    assert "재부팅" in out[0].text

    w._usb_checked = 0.0                       # 간격이 지났다고 치고
    answer["rep"] = {"kernel_stuck": False}
    assert w._usb() == [], "풀렸는데 경보가 남았다"


def test_the_watch_does_not_ask_the_daemon_on_every_tick(monkeypatch):
    """robotd 의 RPC 는 팔 제어와 같은 한 줄이다 — 매 틱(2초) 점유하지 않는다."""
    from app.services import device_watch as dw
    from app.services import robot_manager as rm

    calls = []
    monkeypatch.setattr(rm, "robotd_available", lambda: True)
    monkeypatch.setattr(rm, "get_usb_stuck", lambda: calls.append(1) or {"kernel_stuck": False})

    w = dw.DeviceWatch()
    for _ in range(5):
        w._usb()
    assert len(calls) == 1


def test_a_dead_robotd_is_not_asked(monkeypatch):
    from app.services import device_watch as dw
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm, "robotd_available", lambda: False)
    monkeypatch.setattr(rm, "get_usb_stuck", lambda: pytest.fail("죽은 데몬에 물었다"))
    assert dw.DeviceWatch()._usb() == []


# ── 화면 ────────────────────────────────────────────────────────────────

def test_the_modal_says_reboot_and_will_not_let_you_press_recover():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    page = (repo / "frontend/src/pages/RobotsPage.tsx").read_text()
    assert "info?.kernel_stuck" in page and "{info.advice}" in page, "모달이 재부팅을 안 말한다"
    assert "disabled={recovering || !!info?.kernel_stuck}" in page, (
        "멈춘 USB 에서 복구 버튼을 누르게 둔다")
    alerts = (repo / "frontend/src/components/DeviceAlerts.tsx").read_text()
    assert "kernel_stuck" in alerts
