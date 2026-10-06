"""캡처 해상도 · 출력 해상도 — camerad·rsd 공통 (2026-10-06).

AR0234(Global Shutter) 카메라는 640×480 을 센서 가운데 1/3 을 **잘라** 만든다. 넓은 모드로
받아(캡처) 데이터셋 크기로 줄이면(출력) 화각은 넓고 크기는 같다. 출력이 비면 **원본 해상도**.

여기서 지키는 것: 줄이는 계산이 두 데몬에서 같다 · 늘이지 않고 잘라 맞춘다 · 깊이는 최근접 ·
**intrinsics 가 프레임과 같이 바뀐다**(안 바뀌면 정렬 검사의 mm 답이 조용히 틀린다) ·
rsd 의 "이미 반영한 요청" 비교가 캡처 때문에 깨지지 않는다 · 모드 목록은 장치가 말한 것.
"""

from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]


# ── 공용 계산 (piper_cam/fit.py) ──


def test_a_matching_aspect_is_a_plain_shrink_that_keeps_the_edges():
    from piper_cam.fit import fit_frame

    f = np.zeros((960, 1280, 3), np.uint8)
    f[:, :8] = 255
    out = fit_frame(f, (640, 480))
    assert out.shape[:2] == (480, 640)
    assert out[:, :2].mean() > 100, "가장자리가 사라졌다 — 줄인 게 아니라 잘랐다"


def test_a_different_aspect_is_cropped_to_fit_never_stretched():
    """1920×1200(16:10) → 640×480(4:3): 가운데를 4:3 으로 자른 뒤 줄인다."""
    from piper_cam.fit import fit_box, fit_frame

    assert fit_box(1920, 1200, 640, 480)[:4] == (160, 0, 1600, 1200)
    f = np.zeros((1200, 1920, 3), np.uint8)
    f[:, :100] = 255                                   # 잘려 나갈 띠
    assert fit_frame(f, (640, 480))[:, :4].mean() < 5, "자르지 않고 늘였다"


def test_original_size_passes_the_frame_through_untouched():
    from piper_cam.fit import fit_frame

    f = np.zeros((480, 640, 3), np.uint8)
    assert fit_frame(f, None) is f, "원본 해상도인데 손을 댔다"
    assert fit_frame(f, (640, 480)) is f


def test_depth_is_shrunk_by_nearest_so_no_distance_is_invented():
    """면적 평균은 물체 경계에서 앞(300mm)과 뒤(1000mm)를 섞어 **없는 거리**를 만든다."""
    from piper_cam.fit import fit_frame

    d = np.full((960, 1280), 1000, np.uint16)
    d[:, :641] = 300
    out = fit_frame(d, (640, 480), nearest=True)
    assert set(np.unique(out)) <= {300, 1000}, f"없는 거리가 생겼다: {np.unique(out)}"


def test_intrinsics_follow_the_same_crop_and_scale():
    """⚠ 프레임만 줄이고 내부 파라미터를 그대로 두면 정렬 검사의 mm 답이 조용히 틀린다.
    1920×1200 → 640×480: 좌우 160px 잘림 + 0.4배. 주점은 잘린 만큼 옮긴 뒤 배율."""
    from piper_cam.fit import fit_intrinsics

    raw = {"fx": 1000.0, "fy": 1000.0, "cx": 960.0, "cy": 600.0,
           "width": 1920, "height": 1200, "model": "x", "coeffs": [0.1, 0, 0, 0, 0]}
    got = fit_intrinsics(raw, (640, 480))
    assert (got["width"], got["height"]) == (640, 480)
    assert got["fx"] == pytest.approx(400.0) and got["fy"] == pytest.approx(400.0)
    assert got["cx"] == pytest.approx((960 - 160) * 0.4)       # = 320, 여전히 가운데
    assert got["cy"] == pytest.approx(600 * 0.4)
    assert got["coeffs"] == raw["coeffs"], "왜곡 계수는 정규화 좌표 값이라 그대로다"
    assert fit_intrinsics(raw, None) is raw


# ── rsd ──


def _rs_device():
    from piper_rs.hub import _RSDevice

    return _RSDevice("123", "D435", "2-1", {"color", "depth"})


def test_rsd_asks_the_device_for_the_capture_size_but_remembers_the_callers_request():
    """⚠ `info()["want"]` 는 게이트웨이가 "이 요청을 이미 반영했나" 를 보는 값이다.
    캡처로 덮어 버리면 매번 다르다고 보고 **재연결**한다(rsd 는 refcount 만 는다)."""
    dev = _rs_device()
    dev._want["color"] = (640, 480, 30)
    dev._capture["color"] = (1280, 720)
    assert dev._request("color") == (1280, 720, 30), "장치엔 캡처 크기로 요청해야 한다"
    assert dev._want["color"] == (640, 480, 30), "호출자의 요청을 덮었다"
    dev._capture.pop("color")
    assert dev._request("color") == (640, 480, 30)


def test_rsd_reports_the_published_size_and_fits_the_intrinsics():
    src = (REPO / "rs" / "piper_rs" / "hub.py").read_text()
    info = src.split("    def info(self, cam_id", 1)[1].split("\n    def ", 1)[0]
    assert "dev._output.get(stream) or (got[0], got[1])" in info, \
        "발행 크기가 아니라 캡처 크기를 알린다 — 녹화가 엉뚱한 크기를 기다린다"
    intr = src.split("def _intrinsics_impl", 1)[1].split("\n    def ", 1)[0]
    assert "fit_intrinsics(raw, dev._output.get(stream))" in intr
    loop = src.split("if updates and self._output:", 1)[1][:400]
    assert 'nearest=(k == "depth")' in loop, "깊이를 면적 평균으로 줄인다"


def test_both_daemons_expose_the_modes_verb_and_share_one_fit():
    for daemon in ("camerad.py", "rsd.py"):
        methods = (REPO / "daemons" / daemon).read_text().split("_METHODS = {", 1)[1].split("}", 1)[0]
        assert '"modes"' in methods, f"{daemon} 가 모드 목록을 안 노출한다"
    for hub in (REPO / "cam" / "piper_cam" / "hub.py", REPO / "rs" / "piper_rs" / "hub.py"):
        assert "from piper_cam.fit import" in hub.read_text(), f"{hub.name} 가 계산을 따로 한다"


# ── 장치에 직접 묻는다 ──


@pytest.mark.skipif(not Path("/dev/video0").exists(), reason="V4L2 장치 없음")
def test_v4l2_modes_come_from_the_driver():
    """OpenCV 는 모드를 모른다 — 드라이버 열거 ioctl 로 묻는다. 지어낸 목록은
    "목록에 있는데 안 열린다" 가 된다."""
    import glob

    from piper_cam.v4l2 import list_modes

    found = [m for dev in sorted(glob.glob("/dev/video*")) for m in list_modes(dev)]
    assert found, "어느 장치에서도 모드를 못 읽었다"
    assert all(m["width"] > 0 and m["height"] > 0 for m in found)


# ── 게이트웨이 ──


def test_the_gateway_saves_both_and_reopens_an_open_camera(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.camera_manager import CameraInfo, camera_manager

    calls = []

    class _Hub:
        def connect(self, cid, w, h, fps, controls, capture=None, output=None):
            calls.append((capture, output)); return True, "OK"

        def info(self, cid):
            return {"want": [640, 480, 30]}

        def modes(self, cid):
            return [{"width": 1280, "height": 960, "fps": 120}]

        def __getattr__(self, name):            # to_dict 가 묻는 나머지(has_frame 등)
            return lambda *a, **k: False

    cam = CameraInfo(id="/dev/video99", name="t")
    cam.connected = True
    monkeypatch.setattr(CameraInfo, "_hub", property(lambda self: _Hub()))
    monkeypatch.setattr(camera_manager, "save_session", lambda: None)

    with TestClient(app) as c:
        # 기동이 세션을 복원하며 목록을 새로 만든다 — 그 **뒤에** 넣는다
        monkeypatch.setitem(camera_manager.cameras, cam.id, cam)
        assert c.get("/api/cameras/modes", params={"id": cam.id}).json()["modes"][0]["width"] == 1280
        r = c.post("/api/cameras/resolution",
                   json={"id": cam.id, "capture": "1280x960", "output": [640, 480]})
        assert r.status_code == 200, r.text
        assert r.json()["capture"] == [1280, 960] and r.json()["output"] == [640, 480]
        assert calls[-1] == ([1280, 960], [640, 480]), "열려 있는데 다시 안 열었다"
        r = c.post("/api/cameras/resolution", json={"id": cam.id, "capture": None, "output": None})
        assert r.json()["output"] is None, "비우면 원본 해상도여야 한다"
        assert c.post("/api/cameras/resolution",
                      json={"id": cam.id, "output": "wide"}).status_code == 400
