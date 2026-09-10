"""시스템 로그 — 데몬 저널을 SSH 없이 (설정 → 로그).

컨테이너 게이트웨이는 호스트 저널을 못 읽어 unitd 가 읽어 준다. 데몬은 stdout 이라
journald 우선순위가 전부 info — 레벨은 메시지 토큰으로 가른다. 기본은 경고 이상.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _unitd():
    spec = importlib.util.spec_from_file_location("unitd", REPO / "daemons" / "unitd.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_levels_come_from_the_message_because_stdout_is_always_info():
    u = _unitd()
    assert u.infer_level("2026-09-09 17:14:37,670 [WARNING] so101d: 캘리브레이션 파일이 없습니다", 6) == "warning"
    assert u.infer_level("2026-09-09 17:14:37,670 [ERROR] robotd: E-stop 토크 차단 실패", 6) == "error"
    assert u.infer_level("INFO:     127.0.0.1:52878 - \"GET /api/robots/relay/status HTTP/1.1\" 200 OK", 6) == "info"
    assert u.infer_level("ERROR:    Exception in ASGI application", 6) == "error"
    assert u.infer_level("piper-update.service: Failed with result 'exit-code'.", 6) == "error"
    assert u.infer_level("Traceback (most recent call last):", 6) == "error"
    assert u.infer_level("Started piper-robotd.service - Piper robot daemon", 6) == "info"
    # 트레이스백 프레임·예외 줄은 첫 줄 없이 와도 오류다 — journald 가 -g 로 골라 보낼 때 그렇다
    assert u.infer_level('  File "/x/hub.py", line 1, in f', 6) == "error"
    assert u.infer_level("RuntimeError: 모터 무응답", 6) == "error"
    assert u.infer_level("piper_shm.arm.ArmSegmentError: 팔 세그먼트가 없습니다", 6) == "error"
    assert u.infer_level("some kernel-ish line", 3) == "error" and u.infer_level("x", 4) == "warning"


def test_journal_json_becomes_rows_and_bad_lines_are_skipped():
    u = _unitd()
    text = "\n".join([
        json.dumps({"__REALTIME_TIMESTAMP": "1789005640940568", "PRIORITY": "6",
                    "_SYSTEMD_USER_UNIT": "piper-robotd.service", "MESSAGE": "2026-09-10 10:00:00,000 [WARNING] robotd: x"}),
        "not json",
        json.dumps({"__REALTIME_TIMESTAMP": "1789005640950000", "PRIORITY": "6",
                    "_SYSTEMD_USER_UNIT": "piper-so101d.service", "MESSAGE": [72, 105]}),   # 바이트 배열
    ])
    rows = u.parse_journal_lines(text)
    assert [r["unit"] for r in rows] == ["robotd", "so101d"]
    assert rows[0]["level"] == "warning" and abs(rows[0]["t"] - 1789005640.94) < 0.01
    assert rows[1]["msg"] == "Hi"


def test_only_our_units_can_be_read_and_all_is_a_glob():
    u = _unitd()
    assert "piper-robotd.service" in u.log_units("all") and "piper-update.service" in u.log_units("all") and "piper-*" not in u.log_units("all")
    assert u.log_units("robotd") == ["piper-robotd.service"] == u.log_units("piper-robotd.service")
    assert u.log_units("gateway") == ["piper-gateway.service"] and u.log_units("update") == ["piper-update.service"]
    for bad in ("sshd", "../x", "nginx", ""):
        with pytest.raises(ValueError):
            u.log_units(bad)
    with pytest.raises(ValueError, match="시각"):
        u.fetch_logs("robotd", 10, "info", "rm -rf /")
    with pytest.raises(ValueError, match="레벨"):
        u.fetch_logs("robotd", 10, "loud")
    assert '"logs"' in u.__dict__["_METHODS"].__repr__() or "logs" in u._METHODS


def test_filtering_for_errors_reads_deeper_than_the_lines_it_returns(monkeypatch):
    """오류 30줄을 보려면 info 수천 줄을 훑어야 한다 — 거를수록 더 읽고, 돌려주는 건 요청한 줄 수."""
    u = _unitd()
    seen = {}
    ALL = [json.dumps({"__REALTIME_TIMESTAMP": str(1789005640000000 + i), "PRIORITY": "6",
                       "_SYSTEMD_USER_UNIT": "piper-robotd.service",
                       "MESSAGE": f"[{'ERROR' if i % 10 == 0 else 'INFO'}] robotd: line {i}"}) for i in range(600)]
    import io, re
    def run(cmd, **k):
        class R:
            returncode = 0; stderr = ""; stdout = "LoadState=loaded"
        return R()
    class FakeProc:                                   # journalctl -r -n N [-g PAT] — 최신부터 N 줄
        def __init__(self, cmd, **k):
            n = int(cmd[cmd.index("-n") + 1]); seen["n"] = n
            seen["g"] = cmd[cmd.index("-g") + 1] if "-g" in cmd else None
            assert "-r" in cmd, "최신부터 읽어야 예산에 걸려도 최신 오류가 남는다"
            pool = [l for l in ALL if not seen["g"] or re.search(seen["g"], json.loads(l)["MESSAGE"])]
            self.stdout = io.StringIO("\n".join(reversed(pool[-n:])) + "\n")
        def poll(self): return 0
        def kill(self): pass
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(u.subprocess, "run", run)
    monkeypatch.setattr(u.subprocess, "Popen", FakeProc)
    import select as _select
    monkeypatch.setattr(u, "select", type("S", (), {"select": staticmethod(lambda r, w, x, t: (r, [], []))})(), raising=False)
    out = u.fetch_logs("robotd", 5, "error")
    assert seen["n"] == 50 and seen["g"] == u.LOG_GREP_ERROR, "거를 때는 journald 가 -g 로 전 구간을 거른다 — 접근 로그가 창을 채운다"
    assert len(out["entries"]) == 5 and out["truncated"] and all(e["level"] == "error" for e in out["entries"])
    out = u.fetch_logs("robotd", 50, "info")
    assert seen["n"] == 50 and seen["g"] is None and len(out["entries"]) == 50 and not out["truncated"]
    assert out["partial"] is False and out["entries"][0]["msg"].endswith("line 550"), "시간순(오래된 것부터)으로 돌려준다"
    import re
    for line in ("  File \"/x/hub.py\", line 1, in f", "RuntimeError: 모터 무응답", "piper-update.service: Failed with result 'exit-code'."):
        assert re.search(u.LOG_GREP_ERROR, line), line
    assert not re.search(u.LOG_GREP_ERROR, "INFO:     127.0.0.1 - \"GET /api/x\" 200 OK")


def test_the_gateway_route_and_the_panel_exist_with_a_warning_default():
    router = (REPO / "backend" / "app" / "routers" / "system.py").read_text()
    body = router.split('@router.get("/logs")', 1)[1].split("\n@router", 1)[0]
    assert "syslog.fetch" in body and 'format == "text"' in body and "attachment" in body
    svc = (REPO / "backend" / "app" / "services" / "syslog.py").read_text()
    assert 'rpc_call(C.UNITD, "logs"' in svc and "from daemons.unitd import fetch_logs" in svc
    panel = (REPO / "frontend" / "src" / "components" / "SystemLogPanel.tsx").read_text()
    assert "useState<'error' | 'warning' | 'info'>('warning')" in panel, "기본은 경고 이상"
    for needle in ("/system/logs?", "format: 'text'", "따라가기", "오류만", "전체 piper-*"):
        assert needle in panel, needle
    page = (REPO / "frontend" / "src" / "pages" / "SettingsPage.tsx").read_text()
    assert "{ id: 'logs', label: '로그' }" in page and "<SystemLogPanel initialUnit={logUnit} />" in page
    services = (REPO / "frontend" / "src" / "components" / "ServicesPanel.tsx").read_text()
    assert "onShowLog" in services and "이 데몬의 저널" in services


def test_a_traceback_is_one_error_not_one_error_line_and_many_info_lines():
    """"오류만" 에서 `Traceback` 한 줄만 남고 프레임·예외 줄이 빠지면 무엇이 터졌는지 못 본다."""
    u = _unitd()
    mk = lambda i, m: json.dumps({"__REALTIME_TIMESTAMP": str(1789005640000000 + i), "PRIORITY": "6",
                                  "_SYSTEMD_USER_UNIT": "piper-so101d.service", "MESSAGE": m})
    rows = u.parse_journal_lines("\n".join([
        mk(1, "2026-09-09 17:59:00,000 [INFO] so101d: 발행 중"),
        mk(2, "Traceback (most recent call last):"),
        mk(3, '  File "/x/hub.py", line 1, in _publish_loop'),
        mk(4, "RuntimeError: 모터 무응답"),
        mk(5, "2026-09-09 17:59:01,000 [WARNING] so101d: 릴레이 중단"),
        mk(6, "2026-09-09 17:59:02,000 [INFO] so101d: 다시"),
    ]))
    assert [r["level"] for r in rows] == ["info", "error", "error", "error", "warning", "info"]


def test_repeated_lines_are_counted_not_listed():
    """프론트 개발 서버가 게이트웨이 재시작 사이에 `ECONNREFUSED` 를 24번 찍어 "오류만"
    창을 통째로 채웠다(실측). 연달아 같은 줄은 하나로 세고, 마지막 시각을 남긴다."""
    u = _unitd()
    rows = [{"t": i, "unit": "frontend", "level": "error", "msg": "Error: connect ECONNREFUSED"} for i in range(24)]
    rows += [{"t": 30, "unit": "so101d", "level": "error", "msg": "RuntimeError: x"},
             {"t": 31, "unit": "frontend", "level": "error", "msg": "Error: connect ECONNREFUSED"}]
    c = u.collapse_repeats(rows)
    assert [(r["unit"], r["count"]) for r in c] == [("frontend", 24), ("so101d", 1), ("frontend", 1)]
    assert c[0]["t"] == 0 and c[0]["t_last"] == 23
