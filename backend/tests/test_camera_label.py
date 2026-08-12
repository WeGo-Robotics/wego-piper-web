"""카메라 별칭 — "탑뷰"인지 "손목"인지 화면에서 바로 알아보기 위한 이름.

## 별칭은 LeRobot 카메라 키가 **아니다**

데이터셋 피처는 `observation.images.<키>` 로 굳고 정책도 그 키로 학습된다.
별칭이 곧 키가 되면 카메라 이름을 고치는 순간 **이미 학습된 정책이 안 열린다.**
그래서 별칭은 표시 전용이고, 키는 녹화·추론 페이지에서 따로 정한다.
대신 그 드롭다운이 별칭을 보여줘서 고르는 순간에 알 수 있게 한다.
"""

import json
import re
from pathlib import Path

from app.services.camera_manager import CameraInfo, CameraManager

_REPO = Path(__file__).resolve().parents[2]


def _cam(**kw) -> CameraInfo:
    return CameraInfo(id=kw.pop("id", "/dev/video0"), name=kw.pop("name", "HD Webcam"), **kw)


def test_display_name_prefers_label():
    assert _cam(label="탑뷰").to_dict()["display_name"] == "탑뷰"


def test_display_name_falls_back_to_hardware_name():
    """별칭이 없으면 하드웨어 이름. 각 화면이 `label || name` 을 따로 적으면 규칙이 갈린다."""
    assert _cam().to_dict()["display_name"] == "HD Webcam"
    assert _cam(label="  ").to_dict()["label"] == "  "   # 저장은 그대로 (설정 시 strip)


def test_register_accepts_a_label():
    m = CameraManager()
    cam = _cam(id="rs:1:color")
    cam.connected = True                      # connect 우회
    m.cameras[cam.id] = cam

    assert m.register_camera("rs:1:color", "  탑뷰  ") is True
    assert cam.label == "탑뷰", "앞뒤 공백을 안 다듬었다"
    assert cam.ready is True


def test_register_without_label_keeps_existing():
    """이름을 안 주고 재등록했다고 기존 별칭이 지워지면 안 된다."""
    m = CameraManager()
    cam = _cam(id="rs:1:color", label="손목")
    cam.connected = True
    m.cameras[cam.id] = cam

    m.register_camera("rs:1:color")           # label=None
    assert cam.label == "손목"


def test_set_label_can_clear():
    m = CameraManager()
    cam = _cam(label="탑뷰")
    m.cameras[cam.id] = cam

    assert m.set_label(cam.id, "") is True
    assert cam.label == ""
    assert cam.to_dict()["display_name"] == "HD Webcam"


def test_set_label_unknown_camera():
    assert CameraManager().set_label("/dev/nope", "x") is False


def test_label_survives_session_roundtrip(tmp_path, monkeypatch):
    """카메라를 옮겨 달 때까지 이름이 유지돼야 한다 — 재시작마다 다시 붙이면 안 쓴다."""
    m = CameraManager()
    cam = _cam(id="rs:1:color", label="탑뷰", cam_type="realsense")
    cam.ready = True
    m.cameras[cam.id] = cam

    path = tmp_path / "camera_session.json"
    monkeypatch.setattr(CameraManager, "CAMERA_SESSION_PATH", path)
    m.save_session()
    assert json.loads(path.read_text())[0]["label"] == "탑뷰"

    # 새 매니저가 스캔했다고 치고 복원
    m2 = CameraManager()
    fresh = _cam(id="rs:1:color", cam_type="realsense")
    monkeypatch.setattr(CameraManager, "scan", lambda self: m2.cameras.update({fresh.id: fresh}))
    assert m2.restore_session() is True
    assert m2.cameras["rs:1:color"].label == "탑뷰"


def test_label_is_not_the_lerobot_camera_key():
    """**계약** — 별칭이 `--cameras` JSON 의 키로 새어들면 안 된다.

    키는 호출부가 준 `camera_mapping` 에서만 온다. 여기가 흔들리면
    "카메라 이름을 바꿨더니 학습된 정책이 안 열린다"가 된다.
    """
    from app.routers.models import _build_cameras_json

    built = _build_cameras_json({"top": "/dev/video0"})
    assert list(built) == ["top"]


def test_dropdowns_show_the_label():
    """드롭다운이 별칭을 안 보여주면 이 기능의 목적(고를 때 알아보기)이 사라진다."""
    shared = (_REPO / "frontend" / "src" / "types" / "camera.ts").read_text()
    assert "camOptionText" in shared and "c.label" in shared

    for page in ("RecordingPage", "InferencePage"):
        src = (_REPO / "frontend" / "src" / "pages" / f"{page}.tsx").read_text()
        assert "camOptionText" in src, f"{page} 가 별칭을 안 보여준다"


def test_ready_cam_type_is_declared_once():
    """예전엔 같은 타입이 세 페이지에 복사돼 있었다 — 필드 하나 늘리려면 세 곳을 고쳐야 했다."""
    hits = [
        p.name for p in (_REPO / "frontend" / "src").rglob("*.tsx")
        if re.search(r"^type ReadyCam\s*=", p.read_text(), re.M)
    ]
    assert not hits, f"ReadyCam 이 페이지에 또 정의돼 있다: {hits}"


def test_settings_open_in_a_modal_not_inside_the_card():
    """**회귀** — 카드 안에서 설정을 펼치면 그리드 행 높이가 늘어나 옆 카드까지 커진다.

    등록·미등록 두 카드가 **같은 모달**을 열어야 화면이 흔들리지 않고
    설정 UI 가 두 벌로 갈라지지도 않는다.
    """
    src = (_REPO / "frontend" / "src" / "pages" / "CamerasPage.tsx").read_text()
    assert "expandedCam" not in src and "isExpanded" not in src, (
        "카드 안에서 펼치는 코드가 남아 있다"
    )
    assert src.count("openSettings(cam.id)") == 2, "두 카드가 같은 모달을 열어야 한다"
    assert "fixed inset-0" in src, "모달 오버레이가 없다"
