"""유휴 자동 종료 — 읽는 곳이 없는 카메라를 일정 시간 뒤 닫는다 (feature/stream-based-cameras.md §5-5).

이 기능이 틀릴 수 있는 방향은 둘이다. **안 닫히는 쪽**은 불편이고, **수집 도중 닫히는 쪽**은
데이터를 잃는다. 그래서 이 파일은 거의 전부가 "닫아선 안 될 때 안 닫는가"를 본다:

- 읽는 곳이 있으면(락) 안 닫는다 · 모르면 안 닫는다
- 연결 직후·시작 요청 직후(유예)에는 안 닫는다 · 하한(30초) 밑으로는 안 내려간다
- 상시 도는 감시자(관찰자)는 "읽는 곳"으로 안 잡힌다 — 안 그러면 영원히 안 닫힌다
- 읽는 프로세스가 죽으면 표지가 저절로 풀린다 — 하트비트 없이
"""

import subprocess
import sys
import textwrap
import time
import uuid

import numpy as np
import pytest

from piper_cam.idle import MIN_IDLE_S, IdleReaper, clear_stop, record_stop
from piper_shm import Publisher, Subscriber, has_readers, unlink


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _reaper(readers=lambda seg: False, exists=lambda seg: True):
    clock = _Clock()
    return IdleReaper(clock=clock, has_readers=readers, segment_exists=exists), clock


def _tick(r, clock, advance=0.0):
    clock.t += advance
    stopped_ids: list[str] = []
    out = r.tick(stopped_ids.append, lambda c: c, every_s=0.0)
    return out, stopped_ids


# ── 타이머 ──────────────────────────────────────────────────────────────

def test_it_closes_only_after_the_idle_time_has_passed():
    r, clock = _reaper()
    assert r.set_idle("cam", 60) == 60

    out, ids = _tick(r, clock, 59)
    assert out == [] and ids == [], "시간이 안 됐는데 닫았다"
    out, ids = _tick(r, clock, 2)
    assert ids == ["cam"] and out == [("cam", 60)]


def test_a_reader_resets_the_clock():
    busy = {"on": True}
    r, clock = _reaper(readers=lambda seg: busy["on"])
    r.set_idle("cam", 60)

    for _ in range(5):                           # 읽는 곳이 계속 있다 — 아무리 지나도
        _, ids = _tick(r, clock, 100)
        assert ids == []
    busy["on"] = False
    _, ids = _tick(r, clock, 59)                 # 마지막으로 보인 때부터 다시 센다
    assert ids == []
    _, ids = _tick(r, clock, 2)
    assert ids == ["cam"]


def test_the_floor_keeps_a_typo_from_closing_a_camera_right_after_connecting():
    r, clock = _reaper()
    assert r.set_idle("cam", 1) == MIN_IDLE_S == 30
    _, ids = _tick(r, clock, 29)
    assert ids == []


def test_zero_turns_it_off_and_a_never_set_camera_is_never_touched():
    r, clock = _reaper()
    r.set_idle("cam", 60)
    assert r.set_idle("cam", 0) == 0
    _, ids = _tick(r, clock, 10_000)
    assert ids == []
    _, ids = _tick(r, clock, 10_000)             # 설정한 적 없는 카메라는 애초에 없다
    assert ids == []


def test_a_hold_protects_the_camera_until_the_consumer_can_attach():
    """시작 요청 → 프로세스 기동 → 정책 로드 → 카메라 연결 사이에는 읽는 곳이 없다."""
    r, clock = _reaper()
    r.set_idle("cam", 60)
    assert r.hold("cam", 300) is True

    _, ids = _tick(r, clock, 299)
    assert ids == [], "소비자가 붙기 전에 닫았다"
    _, ids = _tick(r, clock, 70)                 # 유예가 끝나고 유휴 시간도 지났다
    assert ids == ["cam"]


def test_a_hold_on_a_camera_without_the_option_does_nothing():
    r, _ = _reaper()
    assert r.hold("cam", 300) is False


def test_reconnecting_restarts_the_grace_period():
    """연결할 때마다 `set_idle` 이 불린다 — 다시 연결한 직후에는 읽는 곳이 없어도 닫지 않는다."""
    r, clock = _reaper()
    r.set_idle("cam", 60)
    clock.t += 55
    r.set_idle("cam", 60)
    _, ids = _tick(r, clock, 30)
    assert ids == []


def test_a_camera_without_a_segment_is_skipped():
    """연결 안 된 카메라는 닫을 것도 없고, "읽는 곳이 없다"로 오판해 닫았다고 적으면 안 된다."""
    r, clock = _reaper(exists=lambda seg: False)
    r.set_idle("cam", 60)
    out, ids = _tick(r, clock, 10_000)
    assert out == [] and ids == []


def test_not_knowing_counts_as_somebody_reading():
    def boom(seg):
        raise OSError("flock 불가")

    r, clock = _reaper(readers=boom)
    r.set_idle("cam", 60)
    _, ids = _tick(r, clock, 10_000)
    assert ids == [], "모르는데 닫았다"


def test_a_failed_stop_is_not_recorded_and_is_retried():
    r, clock = _reaper()
    r.set_idle("cam", 60)
    clock.t += 100
    calls = []

    def fail(cam_id):
        calls.append(cam_id)
        raise RuntimeError("장치가 안 놓아 준다")

    assert r.tick(fail, lambda c: c, every_s=0.0) == []
    assert r.idle_seconds("cam") == 60, "실패했는데 타이머를 지웠다"
    clock.t += 1
    assert r.tick(fail, lambda c: c, every_s=0.0) == []
    assert len(calls) == 2


def test_it_forgets_a_camera_it_closed_until_the_next_connect_sets_it_again():
    r, clock = _reaper()
    r.set_idle("cam", 60)
    _tick(r, clock, 100)
    assert r.idle_seconds("cam") == 0


def test_ticks_are_rate_limited():
    """데몬 루프는 요청이 몰리면 빨리 돈다 — 매 반복마다 락을 떠보면 안 된다."""
    probes = []
    r, clock = _reaper(readers=lambda seg: probes.append(seg) or True)
    r.set_idle("cam", 60)

    r.tick(lambda c: None, lambda c: c, every_s=5.0)
    assert len(probes) == 1
    clock.t += 1
    r.tick(lambda c: None, lambda c: c, every_s=5.0)
    assert len(probes) == 1, "1초 만에 또 떠봤다"
    clock.t += 5
    r.tick(lambda c: None, lambda c: c, every_s=5.0)
    assert len(probes) == 2


# ── 표지 (flock) ────────────────────────────────────────────────────────

@pytest.fixture
def segment():
    name = f"pytest-idle-{uuid.uuid4().hex[:8]}"
    pub = Publisher(name, 4, 4)
    pub.publish(np.zeros((4, 4, 3), np.uint8))
    yield name
    pub.close()
    unlink(name)


def test_nobody_reads_until_a_consumer_opens(segment):
    assert has_readers(segment) is False
    sub = Subscriber(segment)
    try:
        assert sub.leased is True
        assert has_readers(segment) is True
    finally:
        sub.close()
    assert has_readers(segment) is False


def test_an_observer_is_not_counted_as_a_reader(segment):
    """상시 도는 감시·스냅샷이 "읽는 곳"으로 잡히면 영원히 안 닫힌다."""
    obs = Subscriber(segment, observer=True)
    try:
        assert obs.leased is False
        assert has_readers(segment) is False
        assert obs.read() is not None, "관찰자도 읽기는 된다"
    finally:
        obs.close()


def test_probing_does_not_block_a_consumer_that_opens_afterwards(segment):
    assert has_readers(segment) is False
    sub = Subscriber(segment)
    try:
        assert sub.leased is True
    finally:
        sub.close()


def test_a_reader_that_dies_releases_its_mark_without_a_heartbeat(segment):
    """커널이 죽은 프로세스의 락을 푼다 — 카운터였다면 영영 남았을 것이다."""
    code = textwrap.dedent(f"""
        import time
        from piper_shm import Subscriber
        s = Subscriber({segment!r})
        print("ready", flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        assert has_readers(segment) is True
    finally:
        p.kill()
        p.wait()
    for _ in range(50):
        if not has_readers(segment):
            break
        time.sleep(0.02)
    assert has_readers(segment) is False


def test_a_missing_segment_has_no_readers():
    assert has_readers(f"pytest-idle-none-{uuid.uuid4().hex[:6]}") is False


# ── 기록 ────────────────────────────────────────────────────────────────

class _Bus:
    class _R:
        def __init__(self):
            self.kv, self.ttl = {}, {}

        def set(self, k, v, ex=None):
            self.kv[k], self.ttl[k] = v, ex

        def delete(self, k):
            self.kv.pop(k, None)

    def __init__(self):
        self.r = self._R()


def test_a_stop_is_recorded_with_a_ttl_and_cleared_on_reconnect():
    import json

    from piper_bus import contract as C

    bus = _Bus()
    record_stop(bus, "rs:1:color", 300)
    key = C.cam_idle_key("rs:1:color")
    assert json.loads(bus.r.kv[key])["after_s"] == 300
    assert bus.r.ttl[key] == C.CAM_IDLE_TTL_S, "TTL 이 없으면 데몬이 죽었을 때 영원히 남는다"

    clear_stop(bus, "rs:1:color")
    assert key not in bus.r.kv


def test_recording_a_stop_never_stops_the_stopping():
    class _Dead:
        class r:
            @staticmethod
            def set(*a, **k):
                raise ConnectionError("redis 없음")

    record_stop(_Dead(), "cam", 60)           # 던지지 않는다


# ── 게이트웨이 ──────────────────────────────────────────────────────────

class _Hub:
    def __init__(self, knows=True):
        self.knows, self.calls = knows, []

    def set_stream(self, cam_id, name):
        return name

    def set_idle(self, cam_id, seconds):
        self.calls.append(("set_idle", seconds))
        return max(30, seconds) if (self.knows and seconds) else (0 if self.knows else None)

    def hold(self, cam_id, seconds):
        self.calls.append(("hold", seconds))
        return True

    def connect(self, *a, **k):
        self.calls.append(("connect",))
        return True, "OK"

    def info(self, cam_id):
        return {"connected": True, "want": [], "width": 640, "height": 480, "fps": 30}

    def has_frame(self, cam_id):
        return False


def _cam(monkeypatch, hub, **kw):
    from app.services.camera_manager import CameraInfo

    monkeypatch.setattr(CameraInfo, "_hub", property(lambda self: hub))
    return CameraInfo(id="/dev/video0", name="HD Webcam", stream="top", ready=True, **kw)


def test_connecting_tells_the_daemon_the_idle_time_before_the_stream_opens(monkeypatch):
    hub = _Hub()
    cam = _cam(monkeypatch, hub, idle_stop_s=300)
    assert cam.connect()[0] is True
    assert [c[0] for c in hub.calls] == ["set_idle", "connect"]
    assert hub.calls[0] == ("set_idle", 300)


def test_an_off_camera_that_was_never_pushed_costs_no_rpc(monkeypatch):
    """옛 데몬이 매 연결마다 "알 수 없는 메서드" 경고를 남기지 않게 한다."""
    hub = _Hub()
    cam = _cam(monkeypatch, hub)
    cam.connect()
    assert [c[0] for c in hub.calls] == ["connect"]


def test_turning_it_off_after_it_was_on_must_reach_the_daemon(monkeypatch):
    """안 알리면 데몬의 타이머가 남아 사람이 껐는데도 닫는다."""
    hub = _Hub()
    cam = _cam(monkeypatch, hub, idle_stop_s=300)
    cam.connect()
    hub.calls.clear()

    cam.idle_stop_s = 0
    cam.connect()
    assert ("set_idle", 0) in hub.calls


def test_a_daemon_that_does_not_know_the_verb_does_not_block_connecting(monkeypatch):
    hub = _Hub(knows=False)
    cam = _cam(monkeypatch, hub, idle_stop_s=300)
    assert cam.connect()[0] is True


def test_hold_is_only_sent_when_the_option_is_on(monkeypatch):
    hub = _Hub()
    on = _cam(monkeypatch, hub, idle_stop_s=60)
    assert on.hold(300) is True
    off = _cam(monkeypatch, hub)
    hub.calls.clear()
    assert off.hold(300) is False and hub.calls == []


def test_setting_validates_instead_of_fixing(monkeypatch):
    from app.services.camera_manager import CameraManager

    hub = _Hub()
    cam = _cam(monkeypatch, hub)
    m = CameraManager()
    m.cameras[cam.id] = cam

    assert m.set_idle_stop(cam.id, 0)[:2] == (True, 0)
    for bad in (1, 29, 24 * 3600 + 1, -5):
        with pytest.raises(ValueError):
            m.set_idle_stop(cam.id, bad)
    assert cam.idle_stop_s == 0, "거절됐는데 값이 바뀌었다"
    assert m.set_idle_stop(cam.id, 300)[:2] == (True, 300)
    assert m.set_idle_stop("/dev/nope", 300) == (False, 0, False)


def test_the_setting_is_pushed_at_once_only_while_connected(monkeypatch):
    from app.services.camera_manager import CameraManager

    hub = _Hub()
    cam = _cam(monkeypatch, hub)
    m = CameraManager()
    m.cameras[cam.id] = cam

    cam.connected = False
    assert m.set_idle_stop(cam.id, 300) == (True, 300, False)
    assert hub.calls == [], "연결 안 된 카메라에 RPC 를 날렸다 — 다음 연결이 알린다"

    cam.connected = True
    assert m.set_idle_stop(cam.id, 600) == (True, 600, True)
    assert hub.calls == [("set_idle", 600)]


def test_the_setting_survives_a_restart_and_a_bad_value_turns_it_off(tmp_path, monkeypatch):
    import json

    from app.services.camera_manager import CameraInfo, CameraManager

    path = tmp_path / "camera_session.json"
    monkeypatch.setattr(CameraManager, "CAMERA_SESSION_PATH", path)
    cam = CameraInfo(id="/dev/video0", name="HD Webcam", stream="top", ready=True, idle_stop_s=600)
    m = CameraManager()
    m.cameras[cam.id] = cam
    m.save_session()
    assert json.loads(path.read_text())[0]["idle_stop_s"] == 600

    for stored, expected in ((600, 600), (None, 0), ("abc", 0), (5, 30), (10**9, 24 * 3600)):
        data = json.loads(path.read_text())
        data[0]["idle_stop_s"] = stored
        path.write_text(json.dumps(data))
        fresh = CameraInfo(id="/dev/video0", name="HD Webcam")
        m2 = CameraManager()
        monkeypatch.setattr(CameraManager, "scan", lambda self, f=fresh: self.cameras.update({f.id: f}))
        assert m2.restore_session() is True
        assert fresh.idle_stop_s == expected, (stored, fresh.idle_stop_s)


def test_a_picker_row_says_why_it_is_idle_when_the_daemon_closed_it(monkeypatch):
    """"유휴로 종료됨"은 사람이 켠 옵션의 결과다 — USB 를 확인하러 가면 안 된다."""
    from app.services import camera_manager as cm

    hub = _Hub()
    cam = _cam(monkeypatch, hub, idle_stop_s=300)
    cam.connected, cam.present = False, True
    m = cm.CameraManager()
    m.cameras[cam.id] = cam

    monkeypatch.setattr(cm, "_idle_record", lambda cid: None)
    assert m.streams()[0]["reason"] == "연결 안 됨"
    monkeypatch.setattr(cm, "_idle_record", lambda cid: {"at": 1.0, "after_s": 300})
    row = m.streams()[0]
    assert row["reason"] == "유휴로 종료됨" and row["state"] == "idle"
    assert row["idle_stop_s"] == 300


def test_connecting_clears_the_old_record(monkeypatch):
    from app.services import camera_manager as cm

    cleared = []
    monkeypatch.setattr(cm, "_clear_idle_record", lambda cid: cleared.append(cid))
    cam = _cam(monkeypatch, _Hub())
    cam.connect()
    assert cleared == ["/dev/video0"]


def test_the_endpoint_warns_when_the_daemon_could_not_take_it(monkeypatch):
    import asyncio

    from app.routers import cameras as router
    from app.services.camera_manager import CameraManager, camera_manager

    hub = _Hub(knows=False)
    cam = _cam(monkeypatch, hub)
    cam.connected = True
    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(CameraManager, "save_session", lambda self: None)

    out = asyncio.run(router.set_idle_stop(router.IdleStopRequest(id=cam.id, seconds=300)))
    assert out["idle_stop_s"] == 300
    assert "받지 못했습니다" in out["warning"], "저장은 됐지만 닫히지 않는다는 걸 말해야 한다"


def test_prepare_holds_a_camera_that_has_the_option(monkeypatch):
    from app.core.config import settings
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    hub = _Hub()
    cam = _cam(monkeypatch, hub, idle_stop_s=60)
    cam.present = True
    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(settings, "camera_transport", "shm")
    monkeypatch.setattr(camera_config, "_wait_for_frame", lambda *a, **k: True)

    camera_config.prepare_cameras({"hand": "top"}, purpose="test")
    assert ("hold", camera_config.START_HOLD_S) in hub.calls


# ── 관찰자 목록은 코드가 지킨다 ─────────────────────────────────────────

def test_only_long_lived_readers_are_consumers():
    """상시 도는 감시·스냅샷·존재 확인은 전부 관찰자여야 한다 — 하나라도 소비자로 열면 그
    카메라는 영원히 안 닫힌다. 소비자는 셋뿐이다: LeRobot 플러그인(수집·추론), yolod, MJPEG 미리보기."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    allowed_consumers = {
        "vendor/lerobot_camera_pipershm/lerobot_camera_pipershm/pipershmcamera.py",
        "daemons/yolod.py",
        "backend/app/services/shm_snapshot.py",           # segment_reader (MJPEG)
    }
    offenders = []
    for root in ("backend/app", "daemons", "cam", "rs", "sim", "vendor"):
        for p in (repo / root).rglob("*.py"):
            rel = str(p.relative_to(repo))
            if "tests" in p.parts or "egg-info" in rel:
                continue
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"\bSubscriber\(", line) and "class Subscriber" not in line \
                        and "observer=True" not in line and rel not in allowed_consumers:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "관찰자가 아닌 Subscriber 가 있다:\n  " + "\n  ".join(offenders)

    snap = (repo / "backend/app/services/shm_snapshot.py").read_text()
    assert snap.count("Subscriber(") == 2 and snap.count("observer=True") == 1, (
        "shm_snapshot 은 스트림(소비자)과 한 장 스냅샷(관찰자)이 하나씩이어야 한다")


def test_every_daemon_exposes_the_idle_verbs_and_runs_the_tick():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for daemon, verbs in (("camerad.py", ('"set_idle"', '"hold"')),
                          ("rsd.py", ('"set_idle"', '"hold"')),
                          ("simd.py", ('"cam_set_idle"', '"cam_hold"'))):
        src = (repo / "daemons" / daemon).read_text()
        for v in verbs:
            assert v in src, f"{daemon} 에 {v} 가 없다"
        assert "tick_daemon" in src, f"{daemon} 루프가 유휴 점검을 안 돈다"
        # 요청이 없을 때가 정확히 닫을 때다 — `continue` 보다 앞이어야 한다
        loop = src.split("tick_daemon(", 2)[-1]
        assert src.index("tick_daemon(") < src.index("rpc_next_request"), (
            f"{daemon} 의 점검이 요청 수신 뒤에 있다 — 요청이 없으면 안 돈다")


# ── 닫힌 걸 게이트웨이가 알아야 한다 ────────────────────────────────────

def test_a_camera_the_daemon_closed_is_no_longer_shown_as_connected(monkeypatch):
    """⚠ 게이트웨이는 데몬이 **스스로** 닫은 것을 모른다(사람이 [연결 해제] 를 누를 때만 안다).
    안 내리면 선택기가 닫힌 카메라를 "연결됨"으로 보여 줘서 골랐다가 시작에서 거절당한다."""
    from app.services import camera_manager as cm

    cam = _cam(monkeypatch, _Hub(), idle_stop_s=300)
    cam.connected, cam.present = True, True
    m = cm.CameraManager()
    m.cameras[cam.id] = cam

    monkeypatch.setattr(cm, "_idle_record", lambda cid: None)
    assert m.sync_idle_stops() == [] and cam.connected is True

    monkeypatch.setattr(cm, "_idle_record", lambda cid: {"at": 1.0, "after_s": 300})
    assert m.sync_idle_stops() == [cam.id]
    assert cam.connected is False
    row = m.streams()[0]
    assert row["state"] == "idle" and row["selectable"] is False
    assert row["reason"] == "유휴로 종료됨"


def test_a_camera_without_the_option_is_never_flipped_by_a_stray_record(monkeypatch):
    from app.services import camera_manager as cm

    cam = _cam(monkeypatch, _Hub())                     # 옵션 끔
    cam.connected = True
    m = cm.CameraManager()
    m.cameras[cam.id] = cam
    monkeypatch.setattr(cm, "_idle_record", lambda cid: {"at": 1.0, "after_s": 300})
    assert m.sync_idle_stops() == [] and cam.connected is True


def test_the_watch_does_not_call_an_idle_close_a_fault(monkeypatch):
    """세그먼트가 사라진 것을 "영상이 끊겼습니다"로 읽으면 사람이 켠 옵션이 경보를 울린다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "backend/app/services/device_watch.py").read_text()
    body = src.split("def _cameras", 1)[1].split("cams = {cid: c", 1)[0]
    assert "sync_idle_stops" in body, "유휴 종료를 쓰는 것에서 빼기 전에 반영하지 않는다"
