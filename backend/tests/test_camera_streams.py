"""스트림 이름 — 규칙 · 기본 이름 · 유일성 · 개명 이력 · 마이그레이션 · 디렉토리
(feature/stream-based-cameras.md §5-1·§5-3).

등록한 카메라는 **사용자가 붙이는 이름**으로 불린다. 이 파일은 그 이름이
- 규칙을 어기면 **조용히 고쳐 쓰지 않고** 사유와 함께 거절되는지,
- 옛 별칭을 가진 설치가 첫 기동에서 **사람이 정한 값을 잃지 않는지**,
- 소비자(수집·추론)가 새 이름·옛 이름·옛 장치 id 어느 쪽으로 불러도 같은 카메라를 가리키는지
를 본다.
"""

import json

import pytest

from app.services import camera_streams as S
from app.services.camera_manager import CameraInfo, CameraManager


def _cam(cam_id="/dev/video0", name="HD Webcam", **kw) -> CameraInfo:
    return CameraInfo(id=cam_id, name=name, **kw)


def _mgr(*cams: CameraInfo) -> CameraManager:
    m = CameraManager()
    for c in cams:
        m.cameras[c.id] = c
    return m


def _registered(cam_id, name, stream, **kw) -> CameraInfo:
    cam = _cam(cam_id, name, stream=stream, **kw)
    cam.ready = True
    cam.connected = True
    return cam


# ── 규칙 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ok", ["top", "Wrist_1", "cam-2", "D435_Color_1", "a", "x" * 40])
def test_valid_names(ok):
    assert S.validate(ok) == ok


@pytest.mark.parametrize("bad", [
    "", "   ", "탑뷰", "top view", "top.cam", "a/b", "a=b", "_top", "-top", "x" * 41,
    "top!", "카메라1",
])
def test_invalid_names_are_rejected_not_fixed(bad):
    """⚠ `탑뷰` 를 `cam_1` 로 바꿔 받아 주면 사람은 자기가 정한 이름이 적용된 줄 안다."""
    with pytest.raises(S.StreamNameError):
        S.validate(bad)


def test_validate_trims_surrounding_whitespace_only():
    assert S.validate("  top  ") == "top"


def test_the_reason_names_the_allowed_characters():
    with pytest.raises(S.StreamNameError, match="영문자·숫자"):
        S.validate("탑뷰")


# ── 기본 이름 ───────────────────────────────────────────────────────────

def test_slug_of_device_names():
    assert S.slug("D435 Color") == "D435_Color"
    assert S.slug("HD Webcam") == "HD_Webcam"
    assert S.slug("USB2.0 HD UVC WebCam: USB2.0 HD") == "USB2_0_HD_UVC_WebCam_USB2_0_HD"


def test_slug_falls_back_when_nothing_is_ascii():
    assert S.slug("카메라") == "cam"
    assert S.slug("") == "cam"


def test_default_name_is_numbered_even_for_one_camera():
    assert S.default_name("HD Webcam", []) == "HD_Webcam_1"


def test_default_name_takes_the_smallest_free_number_case_insensitively():
    assert S.default_name("HD Webcam", ["hd_webcam_1", "HD_Webcam_3"]) == "HD_Webcam_2"


def test_default_name_always_fits_the_rules():
    long = "A" * 80
    assert S.NAME_RE.match(S.default_name(long, []))


# ── 옛 별칭 → 이름 ──────────────────────────────────────────────────────

def test_migrate_label_turns_spaces_into_underscores():
    assert S.migrate_label("Top View") == "Top_View"
    assert S.migrate_label("wrist") == "wrist"


@pytest.mark.parametrize("unusable", ["탑뷰", "손목 카메라", "Top (D405)", "", None])
def test_migrate_label_gives_up_on_what_cannot_be_a_name(unusable):
    assert S.migrate_label(unusable) is None


# ── 등록 · 개명 ─────────────────────────────────────────────────────────

def test_register_gives_a_default_name_and_numbers_the_second_of_a_model():
    a = _cam("/dev/video0", "HD Webcam", usb_port="1-1")
    b = _cam("/dev/video2", "HD Webcam", usb_port="1-2")
    a.connected = b.connected = True
    m = _mgr(a, b)

    assert m.register_camera(a.id) == (True, "OK")
    assert m.register_camera(b.id) == (True, "OK")
    assert (a.stream, b.stream) == ("HD_Webcam_1", "HD_Webcam_2")


def test_register_validates_the_name_before_opening_the_device():
    """틀린 이름 때문에 실패할 일이면 카메라를 열었다 닫는 일(RealSense 는 수 초)을
    먼저 하지 않는다."""
    class _Boom(CameraInfo):
        def connect(self, *a, **k):
            raise AssertionError("이름이 틀렸는데 장치를 열었다")

    cam = _Boom(id="/dev/video0", name="HD Webcam")
    m = _mgr(cam)
    ok, why = m.register_camera(cam.id, "탑뷰")
    assert ok is False and "영문자·숫자" in why
    assert cam.ready is False and cam.stream == ""


def test_register_refuses_a_name_another_camera_uses():
    a = _registered("rs:1:color", "D435 Color", "top")
    b = _cam("/dev/video0")
    b.connected = True
    m = _mgr(a, b)

    ok, why = m.register_camera(b.id, "TOP")           # 대소문자 무시
    assert ok is False and "다른 카메라" in why
    assert b.ready is False


def test_rename_records_the_old_name_as_an_alias():
    cam = _registered("rs:1:color", "D435 Color", "D435_Color_1")
    m = _mgr(cam)
    assert m.rename_stream(cam.id, "top") is True
    assert cam.stream == "top"
    assert cam.aliases == ["D435_Color_1"]
    assert m.resolve("D435_Color_1") is cam, "옛 이름이 같은 카메라로 안 풀린다"


def test_rename_conflict_is_distinct_from_a_rule_violation():
    a = _registered("rs:1:color", "D435 Color", "top")
    b = _registered("/dev/video0", "HD Webcam", "wrist")
    m = _mgr(a, b)
    with pytest.raises(S.StreamNameConflict):
        m.rename_stream(b.id, "top")
    with pytest.raises(S.StreamNameError) as e:
        m.rename_stream(b.id, "탑뷰")
    assert not isinstance(e.value, S.StreamNameConflict)
    assert b.stream == "wrist", "거절됐는데 이름이 바뀌었다"


def test_rename_unknown_camera_is_false_and_unregistered_is_refused():
    m = _mgr(_cam())
    assert m.rename_stream("/dev/nope", "x") is False
    with pytest.raises(S.StreamNameError, match="등록한 카메라만"):
        m.rename_stream("/dev/video0", "top")


def test_swapping_two_cameras_roles_uses_a_temporary_name_and_the_current_name_wins():
    """카메라를 서로 옮겨 달면 top 과 wrist 가 맞바뀐다. 현재 이름은 유일해야 하므로
    임시 이름을 거친다. 끝나면 옛 이름이 아니라 **현재 이름이 이긴다.**"""
    a = _registered("rs:1:color", "D435 Color", "top")
    b = _registered("/dev/video0", "HD Webcam", "wrist")
    m = _mgr(a, b)

    m.rename_stream(a.id, "tmp")
    m.rename_stream(b.id, "top")
    m.rename_stream(a.id, "wrist")

    assert (a.stream, b.stream) == ("wrist", "top")
    assert m.resolve("top") is b
    assert m.resolve("wrist") is a
    # 서로의 옛 이름은 이력에서 빠져 있어야 한다 — 남아 있으면 현재 이름과 다투게 된다
    assert "top" not in [x.lower() for x in a.aliases]
    assert "wrist" not in [x.lower() for x in b.aliases]


def test_new_default_names_skip_names_held_as_aliases():
    """옛 이름이 다른 카메라에 재사용되면 그 이름으로 저장된 매핑이 엉뚱한 카메라로 풀린다."""
    a = _registered("rs:1:color", "HD Webcam", "HD_Webcam_1")
    m = _mgr(a)
    m.rename_stream(a.id, "top")                  # HD_Webcam_1 은 이력으로 간다
    b = _cam("/dev/video0", "HD Webcam")
    b.connected = True
    m.cameras[b.id] = b

    m.register_camera(b.id)
    assert b.stream != "HD_Webcam_1"
    assert b.stream == "HD_Webcam_2"


def test_alias_history_is_capped():
    cam = _registered("rs:1:color", "D435 Color", "n0")
    m = _mgr(cam)
    for i in range(1, S.MAX_ALIASES + 5):
        m.rename_stream(cam.id, f"n{i}")
    assert len(cam.aliases) == S.MAX_ALIASES
    assert cam.aliases[-1] == f"n{S.MAX_ALIASES + 3}"


def test_unregister_releases_the_name_and_the_history():
    cam = _registered("rs:1:color", "D435 Color", "top")
    cam.aliases = ["old"]
    m = _mgr(cam)
    assert m.unregister_camera(cam.id) is True
    assert cam.stream == "" and cam.aliases == [] and cam.ready is False
    assert m.resolve("top") is None and m.resolve("old") is None


# ── 조회 ────────────────────────────────────────────────────────────────

def test_resolve_accepts_name_alias_device_id_and_segment_name():
    cam = _registered("rs:250122070363:color", "D435 Color", "top")
    cam.aliases = ["D435_Color_1"]
    m = _mgr(cam)
    for value in ("top", "TOP", "D435_Color_1", "rs:250122070363:color",
                  "rs_250122070363_color"):
        assert m.resolve(value) is cam, value
    assert m.resolve("nope") is None and m.resolve("") is None


# ── 디렉토리 ────────────────────────────────────────────────────────────

def test_directory_lists_registered_cameras_with_their_state():
    live = _registered("rs:1:color", "D435 Color", "top")
    idle = _cam("/dev/video0", "HD Webcam", stream="wrist")
    idle.ready, idle.connected, idle.present = True, False, True
    gone = _cam("/dev/video2", "HD Webcam", stream="side")
    gone.ready, gone.connected, gone.present = True, False, False
    unregistered = _cam("/dev/video4", "HD Webcam")
    m = _mgr(live, idle, gone, unregistered)

    rows = {r["stream"]: r for r in m.streams()}
    assert set(rows) == {"top", "wrist", "side"}, "미등록이 목록에 있다"
    assert rows["top"]["state"] == "live" and rows["top"]["selectable"] is True
    assert rows["wrist"]["state"] == "idle" and rows["wrist"]["selectable"] is False
    assert rows["side"]["state"] == "missing" and rows["side"]["selectable"] is False
    assert rows["wrist"]["reason"] and rows["side"]["reason"], "비활성인데 이유를 안 말한다"


def test_directory_kind_comes_from_the_stream_type_not_the_name():
    """이름은 사용자가 정한다 — `wrist` 가 깊이일 수 있다."""
    depth = _registered("rs:1:depth", "D435 Depth", "wrist", stream_type="depth")
    color = _registered("/dev/video0", "HD Webcam", "front")
    m = _mgr(depth, color)
    kinds = {r["stream"]: r["kind"] for r in m.streams()}
    assert kinds == {"wrist": "depth", "front": "color"}


def test_directory_sorted_and_carries_the_segment_to_open():
    from piper_shm import segment_for_camera

    a = _registered("rs:1:color", "D435 Color", "zeta")
    b = _registered("/dev/video0", "HD Webcam", "Alpha")
    rows = _mgr(a, b).streams()
    assert [r["stream"] for r in rows] == ["Alpha", "zeta"]
    assert rows[0]["segment"] == segment_for_camera("/dev/video0")


# ── 소비 경로 ───────────────────────────────────────────────────────────

def test_consumers_can_call_a_camera_by_its_stream_name(monkeypatch):
    """수집·추론은 `{키: 이름}` 을 보낸다. 같은 카메라를 옛 장치 id 로 불러도 같은 설정이 나온다."""
    from app.core.config import settings
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    cam = _registered("rs:250122070363:color", "D435 Color", "top")
    cam.aliases = ["D435_Color_1"]
    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(settings, "camera_transport", "shm")

    by_name = camera_config.build_cameras_json({"hand": "top"}, width=640, height=480, fps=30)
    by_alias = camera_config.build_cameras_json({"hand": "D435_Color_1"}, width=640, height=480, fps=30)
    by_id = camera_config.build_cameras_json({"hand": cam.id}, width=640, height=480, fps=30)
    assert by_name == by_alias == by_id
    assert by_name["hand"]["segment"] == "rs_250122070363_color"
    assert list(by_name) == ["hand"], "이름이 LeRobot 키로 새어들었다"


def test_unknown_values_are_left_for_the_caller_to_report(monkeypatch):
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    monkeypatch.setattr(camera_manager, "cameras", {})
    assert camera_config.normalize_mapping({"top": "ghost", "hand": ""}) == {"top": "ghost", "hand": ""}


def test_sidecar_records_the_stream_name_next_to_the_device_id(monkeypatch):
    """개명하면 장치 id 만으로는 "어느 스트림이었나"를 모른다 — 장치 id 는 재열거로도 바뀐다."""
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    cam = _registered("rs:1:color", "D435 Color", "top")
    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(CameraInfo, "running_profile", lambda self: {"width": 640, "height": 480, "fps": 30})

    side = camera_config.camera_sidecar({"hand": "top"})
    assert side["cameras"]["hand"]["stream"] == "top"
    assert side["cameras"]["hand"]["id"] == "rs:1:color"


# ── 세션 · 마이그레이션 ─────────────────────────────────────────────────

def _scan_returns(monkeypatch, m: CameraManager, *cams: CameraInfo):
    monkeypatch.setattr(CameraManager, "scan", lambda self: self.cameras.update({c.id: c for c in cams}))


def _session(monkeypatch, tmp_path):
    path = tmp_path / "camera_session.json"
    monkeypatch.setattr(CameraManager, "CAMERA_SESSION_PATH", path)
    return path


def test_name_and_history_survive_a_session_roundtrip(tmp_path, monkeypatch):
    path = _session(monkeypatch, tmp_path)
    cam = _registered("rs:1:color", "D435 Color", "top", cam_type="realsense")
    cam.aliases = ["D435_Color_1"]
    _mgr(cam).save_session()

    saved = json.loads(path.read_text())[0]
    assert saved["stream"] == "top" and saved["aliases"] == ["D435_Color_1"]
    assert saved["label"] == "top", "옛 필드는 되돌려도 이름이 보이게 같은 값으로 남긴다"

    m2 = CameraManager()
    fresh = _cam("rs:1:color", "D435 Color", cam_type="realsense")
    _scan_returns(monkeypatch, m2, fresh)
    assert m2.restore_session() is True
    assert fresh.stream == "top" and fresh.aliases == ["D435_Color_1"]
    assert m2.notices() == [], "새 형식인데 마이그레이션 안내가 떴다"


def _legacy(path, *entries):
    path.write_text(json.dumps([
        {"id": e["id"], "key": e["id"], "name": e.get("name", "HD Webcam"),
         "label": e.get("label", ""), "cam_type": "opencv", "config": {}}
        for e in entries
    ]))


def test_a_legacy_session_keeps_labels_that_can_be_names(tmp_path, monkeypatch):
    path = _session(monkeypatch, tmp_path)
    _legacy(path, {"id": "/dev/video0", "label": "Top View"})
    m = CameraManager()
    cam = _cam("/dev/video0")
    _scan_returns(monkeypatch, m, cam)

    assert m.restore_session() is True
    assert cam.stream == "Top_View"
    assert m.notices() == [], "그대로 쓸 수 있는 별칭인데 안내가 떴다"
    # 곧바로 새 형식으로 다시 쓴다 — 안 쓰면 재시작마다 같은 변환을 되풀이한다
    assert json.loads(path.read_text())[0]["stream"] == "Top_View"


def test_a_legacy_label_that_cannot_be_a_name_is_replaced_and_announced(tmp_path, monkeypatch):
    """⚠ 사람이 정한 값이 흔적 없이 바뀌면 안 된다 — 바뀐 사실을 남긴다."""
    path = _session(monkeypatch, tmp_path)
    _legacy(path, {"id": "/dev/video0", "label": "탑뷰", "name": "HD Webcam"})
    m = CameraManager()
    cam = _cam("/dev/video0", "HD Webcam")
    _scan_returns(monkeypatch, m, cam)

    assert m.restore_session() is True
    assert cam.stream == "HD_Webcam_1"
    notes = m.notices()
    assert len(notes) == 1 and "탑뷰" in notes[0] and "HD_Webcam_1" in notes[0]

    m.clear_notices()
    assert m.notices() == []


def test_legacy_labels_that_collide_do_not_share_a_name(tmp_path, monkeypatch):
    path = _session(monkeypatch, tmp_path)
    _legacy(path, {"id": "/dev/video0", "label": "top"}, {"id": "/dev/video2", "label": "TOP"})
    m = CameraManager()
    a, b = _cam("/dev/video0"), _cam("/dev/video2")
    _scan_returns(monkeypatch, m, a, b)

    assert m.restore_session() is True
    assert a.stream == "top"
    assert b.stream and b.stream.lower() != "top"
    assert len(m.notices()) == 1


def test_saved_names_win_over_defaults_given_to_earlier_cameras(tmp_path, monkeypatch):
    """한 대의 기본 이름이 아직 처리 안 된 다른 카메라의 저장된 이름을 가로채면 안 된다."""
    path = _session(monkeypatch, tmp_path)
    path.write_text(json.dumps([
        {"id": "/dev/video0", "key": "/dev/video0", "name": "HD Webcam", "label": "",
         "cam_type": "opencv", "config": {}},                       # 이름 없음 → 기본
        {"id": "/dev/video2", "key": "/dev/video2", "name": "HD Webcam", "stream": "HD_Webcam_1",
         "cam_type": "opencv", "config": {}},                       # 새 형식, 저장된 이름
    ]))
    m = CameraManager()
    a, b = _cam("/dev/video0"), _cam("/dev/video2")
    _scan_returns(monkeypatch, m, a, b)

    assert m.restore_session() is True
    assert b.stream == "HD_Webcam_1"
    assert a.stream == "HD_Webcam_2"


def test_clear_all_drops_pending_notices(tmp_path, monkeypatch):
    _session(monkeypatch, tmp_path)
    m = CameraManager()
    m._add_notice("x")
    assert m.notices() == ["x"]
    m.clear_all()
    assert m.notices() == []


# ── 시작할 때 준비 — 연결된 것만 쓴다 ───────────────────────────────────

class _Prep:
    """`prepare_cameras` 가 만지는 면만 가진 카메라. 연결 시도를 기록한다."""

    def __init__(self, cam_id, connected, want=None, present=True):
        self.id, self.name, self.stream, self.aliases = cam_id, "D435 Color", "top", []
        self.present, self.ready, self.label = present, True, "top"
        self._connected, self._want = connected, want or []
        self.connects: list[tuple] = []

    def running_profile(self):
        return {"connected": self._connected, "want": self._want,
                "width": 640, "height": 480, "fps": 30}

    def hold(self, seconds):                    # 유휴 종료 유예 — 이 가짜는 꺼져 있다
        return False

    def connect(self, w=0, h=0, f=0):
        self.connects.append((w, h, f))
        self._connected, self._want = True, [w, h, f]
        return True, "OK"


def _prepare(monkeypatch, cam, mapping, **kw):
    from app.core.config import settings
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(settings, "camera_transport", "shm")
    monkeypatch.setattr(camera_config, "_wait_for_frame", lambda *a, **k: True)
    camera_config.prepare_cameras(mapping, purpose="test", **kw)


def test_prepare_refuses_a_camera_that_is_not_connected_and_does_not_open_it(monkeypatch):
    """선택기에서 비활성으로 보이는 카메라다 — 시작 요청이 몰래 열면 "연결된 것만 쓴다"가
    깨지고, 유휴 종료를 켠 카메라가 시작 때마다 조용히 되살아난다."""
    from app.services.camera_config import CameraPrepareError

    cam = _Prep("rs:1:color", connected=False)
    with pytest.raises(CameraPrepareError, match="연결되지 않았습니다"):
        _prepare(monkeypatch, cam, {"hand": "top"})
    assert cam.connects == [], "연결 안 된 카메라를 열었다"


def test_prepare_trusts_the_daemon_not_the_gateway_cache(monkeypatch):
    """게이트웨이의 `connected` 는 2초 주기로 따라온다 — 방금 연결한 것을 놓칠 수 있다.
    판정은 데몬이 말하는 값이다."""
    cam = _Prep("rs:1:color", connected=True)
    cam.connected = False                     # 캐시는 아직 모른다
    _prepare(monkeypatch, cam, {"hand": "top"})       # 거절되지 않는다
    assert cam.connects == []


def test_prepare_renegotiates_a_connected_camera_only_when_the_request_differs(monkeypatch):
    cam = _Prep("rs:1:color", connected=True, want=[640, 480, 30])
    _prepare(monkeypatch, cam, {"hand": "top"}, width=640, height=480, fps=30)
    assert cam.connects == [], "같은 요청인데 다시 연결했다 — 그 사이 소비자가 프레임을 잃는다"

    _prepare(monkeypatch, cam, {"hand": "top"}, width=848, height=480, fps=15)
    assert cam.connects == [(848, 480, 15)]


def test_prepare_still_names_a_camera_that_is_unplugged(monkeypatch):
    from app.services.camera_config import CameraPrepareError

    cam = _Prep("rs:1:color", connected=False, present=False)
    with pytest.raises(CameraPrepareError, match="연결돼 있지 않습니다"):
        _prepare(monkeypatch, cam, {"hand": "top"})


# ── 데몬이 느려도 게이트웨이는 선다 ─────────────────────────────────────

def test_camera_listing_does_not_block_the_event_loop_while_a_daemon_is_slow(monkeypatch):
    """2026-10-14: rsd 가 20초 응답을 못 하는 동안 `/api/cameras/current` 가 이벤트 루프를 붙잡아
    게이트웨이가 통째로 섰다 — E-stop heartbeat 가 20초 끊겼다(`유실 10214건`).

    느린 데몬을 흉내 내고, 그 사이 **다른 코루틴이 계속 도는지** 본다.
    """
    import asyncio
    import time

    from app.routers import cameras as router
    from app.services.camera_manager import camera_manager

    def slow(*a, **k):
        time.sleep(0.5)                       # 데몬이 느리다
        return {"cameras": []}

    monkeypatch.setattr(camera_manager, "get_current", slow)
    monkeypatch.setattr(camera_manager, "get_ready_cameras", lambda: (time.sleep(0.5) or []))

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        await asyncio.gather(router.get_current(), router.get_ready_cameras())
        hb.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    # 0.5초 동안 50ms 주기면 ~10 번. 루프가 막혔다면 0~1 번이다.
    assert ticks >= 5, f"느린 데몬이 이벤트 루프를 막았다 (heartbeat {ticks}회)"
