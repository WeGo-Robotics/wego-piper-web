"""`/dev/shm` 프레임 전송 (refactor/camera-transport.md 착수 순서 1~2단계).

**데몬을 쪼개기 전에 전송이 되는지부터** 확인하는 단계다. 지금은 게이트웨이의
`camera_manager` 가 임시로 세그먼트를 채우고, 소비자(LeRobot 플러그인)가 읽는다.

여기서 잠그는 것:

1. **찢어진 프레임이 정책에 들어가지 않는다** — seqlock 이 전부다
2. **세그먼트가 남지 않는다** — 남으면 소비자가 "멈춘 화면"을 본다
3. 소비자가 라이터 버퍼를 그대로 보지 않는다 (복사)
"""

import numpy as np
import pytest

pytest.importorskip("piper_shm")
from piper_shm import (  # noqa: E402
    Layout,
    Publisher,
    SegmentError,
    Subscriber,
    list_segments,
    segment_path,
    unlink,
)
from piper_shm import segment as S  # noqa: E402

_NAME = "pytest_shm"


@pytest.fixture(autouse=True)
def clean():
    unlink(_NAME)
    yield
    unlink(_NAME)


def _frame(v: int, h=48, w=64) -> np.ndarray:
    return np.full((h, w, 3), v, np.uint8)


def test_roundtrip_preserves_pixels_exactly():
    """**JPEG 이중압축이 없다**는 것이 이 전송을 고른 이유 중 하나다."""
    pub = Publisher(_NAME, 64, 48)
    sub = Subscriber(_NAME)
    src = np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)
    pub.publish(src)
    got, seq, wall = sub.read()
    assert np.array_equal(got, src), "픽셀이 변했다"
    assert seq == 1 and wall > 0
    sub.close(); pub.close()


def test_reader_gets_a_copy_not_the_live_slot():
    """복사가 없으면 라이터가 덮어써 **정책이 반쯤 갈린 프레임을 본다.**"""
    pub = Publisher(_NAME, 64, 48)
    sub = Subscriber(_NAME)
    pub.publish(_frame(1))
    got, _, _ = sub.read()
    for v in range(2, 6):
        pub.publish(_frame(v))
    assert got[0, 0, 0] == 1, "리더 버퍼가 라이터에 덮였다"
    sub.close(); pub.close()


def test_no_frame_yet_returns_none():
    pub = Publisher(_NAME, 64, 48)
    sub = Subscriber(_NAME)
    assert sub.read() is None
    assert sub.read_new(timeout_s=0.05) is None
    sub.close(); pub.close()


def test_seqlock_never_yields_a_torn_frame():
    """**핵심.** 라이터를 마구 돌리는 동안 읽어도 한 프레임 안의 값이 섞이면 안 된다.

    각 프레임을 단색으로 발행하므로, 찢어졌다면 한 배열에 두 값이 섞여 나온다.
    """
    pub = Publisher(_NAME, 128, 96)
    sub = Subscriber(_NAME)
    torn = 0
    for v in range(1, 400):
        pub.publish(_frame(v % 251 + 1, 96, 128))
        got = sub.read()
        if got is None:
            continue
        if len(np.unique(got[0])) != 1:
            torn += 1
    assert torn == 0, f"찢어진 프레임 {torn}개"
    sub.close(); pub.close()


def test_reader_retries_when_writer_laps_it():
    """라이터가 한 바퀴 돌면 재시도한다 — 조용히 옛 프레임을 주지 않는다."""
    pub = Publisher(_NAME, 32, 32, n_slots=3)
    sub = Subscriber(_NAME, max_retries=2)
    for v in range(1, 10):
        pub.publish(_frame(v, 32, 32))
    got = sub.read()
    assert got is not None and got[1] == 9, "최신 프레임이 아니다"
    sub.close(); pub.close()


def test_shape_mismatch_is_loud():
    """조용히 리사이즈하면 **정책 입력 크기가 말없이 바뀐다.**"""
    pub = Publisher(_NAME, 64, 48)
    with pytest.raises(ValueError, match="프레임 모양"):
        pub.publish(_frame(1, 10, 10))
    pub.close()


def test_close_unlinks_the_segment():
    """⚠ 남기면 소비자가 그 세그먼트를 열고 **멈춘 화면**을 본다."""
    pub = Publisher(_NAME, 32, 32)
    assert _NAME in list_segments()
    pub.close()
    assert _NAME not in list_segments()
    assert not segment_path(_NAME).exists()


def test_missing_segment_fails_cleanly():
    """발행자가 없으면 소비자는 **깨끗하게** 죽어야 한다 — 좀비로 남지 않는다."""
    with pytest.raises(SegmentError, match="세그먼트가 없습니다"):
        Subscriber("definitely_not_there")


def test_bad_magic_is_rejected():
    """엉뚱한 파일을 세그먼트로 읽으면 쓰레기 프레임이 정책에 들어간다."""
    path = segment_path(_NAME)
    path.write_bytes(b"\0" * 4096)
    with pytest.raises(SegmentError, match="매직|크기"):
        Subscriber(_NAME)


def test_layout_math():
    lay = Layout(width=640, height=480, channels=3)
    assert lay.slot_bytes == 640 * 480 * 3
    assert lay.total_bytes == S.HEADER_SIZE + lay.slot_bytes * lay.n_slots


def test_header_fits_one_cache_line():
    """슬롯 시작이 캐시라인에 정렬돼야 memcpy 가 빠르다."""
    import struct

    assert struct.calcsize(S._HEADER_FMT) <= S.HEADER_SIZE == 64


# ── 발행측(게이트웨이 임시 배선) ────────────────────────────────────────────

def test_publisher_recreates_on_resolution_change():
    """해상도가 바뀌었는데 옛 세그먼트를 두면 소비자가 옛 크기로 읽어 깨진다."""
    from app.services.shm_publisher import ShmPublisher

    pub = ShmPublisher()
    assert pub.publish(_NAME, _frame(1, 48, 64))
    assert Subscriber(_NAME).shape == (48, 64, 3)
    assert pub.publish(_NAME, _frame(2, 96, 128))
    assert Subscriber(_NAME).shape == (96, 128, 3)
    pub.stop_all()


def test_publish_never_raises_into_the_capture_loop():
    """캡처 루프에서 불린다 — 여기서 던지면 프리뷰·녹화까지 같이 멈춘다."""
    from app.services.shm_publisher import ShmPublisher

    pub = ShmPublisher()
    assert pub.publish(_NAME, None) is False          # noqa: FBT003
    assert pub.publish(_NAME, np.zeros((5, 5), np.uint8)) is False   # 2차원
    pub.stop_all()


def test_gateway_never_unlinks_daemon_segments():
    """**회귀** — 게이트웨이가 데몬 소유의 세그먼트를 지우고 있었다.

    `prepare_cameras` 가 "안 쓰는 것을 치운다"며 unlink 했는데, rsd 가 **발행 중인**
    파일을 지워서 발행자는 계속 쓰고 소비자는 열 수 없는 상태가 됐다.
    증상은 조용했다 — `connect` 는 OK 인데 세그먼트가 없다.

    소유자는 데몬이다. 고아 정리는 각 데몬이 기동할 때 **자기 것만** 한다.
    """
    import inspect
    from pathlib import Path

    from app.services import camera_config

    # **주석이 아니라 호출**을 본다 — 설명문에 옛 이름이 나온다
    import ast

    src = inspect.getsource(camera_config.prepare_cameras)
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse(src.lstrip())) if isinstance(n, ast.Call)
    }
    assert not any("sweep" in c or "unlink" in c for c in calls), (
        f"게이트웨이가 세그먼트를 지운다: {calls}"
    )

    repo = Path(__file__).resolve().parents[2]
    main_calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse((repo / "backend" / "app" / "main.py").read_text()))
        if isinstance(n, ast.Call)
    }
    assert "sweep_stale_segments" not in main_calls, "기동 시 데몬 세그먼트를 쓸어버린다"

    # 데몬은 **자기 접두사만** 치운다 — 남의 것을 지우면 그쪽이 깨진다
    for daemon, prefix in (("rsd.py", "rs_"), ("camerad.py", "dev_")):
        d = (repo / "daemons" / daemon).read_text()
        assert f'startswith("{prefix}")' in d, f"{daemon} 이 자기 것만 치우지 않는다"


def test_shm_config_carries_real_dimensions():
    """LeRobot 은 로봇 카메라에 width/height/fps 를 **필수**로 요구한다.

    없으면 draccus 파싱에서 `Specifying 'width' is required` 로 죽는다.
    요청값을 그대로 쓰면 실제와 어긋난 채 데이터셋 메타에 박히므로
    세그먼트에서 읽는다 (D405 는 848x480 인데 요청은 보통 640x480 이다).
    """
    from app.core.config import settings
    from app.services.camera_config import build_cameras_json

    from piper_shm import segment_for_camera

    # 세그먼트 이름은 **장치 id 에서 규칙으로** 나온다. 손으로 짓지 않는다 —
    # 발행자(데몬)와 소비자(게이트웨이)가 같은 함수를 써야 서로를 찾는다.
    cam_id = "/dev/video-pytest-dims"
    pub = Publisher(segment_for_camera(cam_id), 848, 480)
    try:
        settings.camera_transport = "shm"
        # **요청은 640x480 인데 발행자는 848x480 이다.** 발행자가 이긴다 —
        # 요청값을 그대로 실으면 D405 가 어긋난 치수로 데이터셋 메타에 박힌다.
        cfg = build_cameras_json({"hand": cam_id}, width=640, height=480, fps=15)
        assert (cfg["hand"]["width"], cfg["hand"]["height"]) == (848, 480)
    finally:
        pub.close()
        settings.camera_transport = "direct"

    # 세그먼트가 없으면? 기본값으로라도 **채워야** 한다 (빈 값이면 파싱이 죽는다)
    try:
        settings.camera_transport = "shm"
        gone = build_cameras_json({"hand": "/dev/video-does-not-exist"}, fps=15)
        v = gone["hand"]
        assert v["width"] and v["height"] and v["fps"]
    finally:
        settings.camera_transport = "direct"


# ── 카메라 JSON 조립은 한 곳에서 ────────────────────────────────────────────

def test_recording_and_inference_share_one_builder():
    """**회귀** — 프론트가 따로 조립하면 백엔드 설정을 몰라 **녹화만 옛 경로**를 탄다.

    실제로 그랬다: 추론은 shm 으로 전환됐는데 녹화는 `RecordingPage.tsx` 가
    intelrealsense JSON 을 만들어 보내고 있었다.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    page = (repo / "frontend" / "src" / "pages" / "RecordingPage.tsx").read_text()
    assert "buildCameraConfig" not in page, "프론트가 아직 카메라 JSON 을 조립한다"
    assert "camera_mapping" in page, "매핑을 보내지 않는다"

    for router in ("models.py", "recording.py"):
        src = (repo / "backend" / "app" / "routers" / router).read_text()
        assert "build_cameras_json" in src, f"{router} 가 공용 조립기를 안 쓴다"


def test_transport_switch_changes_both_paths(monkeypatch):
    """스위치 하나로 추론·녹화가 함께 바뀌어야 한다."""
    from app.core.config import settings
    from app.services.camera_config import build_cameras_json

    mapping = {"top": "rs:1:color", "wrist": "/dev/video0"}

    monkeypatch.setattr(settings, "camera_transport", "shm")
    shm = build_cameras_json(mapping, width=640, height=480, fps=15)
    # **키는 dict 키로, 세그먼트는 장치로.** 발행자는 매핑을 모른 채 항상 발행하므로
    # 세그먼트 이름이 LeRobot 키면 매핑이 바뀔 때마다 다시 만들어야 한다.
    # 치수는 세그먼트에서 읽고, 없으면 요청값으로 떨어진다 — **비어 있으면 안 된다.**
    # LeRobot `RobotConfig.__post_init__` 이 width/height/fps 를 필수로 요구해서,
    # 빠지면 `Specifying 'width' is required` 로 녹화가 시작조차 못 한다.
    assert {k: v["segment"] for k, v in shm.items()} == {
        "top": "rs_1_color",
        "wrist": "dev_video0",
    }
    for v in shm.values():
        assert v["type"] == "shm"
        assert (v["width"], v["height"], v["fps"]) == (640, 480, 15)

    monkeypatch.setattr(settings, "camera_transport", "direct")
    direct = build_cameras_json(mapping, width=640, height=480, fps=15)
    assert direct["top"]["type"] == "intelrealsense"
    assert direct["wrist"]["type"] == "opencv"
    assert direct["top"]["width"] == 640, "direct 는 요청 해상도를 실어야 한다"


def test_prepare_is_inverted_between_transports():
    """`direct` 는 카메라를 **해제**하고 `shm` 은 **붙잡는다** — 정반대다.

    이 뒤바뀜을 놓치면 shm 에서 카메라를 해제해 프레임이 끊긴다.
    """
    import inspect

    from app.services import camera_config

    import ast

    src = inspect.getsource(camera_config.prepare_cameras)
    # 문자열이 아니라 **호출**을 본다 — 인자가 붙어도 깨지지 않게
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.lstrip()))
             if isinstance(n, ast.Call)}
    assert "release_all_cameras" in calls, "direct 경로에서 해제하지 않는다"
    assert "cam.connect" in calls, "shm 경로에서 붙잡지 않는다"
    assert 'camera_transport != "shm"' in src, "전송 방식을 안 본다"


def test_arm_error_clearing_survives_refactors():
    """**회귀** — 카메라 로직을 걷어낼 때 `_clear_arm_errors` 가 같이 잘려나갔다.

    F821(미정의 이름)로 잡혔지만, 잡히기 전까지 추론 시작이 `NameError` 로 죽는
    상태였다. 이 함수는 CAN 버스 경합을 피하려고 시작 전/종료 후에 불린다.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "backend" / "app" / "routers" / "models.py").read_text()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_clear_arm_errors" in names, "에러 클리어 함수가 사라졌다"
    # 호출부가 정의보다 많은데 정의가 없으면 NameError — 정적으로 못 박는다
    assert src.count("_clear_arm_errors(") >= 4, "정의 1 + 호출 3 이어야 한다"


def test_segment_name_comes_from_the_device_not_the_lerobot_key():
    """**설계** — 발행자는 매핑을 모른 채 항상 발행한다.

    세그먼트 이름이 LeRobot 키(`top`)면 발행자가 "이번 실행에서 이 카메라가 무슨
    키로 쓰이는지"를 알아야 하고, 매핑이 바뀔 때마다 세그먼트를 다시 만들어야 한다.
    데몬(camerad/rsd)은 실행과 무관하게 사는 존재라 그 모델과 맞지 않는다.
    """
    from piper_shm import segment_for_camera

    assert segment_for_camera("rs:250122070363:color") == "rs_250122070363_color"
    assert segment_for_camera("/dev/video0") == "dev_video0"

    # 발행은 이제 **데몬**이 한다. 장치에서 이름을 뽑는지 확인한다 —
    # 키를 쓰면 데몬이 매핑을 알아야 하고, 그러면 실행과 무관하게 살 수 없다.
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for pkg in ("rs/piper_rs", "cam/piper_cam"):
        src = (repo / pkg / "publish.py").read_text()
        assert "segment_for_camera" in src, f"{pkg} 가 장치 기준 이름을 안 쓴다"

    # 게이트웨이에는 장치 코드가 남아 있으면 안 된다.
    # **주석이 아니라 실제 호출**을 본다 — 설명문에 `VideoCapture` 가 나올 수 있다.
    import ast

    gw = (repo / "backend" / "app" / "services" / "camera_manager.py").read_text()
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse(gw)) if isinstance(n, ast.Call)
    }
    assert not any("VideoCapture" in c for c in calls), "게이트웨이가 아직 장치를 연다"
    assert not any("publish" in c for c in calls), "게이트웨이가 아직 발행한다"


def test_device_opening_config_is_rejected_under_shm(monkeypatch):
    """**회귀** — 낡은 프론트가 보낸 장치 설정이 그대로 wrapper 로 넘어갔다.

    vite 가 낡은 번들을 서빙해 브라우저가 `intelrealsense` 설정을 보냈고,
    rsd 가 쥔 장치를 wrapper 가 또 열려다 죽었다. 에러는 LeRobot 3겹 안쪽에서
    `xioctl(VIDIOC_S_FMT) failed, errno=16 Device or resource busy` 로 났다 —
    거기서 원인을 되짚기는 어렵다. 시작 전에 막는다.
    """
    from app.core.config import settings
    from app.services.camera_config import check_camera_config

    legacy = {"top": {"type": "intelrealsense", "serial_number_or_name": "1"},
              "wrist": {"type": "opencv", "index_or_path": "/dev/video0"}}
    shm_cfg = {"top": {"type": "shm", "segment": "rs_1_color"}}

    monkeypatch.setattr(settings, "camera_transport", "shm")
    err = check_camera_config(legacy)
    assert err and "top" in err and "wrist" in err, "장치 직접 열기를 못 잡는다"
    assert "새로고침" in err, "사용자가 무엇을 해야 하는지 안 알려준다"
    assert check_camera_config(shm_cfg) is None
    assert check_camera_config({}) is None

    # direct 에서는 당연히 정상 설정이다
    monkeypatch.setattr(settings, "camera_transport", "direct")
    assert check_camera_config(legacy) is None


def test_recording_start_checks_before_launching():
    """프로세스를 띄운 뒤 검사하면 팔·카메라를 다 잡은 뒤에 죽는다."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend" / "app" / "routers" / "recording.py").read_text()
    start = src.split('@router.post("/start")', 1)[1].split("@router.", 1)[0]
    assert "check_camera_config" in start, "녹화 시작이 카메라 설정을 검사하지 않는다"
    assert start.index("check_camera_config") < start.index("record_manager.start"), (
        "프로세스를 띄운 뒤에 검사한다"
    )


def test_requested_profile_reaches_the_daemon():
    """**회귀** — UI 에서 고른 해상도·fps 가 데몬까지 가지 않았다.

    `prepare_cameras` 가 `cam.connect()` 를 인자 없이 불러서, 데몬은 늘
    librealsense 기본 프로파일로 열었다. D405 는 그게 848x480@**10** 이라
    녹화 루프가 10Hz 에 묶였고 매 프레임 "루프가 느리다" 경고가 떴다.
    (D405 에는 848x480@15 자체가 없다 — 10 이 상한이다.)
    """
    import ast
    import inspect

    from app.services import camera_config
    from app.services.camera_manager import CameraInfo

    sig = inspect.signature(camera_config.prepare_cameras).parameters
    assert {"width", "height", "fps"} <= set(sig), "요청 프로파일을 받지 않는다"
    assert {"width", "height", "fps"} <= set(inspect.signature(CameraInfo.connect).parameters)

    # 받기만 하고 안 넘기면 소용없다 — 실제로 인자를 실어 부르는지 본다
    src = inspect.getsource(camera_config.prepare_cameras)
    connect_args = [
        len(n.args) + len(n.keywords)
        for n in ast.walk(ast.parse(src.lstrip()))
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "cam.connect"
    ]
    assert connect_args and all(a >= 3 for a in connect_args), (
        f"cam.connect 가 프로파일 없이 불린다: {connect_args}"
    )


def test_dataset_fps_comes_from_the_device_not_the_request():
    """데이터셋에 박는 fps 는 **장치가 실제로 여는 값**이어야 한다.

    요청값을 그대로 쓰면 15fps 라고 적어놓고 10fps 로 채운 데이터셋이 나온다 —
    LeRobot 이 매 프레임 경고를 뱉고 타임스탬프도 어긋난다.
    """
    from app.core.config import settings
    from app.services import camera_config
    from app.services.camera_manager import camera_manager

    from piper_shm import segment_for_camera

    cam_id = "/dev/video-pytest-fps"
    pub = Publisher(segment_for_camera(cam_id), 640, 480)

    class _FakeCam:
        connected = True

        def running_profile(self):
            return {"connected": True, "want": [640, 480, 15],
                    "width": 640, "height": 480, "fps": 10}   # 장치는 10 밖에 못 낸다

    camera_manager.cameras[cam_id] = _FakeCam()
    try:
        settings.camera_transport = "shm"
        cfg = camera_config.build_cameras_json({"hand": cam_id}, width=640, height=480, fps=15)
        assert cfg["hand"]["fps"] == 10, "요청값(15)을 그대로 실어 데이터셋이 거짓말한다"
    finally:
        settings.camera_transport = "direct"
        camera_manager.cameras.pop(cam_id, None)
        pub.close()


def test_publisher_notices_its_segment_was_deleted():
    """**회귀** — 발행자가 unlink 된 파일에 계속 쓰고 있었다.

    실기 증상: D435 가 `connect` 에 `(True, "OK")` 를 돌려주고 `info` 도
    `connected: True` 인데 세그먼트가 없어서, 추론이 시작 직후
    `SegmentError: 세그먼트가 없습니다` 로 죽었다.

    원인은 unlink 된 뒤에도 **열린 fd 로는 계속 써진다**는 것이다. 발행자는 멀쩡히
    돌고 에러도 로그도 안 남는다. 누가 지웠는지와 무관하게 살아나야 한다.
    """
    import numpy as np

    pub = Publisher(_NAME, 8, 8)
    try:
        pub.publish(np.zeros((8, 8, 3), dtype=np.uint8))
        assert not pub.orphaned

        unlink(_NAME)                      # 누군가 지운다
        assert pub.orphaned, "지워진 것을 모른다 — 조용히 깨지는 상태다"
        # 지워졌어도 쓰기는 여전히 성공한다. 그래서 예외로는 못 잡는다.
        pub.publish(np.zeros((8, 8, 3), dtype=np.uint8))
    finally:
        pub.close()


def test_publish_frame_recreates_an_orphaned_segment():
    """발행 경로가 스스로 되살려야 한다 — 재시작 없이."""
    import numpy as np

    pytest.importorskip("piper_rs")
    from piper_rs import publish as P
    from piper_shm import segment_for_camera

    cam_id = "/dev/video-pytest-orphan"
    name = segment_for_camera(cam_id)
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    try:
        assert P.publish_frame(cam_id, frame)
        assert name in list_segments()

        unlink(name)                       # 누군가 지운다
        assert name not in list_segments()

        assert P.publish_frame(cam_id, frame), "발행이 실패했다"
        assert name in list_segments(), "지워진 세그먼트를 다시 만들지 않는다"
    finally:
        P.stop(cam_id)
        pass


def test_container_privileges_and_transport_stay_in_sync():
    """**둘 중 하나만 바꾸면 조용히 안 되는 조합이 된다.**

    컨테이너에서 `privileged`/`/dev` 를 뺀 근거는 "이 프로세스가 장치를 안 연다"이고,
    그건 두 전송이 `shm` 일 때만 참이다. `direct` 로 되돌리면 장치를 열려다
    권한이 없어 죽는데, 원인이 compose 파일에 있어 찾기 어렵다.
    """
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    backend = compose.split("frontend:")[0]

    privileged = "privileged: true" in backend
    devices = "- /dev:/dev" in backend
    shm_cams = "PIPER_CAMERA_TRANSPORT=shm" in backend
    shm_robot = "PIPER_ROBOT_TRANSPORT=shm" in backend

    assert not (privileged or devices) == (shm_cams and shm_robot), (
        f"권한(privileged={privileged}, /dev={devices})과 "
        f"전송(camera={shm_cams}, robot={shm_robot})이 어긋난다"
    )
    # 세그먼트를 호스트 데몬과 공유하지 못하면 shm 전송 자체가 성립하지 않는다
    if shm_cams or shm_robot:
        assert "ipc: host" in backend, "ipc: host 없이는 /dev/shm 이 컨테이너마다 격리된다"


def test_reconfiguring_a_pipeline_keeps_the_segments():
    """**회귀** — 스캔이 살아 있는 카메라의 세그먼트를 지웠다.

    스캔은 스트림마다 probe 를 부른다. `color` 가 연결된 상태에서 `depth` 를
    probe 하면 스트림 집합이 바뀌어 파이프라인을 접는데, 그때 살아 있던 color
    세그먼트까지 지웠다. 이어지는 `pipeline.start` 가 실패하면(카메라 2대가 USB
    대역폭을 나눠 쓸 때 일어난다) **지워진 채로 남는다** — D435 가 이렇게 사라졌다.

    잠깐 접었다 펴는 것과 장치를 놓는 것은 다르다. 놓을 때만 지워야 한다.
    """
    import ast
    import importlib.util
    from pathlib import Path

    pytest.importorskip("piper_rs")
    src = Path(importlib.util.find_spec("piper_rs.hub").origin).read_text()
    tree = ast.parse(src)

    def stop_calls(fn_name):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and ast.unparse(n.func).endswith("_stop_pipeline")]

    # 재구성 — 세그먼트를 남겨야 한다
    for call in stop_calls("_ensure_streams"):
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert kw.get("unlink_segments") == "False", (
            "재구성이 세그먼트를 지운다 — 소비자가 그 순간 죽고, "
            "start 가 실패하면 지워진 채로 남는다"
        )

    # 진짜로 놓는 자리 — 여기서는 지워야 한다 (안 지우면 /dev/shm 누수)
    for fn in ("disconnect_stream", "force_release"):
        calls = stop_calls(fn)
        assert calls, f"{fn} 이 파이프라인을 안 멈춘다"
        assert any(not c.keywords for c in calls), f"{fn} 이 세그먼트를 안 치운다"
