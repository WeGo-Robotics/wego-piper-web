"""카메라 이름 — 옛 별칭(`label`)이 **스트림 이름**으로 합쳐졌다.

규칙·기본 이름·유일성·마이그레이션은 `test_camera_streams.py` 가 본다. 여기는
**별칭이 있던 자리**(표시명·드롭다운·등록 사유)가 새 이름으로 계속 돌아가는지 본다.

## 이름은 LeRobot 카메라 키가 **아니다**

데이터셋 피처는 `observation.images.<키>` 로 굳고 정책도 그 키로 학습된다.
이름이 곧 키가 되면 카메라 이름을 고치는 순간 **이미 학습된 정책이 안 열린다.**
그래서 이름은 `{키: 이름}` 매핑의 오른쪽에만 있다. 키는 녹화·추론 페이지에서 따로 정한다.
"""

import json
import re
from pathlib import Path

import pytest

from app.services.camera_manager import CameraInfo, CameraManager

_REPO = Path(__file__).resolve().parents[2]


def _cam(**kw) -> CameraInfo:
    return CameraInfo(id=kw.pop("id", "/dev/video0"), name=kw.pop("name", "HD Webcam"), **kw)


def test_display_name_prefers_the_stream_name():
    assert _cam(stream="top").to_dict()["display_name"] == "top"


def test_display_name_falls_back_to_hardware_name():
    """등록 전에는 이름이 없으니 하드웨어 이름. 각 화면이 `stream || name` 을 따로 적으면 규칙이 갈린다."""
    assert _cam().to_dict()["display_name"] == "HD Webcam"


def test_label_is_a_read_only_alias_of_the_stream_name():
    """`cam.label or cam.name` 으로 표시명을 만들던 호출부가 그대로 돈다. 쓰기는 막는다 —
    대입이 되면 이름 규칙·유일성 검사를 우회한다."""
    cam = _cam(stream="wrist")
    assert cam.label == "wrist"
    assert cam.to_dict()["label"] == cam.to_dict()["stream"] == "wrist"
    with pytest.raises(AttributeError):
        cam.label = "x"


def test_register_accepts_a_name():
    m = CameraManager()
    cam = _cam(id="rs:1:color")
    cam.connected = True                      # connect 우회
    m.cameras[cam.id] = cam

    assert m.register_camera("rs:1:color", "  Top_View  ")[0] is True
    assert cam.stream == "Top_View", "앞뒤 공백을 안 다듬었다"
    assert cam.ready is True


def test_register_without_a_name_keeps_the_existing_one():
    """이름을 안 주고 재등록했다고 사람이 정한 이름이 기본값으로 돌아가면 안 된다."""
    m = CameraManager()
    cam = _cam(id="rs:1:color", stream="wrist")
    cam.connected = True
    m.cameras[cam.id] = cam

    m.register_camera("rs:1:color")           # name=None
    assert cam.stream == "wrist"


def test_label_is_not_the_lerobot_camera_key():
    """**계약** — 별칭이 `--cameras` JSON 의 키로 새어들면 안 된다.

    키는 호출부가 준 `camera_mapping` 에서만 온다. 여기가 흔들리면
    "카메라 이름을 바꿨더니 학습된 정책이 안 열린다"가 된다.
    """
    from app.services.camera_config import build_cameras_json

    built = build_cameras_json({"top": "/dev/video0"})
    assert list(built) == ["top"]


def test_dropdowns_show_the_stream_name_and_use_the_shared_picker():
    """드롭다운이 이름을 안 보여주면 이 기능의 목적(고를 때 알아보기)이 사라진다.
    수집·추론이 **같은 선택기**를 써야 한 쪽만 규칙이 갈리지 않는다."""
    types = (_REPO / "frontend" / "src" / "types" / "camera.ts").read_text()
    assert "streamOptionText" in types and "s.stream" in types

    picker = (_REPO / "frontend" / "src" / "components" / "CameraStreamPicker.tsx").read_text()
    assert "disabled" in picker and "s.selectable" in picker, "연결 안 된 것을 비활성으로 두지 않는다"

    for page in ("RecordingPage", "InferencePage"):
        src = (_REPO / "frontend" / "src" / "pages" / f"{page}.tsx").read_text()
        assert "CameraStreamSelect" in src, f"{page} 가 공용 선택기를 안 쓴다"
        assert "IdleStreams" in src, f"{page} 에 연결 안 된 카메라를 연결할 자리가 없다"
        assert "/cameras/ready" not in src, f"{page} 가 아직 장치 목록으로 고른다"


def test_encoder_probe_still_lists_devices_by_the_shared_type():
    """인코더 프로브는 장치 id 로 카메라를 고른다(프레임을 직접 읽는 도구) — 옛 드롭다운 유지."""
    shared = (_REPO / "frontend" / "src" / "types" / "camera.ts").read_text()
    assert "camOptionText" in shared and "c.stream" in shared


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


def _fake_cam(cam_id: str, why: str):
    """connect 가 **사유와 함께** 실패하는 카메라. camerad 가 그렇게 답한다."""
    class _C:
        id = cam_id
        name = "HD Webcam"
        connected = False
        ready = False
        stream = ""
        aliases: list = []

        def connect(self):
            return False, why

    return _C()


def test_a_failed_register_says_why_not_just_that_it_failed():
    """⚠ 현장(2026-09-22, .120 을 들고 나갔을 때): 스캔은 되는데 등록만 안 됐고 화면은
    "등록 실패" 한 줄이었다. 진짜 사유는 camerad 가 이미 말하고 있었는데
    (`Cannot open /dev/video0` 같은) 게이트웨이가 `ok, _ = cam.connect()` 로 **버렸다.**

    사유 없는 실패는 고칠 수 없는 실패다 — 장치를 못 여는 이유는 매번 다르다.
    """
    from app.services.camera_manager import CameraManager

    m = CameraManager()
    m.cameras["/dev/video0"] = _fake_cam("/dev/video0", "Cannot open /dev/video0")
    ok, why = m.register_camera("/dev/video0")
    assert ok is False
    assert "Cannot open /dev/video0" in why, f"사유가 사라졌다: {why!r}"


def test_an_unknown_camera_is_not_called_a_connection_problem():
    from app.services.camera_manager import CameraManager

    ok, why = CameraManager().register_camera("/dev/nope")
    assert ok is False and "/dev/nope" in why
