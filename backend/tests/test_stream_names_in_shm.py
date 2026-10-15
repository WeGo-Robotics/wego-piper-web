"""스트림 이름이 **세그먼트까지** 내려간다 (feature/stream-based-cameras.md §5-4).

사용자가 카메라에 붙인 이름이 `/dev/shm/piper.cam.<이름>` 이 된다. 이 파일은

- 이름 지정이 세그먼트 이름 함수(`segment_for_camera`)를 한 곳에서 바꾸는지,
- 발행자 pid 가 헤더에 남고 **죽은 발행자의 잔재만** 치우는지,
- 데몬이 이름을 받아 발행을 옮기는지,
- 게이트웨이가 **확인된 이름만** 드는지(옛 데몬이면 규칙 이름 그대로)
를 본다.
"""

import os
import subprocess
import sys
import uuid

import numpy as np
import pytest

from piper_shm import (
    Publisher,
    Subscriber,
    clear_segment_name,
    dead_segments,
    list_segments,
    owner_pid,
    rule_segment_name,
    segment_for_camera,
    segment_path,
    set_segment_name,
    unlink,
)


def _name(tag="x") -> str:
    # 운영 세그먼트와 섞이지 않게 — 테스트 전용 접두사
    return f"pytest-{tag}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def segs():
    made: list[str] = []
    yield made
    for n in made:
        unlink(n)


# ── 이름 규칙 · 지정 ────────────────────────────────────────────────────

def test_the_rule_name_is_unchanged_without_an_assignment():
    assert segment_for_camera("rs:250122070363:color") == "rs_250122070363_color"
    assert segment_for_camera("/dev/video0") == "dev_video0"


def test_an_assigned_name_replaces_the_rule_everywhere_one_function_is_called():
    set_segment_name("/dev/video0", "top")
    assert segment_for_camera("/dev/video0") == "top"
    assert rule_segment_name("/dev/video0") == "dev_video0", "규칙 함수는 지정을 보면 안 된다"
    clear_segment_name("/dev/video0")
    assert segment_for_camera("/dev/video0") == "dev_video0"


@pytest.mark.parametrize("name", ["top", "D435_Color_1", "a-b", "x" * 40])
def test_a_valid_stream_name_passes_through_the_rule_unchanged(name):
    """이미 세그먼트 이름인 값을 규칙 함수에 넣어도(`/api/vision/segments` 가 그렇게 준다)
    그대로 돌아온다 — 앞뒤 `_` 를 떼는 규칙이 건드릴 글자가 이름 규칙에 없다."""
    assert segment_for_camera(name) == name


def test_two_cameras_cannot_share_one_segment_name():
    """한 세그먼트에 라이터가 둘이면 `write_seq` 가 깨져 리더가 찢어진 프레임을 본다."""
    set_segment_name("/dev/video0", "top")
    with pytest.raises(ValueError, match="다른 카메라"):
        set_segment_name("/dev/video2", "top")
    set_segment_name("/dev/video0", "top")           # 자기 이름을 다시 정하는 건 된다


# ── 발행자 pid · 잔재 ───────────────────────────────────────────────────

def test_the_publisher_pid_is_recorded_and_old_readers_still_read(segs):
    name = _name("pid")
    segs.append(name)
    pub = Publisher(name, 8, 6)
    try:
        pub.publish(np.full((6, 8, 3), 7, np.uint8))
        assert owner_pid(name) == os.getpid()
        sub = Subscriber(name)
        try:
            frame, seq, _ = sub.read()
            assert seq == 1 and int(frame[0, 0, 0]) == 7
            assert sub.shape == (6, 8, 3)
        finally:
            sub.close()
    finally:
        pub.close()


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _forge(name: str, pid: int) -> None:
    """세그먼트를 만든 뒤 헤더의 pid 칸만 바꾼다."""
    import struct

    pub = Publisher(name, 4, 4)
    pub.close(unlink=False)
    with open(segment_path(name), "r+b") as f:
        f.seek(52)
        f.write(struct.pack("<I", pid))


def test_dead_segments_finds_only_what_a_dead_publisher_left(segs):
    mine, dead, legacy = _name("live"), _name("dead"), _name("legacy")
    segs += [mine, dead, legacy]
    pub = Publisher(mine, 4, 4)                       # 살아 있는 발행자(이 프로세스)
    try:
        _forge(dead, _dead_pid())
        _forge(legacy, 0)                             # pid 없음 — 옛 발행자의 것

        found = set(dead_segments())
        assert dead in found
        assert mine not in found, "발행 중인 세그먼트를 잔재로 봤다"
        assert legacy not in found, "소유를 모르는 것을 접두사 없이 지웠다"

        # 접두사는 pid 가 **없는** 옛 세그먼트에만 쓴다
        assert legacy in set(dead_segments(legacy_prefix="pytest-legacy"))
        assert mine not in set(dead_segments(legacy_prefix="pytest-live"))
    finally:
        pub.close()


def test_a_live_publisher_with_a_matching_prefix_is_never_swept(segs):
    """접두사가 맞아도 **발행자가 살아 있으면** 남의 것이다 — 다른 데몬이 발행 중이다."""
    name = _name("prefix")
    segs.append(name)
    pub = Publisher(name, 4, 4)
    try:
        assert name not in dead_segments(legacy_prefix="pytest-prefix")
    finally:
        pub.close()


# ── 데몬: 이름을 받아 발행을 옮긴다 ─────────────────────────────────────

def test_a_daemon_moves_a_live_segment_to_the_new_name_and_drops_the_old(segs):
    from piper_cam import publish

    cam = f"/dev/video-{uuid.uuid4().hex[:6]}"
    old = rule_segment_name(cam)
    new = _name("renamed")
    segs += [old, new]
    frame = np.zeros((4, 6, 3), np.uint8)

    assert publish.publish_frame(cam, frame) is True
    assert old in list_segments()

    assert publish.rename(cam, new) == new
    assert old not in list_segments(), "옛 세그먼트가 남았다 — 소비자가 정지 화면을 본다"

    assert publish.publish_frame(cam, frame) is True
    assert new in list_segments(), "다음 프레임이 새 이름으로 나가야 한다"
    publish.stop(cam)


def test_a_name_assigned_before_connecting_is_used_from_the_first_frame(segs):
    """연결 전에 이름을 정해 주면 첫 세그먼트가 처음부터 그 이름이다 — 규칙 이름으로 났다가
    옮겨지는 틈에 소비자가 옛 이름을 보지 않는다."""
    from piper_cam import publish

    cam = f"/dev/video-{uuid.uuid4().hex[:6]}"
    new = _name("first")
    segs += [new, rule_segment_name(cam)]
    publish.rename(cam, new)
    publish.publish_frame(cam, np.zeros((4, 6, 3), np.uint8))
    assert new in list_segments()
    assert rule_segment_name(cam) not in list_segments()
    publish.stop(cam)


def test_naming_a_camera_clears_the_scan_thumbnail_it_left_behind(segs):
    """스캔 `probe` 는 썸네일 세그먼트를 남긴다(발행자 없이). 이름을 받은 카메라는 그것이
    영원히 남아 `/api/vision/segments` 에 정지 화면으로 뜨면 안 된다."""
    from piper_cam import publish

    cam = f"/dev/video-{uuid.uuid4().hex[:6]}"
    old, new = rule_segment_name(cam), _name("named")
    segs += [old, new]
    publish.publish_frame(cam, np.zeros((4, 6, 3), np.uint8))
    publish.stop(cam, unlink_segment=False)           # probe 가 하는 것 — 파일만 남는다
    assert old in list_segments()

    publish.rename(cam, new)
    assert old not in list_segments()


def test_a_daemon_refuses_a_name_another_camera_already_uses(segs):
    from piper_cam import publish

    a = f"/dev/video-{uuid.uuid4().hex[:6]}"
    b = f"/dev/video-{uuid.uuid4().hex[:6]}"
    taken = _name("taken")
    publish.rename(a, taken)
    with pytest.raises(ValueError):
        publish.rename(b, taken)
    assert segment_for_camera(b) == rule_segment_name(b), "거절됐는데 이름이 바뀌었다"


def test_every_camera_daemon_exposes_the_naming_verb():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for daemon, verb in (("camerad.py", '"set_stream"'), ("rsd.py", '"set_stream"'),
                         ("simd.py", '"cam_set_stream"')):
        assert verb in (repo / "daemons" / daemon).read_text(), f"{daemon} 에 {verb} 가 없다"


def test_the_gateway_no_longer_guesses_kind_from_the_name():
    """이름은 사용자가 정한다 — `rs_`·`_depth` 모양으로 종류를 추측하면 개명하는 순간 틀린다."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for rel in ("backend/app/services/light_watch.py", "backend/app/services/device_watch.py"):
        src = (repo / rel).read_text()
        assert '.endswith("_depth")' not in src, f"{rel} 이 이름 접미사로 깊이를 가린다"
        assert '.startswith("rs_")' not in src, f"{rel} 이 이름 접두사로 종류를 가린다"


# ── 게이트웨이: 확인된 이름만 든다 ──────────────────────────────────────

class _Hub:
    """데몬을 흉내 낸다. 호출 순서를 기록하고, 이름 지정을 알면 그대로 확인해 준다."""

    def __init__(self, knows_naming=True, connect_ok=True):
        self.knows_naming, self.connect_ok = knows_naming, connect_ok
        self.calls: list[tuple] = []
        self.published_as: dict[str, str] = {}

    def set_stream(self, cam_id, name):
        self.calls.append(("set_stream", cam_id, name))
        if not self.knows_naming:
            return None                   # 옛 데몬 — 이 동사를 모른다
        self.published_as[cam_id] = name
        return name

    def connect(self, cam_id, w, h, f, controls, capture=None, output=None):
        self.calls.append(("connect", cam_id))
        return (True, "OK") if self.connect_ok else (False, "Cannot open")

    def info(self, cam_id):
        return {"connected": True}


def _gateway(monkeypatch, hub, *cams):
    from app.services.camera_manager import CameraInfo, CameraManager

    monkeypatch.setattr(CameraInfo, "_hub", property(lambda self: hub))
    m = CameraManager()
    for c in cams:
        m.cameras[c.id] = c
    return m


def _cam(cam_id="/dev/video0", name="HD Webcam", **kw):
    from app.services.camera_manager import CameraInfo

    return CameraInfo(id=cam_id, name=name, **kw)


def test_registering_tells_the_daemon_the_name_before_it_connects(monkeypatch):
    """데몬은 연결 직후 첫 프레임을 발행한다. 이름이 그보다 늦으면 첫 세그먼트가 규칙
    이름으로 났다가 옮겨진다 — 그 틈에 소비자가 옛 이름을 본다."""
    hub = _Hub()
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)

    assert m.register_camera(cam.id, "top") == (True, "OK")
    verbs = [c[0] for c in hub.calls]
    assert verbs.index("set_stream") < verbs.index("connect")
    assert hub.calls[0] == ("set_stream", "/dev/video0", "top")
    assert segment_for_camera(cam.id) == "top"


def test_a_failed_register_leaves_no_name_behind(monkeypatch):
    """등록 안 된 카메라가 이름을 쥐고 있으면 다른 카메라의 기본 번호를 건너뛰게 만들고,
    소비자는 없는 이름을 찾는다."""
    hub = _Hub(connect_ok=False)
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)

    ok, why = m.register_camera(cam.id, "top")
    assert ok is False and "Cannot open" in why
    assert cam.stream == "" and cam.ready is False
    assert segment_for_camera(cam.id) == "dev_video0"


def test_an_old_daemon_that_does_not_know_naming_keeps_the_rule_name(monkeypatch):
    """⚠ 게이트웨이가 먼저 새 이름을 들면 **없는 세그먼트를 찾는다.** 옛 데몬은 여전히
    규칙 이름으로 발행하므로, 확인이 안 오면 이쪽도 규칙 이름 그대로여야 한다."""
    hub = _Hub(knows_naming=False)
    cam = _cam("rs:1:color", "D435 Color")
    m = _gateway(monkeypatch, hub, cam)

    assert m.register_camera(cam.id, "top") == (True, "OK"), "옛 데몬 때문에 연결이 막혔다"
    assert cam.stream == "top", "레지스트리에는 이름이 있다"
    assert segment_for_camera(cam.id) == "rs_1_color", "확인 없이 새 이름을 들었다"


def test_a_consumer_gets_the_named_segment_only_once_the_daemon_confirmed(monkeypatch):
    from app.core.config import settings
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    for knows, expected in ((True, "top"), (False, "rs_1_color")):
        clear_segment_name("rs:1:color")
        hub = _Hub(knows_naming=knows)
        cam = _cam("rs:1:color", "D435 Color", cam_type="realsense")
        m = _gateway(monkeypatch, hub, cam)
        m.register_camera(cam.id, "top")
        monkeypatch.setattr(camera_manager, "cameras", m.cameras)
        monkeypatch.setattr(settings, "camera_transport", "shm")
        built = camera_config.build_cameras_json({"hand": "top"}, width=640, height=480, fps=30)
        assert built["hand"]["segment"] == expected


def test_renaming_a_connected_camera_moves_the_live_segment(monkeypatch):
    hub = _Hub()
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)
    m.register_camera(cam.id, "top")
    hub.calls.clear()

    assert m.rename_stream(cam.id, "wrist") is True
    assert hub.calls == [("set_stream", "/dev/video0", "wrist")]
    assert segment_for_camera(cam.id) == "wrist"


def test_a_rename_the_daemon_did_not_confirm_keeps_pointing_at_the_live_segment(monkeypatch):
    """데몬이 못 받았으면 세그먼트는 옛 이름으로 계속 발행된다 — 이쪽도 옛 이름을 들어야
    수집·추론이 멀쩡히 읽는다. 레지스트리의 이름만 새것이다."""
    hub = _Hub()
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)
    m.register_camera(cam.id, "top")
    hub.knows_naming = False

    assert m.rename_stream(cam.id, "wrist") is True
    assert cam.stream == "wrist"
    assert segment_for_camera(cam.id) == "top"


def test_renaming_an_idle_camera_still_tells_the_daemon(monkeypatch):
    """게이트웨이의 `connected` 는 2초 주기로 따라오는 캐시다 — 안 열렸다고 믿고 알리지 않으면,
    방금 연결된 카메라는 데몬이 옛 이름으로 발행하는데 이쪽은 새 이름을 찾는다. 데몬은 안 열린
    카메라의 이름을 받아 두었다가 연결할 때 그 이름으로 발행한다."""
    hub = _Hub()
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)
    m.register_camera(cam.id, "top")
    cam.connected = False
    hub.calls.clear()

    m.rename_stream(cam.id, "wrist")
    assert hub.calls == [("set_stream", "/dev/video0", "wrist")]
    assert segment_for_camera(cam.id) == "wrist"


def test_unregistering_a_connected_camera_returns_it_to_the_rule_name(monkeypatch):
    """연결된 채 해제하면 데몬은 옛 이름으로 발행 중이다. 데몬도 규칙 이름으로 되돌려야
    이쪽이 찾는 이름과 맞는다."""
    hub = _Hub()
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)
    m.register_camera(cam.id, "top")
    hub.calls.clear()

    assert m.unregister_camera(cam.id) is True
    assert hub.calls == [("set_stream", "/dev/video0", "dev_video0")]
    assert segment_for_camera(cam.id) == "dev_video0"
    assert cam.stream == "" and cam.ready is False


def test_restoring_a_session_tells_a_long_lived_daemon_the_names(tmp_path, monkeypatch):
    """데몬은 게이트웨이보다 오래 산다. 게이트웨이만 재시작하면 데몬은 규칙 이름으로
    발행 중인데 이쪽은 복원한 새 이름을 찾는다 — 복원 직후 알려야 한다."""
    import json

    from app.services.camera_manager import CameraManager

    path = tmp_path / "camera_session.json"
    monkeypatch.setattr(CameraManager, "CAMERA_SESSION_PATH", path)
    path.write_text(json.dumps([
        {"id": "/dev/video0", "key": "/dev/video0", "name": "HD Webcam", "stream": "top",
         "cam_type": "opencv", "config": {}},
    ]))
    hub = _Hub()
    cam = _cam()
    m = _gateway(monkeypatch, hub, cam)
    monkeypatch.setattr(CameraManager, "scan", lambda self: None)

    assert m.restore_session() is True
    assert ("set_stream", "/dev/video0", "top") in hub.calls
    assert segment_for_camera(cam.id) == "top"


# ── 이름 규칙: 세그먼트 규칙과 부딪히는 것 ──────────────────────────────

@pytest.mark.parametrize("bad", ["top_", "top-", "_top", "-top"])
def test_a_name_must_start_and_end_with_a_letter_or_digit(bad):
    """규칙 함수가 앞뒤 `_` 를 뗀다 — `top_` 은 한 번 지나면 `top` 이 돼 다른 세그먼트를 찾는다."""
    from app.services import camera_streams as S

    with pytest.raises(S.StreamNameError):
        S.validate(bad)


def test_a_name_cannot_equal_another_cameras_rule_name():
    """이름을 지정하지 않은 카메라는 아직 규칙 이름으로 발행된다 — 같은 이름이면 한
    세그먼트에 라이터가 둘이다."""
    from app.services import camera_streams as S

    a, b = _cam("/dev/video0"), _cam("/dev/video2")
    with pytest.raises(S.StreamNameConflict):
        S.check_unique("dev_video2", [a, b], except_id=a.id)
    S.check_unique("dev_video0", [a, b], except_id=a.id)       # 자기 규칙 이름은 된다
    assert S.default_name("dev video", S.reserved_names([a, b])) not in {"dev_video0", "dev_video2"}


# ── 도는 동안은 세그먼트를 옮기지 않는다 ────────────────────────────────

def test_a_connected_camera_cannot_be_renamed_while_a_task_runs(monkeypatch):
    """개명·등록 해제는 발행 중인 세그먼트를 옮긴다. 수집·추론이 그걸 열고 있으면 옛 이름을 문 채
    얼어붙은 화면을 본다 — 에러도 안 난다. 도는 동안은 막는다."""
    from fastapi import HTTPException

    from app.routers import cameras as router
    from app.services.camera_manager import camera_manager

    cam = _cam()
    cam.connected = True
    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(router, "_camera_owner", lambda: "추론")

    with pytest.raises(HTTPException) as e:
        router._guard_stream_move(cam.id)
    assert e.value.status_code == 409 and "추론" in e.value.detail

    cam.connected = False                                  # 옮길 세그먼트가 없다
    router._guard_stream_move(cam.id)
    monkeypatch.setattr(router, "_camera_owner", lambda: None)
    cam.connected = True
    router._guard_stream_move(cam.id)                      # 도는 게 없으면 된다
