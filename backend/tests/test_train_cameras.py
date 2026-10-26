"""학습 카메라 대조 · 요청 해상도 유지 (feature/camera-resolution.md).

실기 사고(2026-10-26): 640×480 으로 학습한 SmolVLA 가 사과를 엇나간 자리로 집으러 갔다. 금요일에 rsd 가
USB 사고로 죽었다 살아나며 요청 해상도를 잊어 D435 1280×720 · D405 848×480(기본값)으로 열렸고, 화면은
그걸 드롭다운 글자 안에만 보였다. 검증의 해상도 경고는 config 입력 크기(256×256)와 카메라 요청값(None)을
비교해서 한 번도 울리지 않았다.

여기서 지키는 것:
1. 학습 해상도는 config 가 아니라 **학습 데이터셋**에서 온다 — 데이터셋이 사라져도 표식으로 남는다
2. 비율이 다르면 막고, 크기만 다르면 경고한다. D405 848×480 은 16:9 로 본다
3. 명시해 연 해상도는 세션에 남고, 해상도 없이 다시 열면 그 값으로 연다 — 데몬이 재시작해도
"""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.services import train_cameras as T


def _dataset(root: Path, repo: str, cams: dict[str, tuple[int, int]], side: dict | None = None) -> Path:
    ds = root / repo
    (ds / "meta").mkdir(parents=True)
    feats = {f"observation.images.{n}": {"dtype": "video", "shape": [h, w, 3],
                                          "names": ["height", "width", "channels"],
                                          "info": {"video.fps": 15}}
             for n, (w, h) in cams.items()}
    feats["observation.state"] = {"dtype": "float32", "shape": [7]}
    (ds / "meta" / "info.json").write_text(json.dumps({"fps": 15, "features": feats}))
    if side is not None:
        (ds / "meta" / T.SIDECAR).write_text(json.dumps({"cameras": side}))
    return ds


def _ckpt(root: Path, repo: str | None) -> Path:
    c = root / "run" / "checkpoints" / "last" / "pretrained_model"
    c.mkdir(parents=True)
    if repo is not None:
        (c / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": repo, "root": None}}))
    return c


@pytest.fixture()
def lerobot_home(tmp_path, monkeypatch):
    from app.core.config import settings

    home = tmp_path / "lerobot"
    monkeypatch.setattr(settings, "lerobot_dir", home)
    return home


# ── 1. 학습 해상도는 데이터셋에서 ──────────────────────────────────────────

def test_sizes_come_from_the_dataset_video_and_the_rest_from_the_sidecar(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple", {"top": (640, 480), "hand": (640, 480)}, side={
        "top": {"width": 640, "height": 480, "fps": 15, "id": "rs:9:color", "stream": "top",
                "model": "D435I Color", "capture_width": 1280, "capture_height": 960},
    })
    info = T.resolve(_ckpt(tmp_path, "org/apple"))
    assert info["source"] == "dataset" and info["dataset"] == "org/apple"
    top, hand = info["cameras"]["top"], info["cameras"]["hand"]
    assert (top["width"], top["height"], top["fps"]) == (640, 480, 15)
    assert top["model"] == "D435I Color" and (top["capture_width"], top["capture_height"]) == (1280, 960)
    assert (hand["width"], hand["height"]) == (640, 480) and "model" not in hand


def test_the_config_input_size_is_not_the_training_resolution(tmp_path, lerobot_home):
    """SmolVLA config 의 256×256 은 베이스 모델에서 물려받은 값이다 — 근거로 쓰면 안 된다."""
    _dataset(lerobot_home, "org/apple", {"top": (640, 480)})
    ck = _ckpt(tmp_path, "org/apple")
    (ck / "config.json").write_text(json.dumps({"input_features": {
        "observation.images.camera1": {"type": "VISUAL", "shape": [3, 256, 256]}}}))
    assert T.resolve(ck)["cameras"]["top"]["width"] == 640


def test_unknown_without_a_dataset(tmp_path, lerobot_home):
    hub = tmp_path / "hub"
    hub.mkdir()
    assert T.resolve(hub) == {"cameras": {}, "dataset": None, "source": "none"}
    assert T.resolve(_ckpt(tmp_path / "b", "org/gone"))["source"] == "none"


def test_the_marker_outlives_the_dataset(tmp_path, lerobot_home):
    ds = _dataset(lerobot_home, "org/apple", {"top": (640, 480)})
    ck = _ckpt(tmp_path, "org/apple")
    assert T.remember(ck, T.resolve(ck))
    shutil.rmtree(ds)
    again = T.resolve(ck)
    assert again["source"] == "marker" and again["cameras"]["top"]["width"] == 640


def test_markers_are_not_written_into_the_hub_cache(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple", {"top": (640, 480)})
    snap = tmp_path / "models--org--m" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": "org/apple"}}))
    assert not T.remember(snap, T.resolve(snap))
    assert not (snap / T.CKPT_MARKER).exists()


def test_old_realsense_datasets_borrow_the_model_name_from_the_registered_device(tmp_path, lerobot_home,
                                                                                 monkeypatch):
    """기종을 남기기 전 데이터셋 — RealSense 는 id 가 시리얼이라 같은 장치다. v4l2 번호는 짐작하지 않는다."""
    from app.services.camera_manager import CameraInfo, camera_manager

    _dataset(lerobot_home, "org/old", {"top": (640, 480), "side": (640, 480)}, side={
        "top": {"id": "rs:9:color"}, "side": {"id": "/dev/video0"}})
    monkeypatch.setattr(camera_manager, "cameras", {
        "rs:9:color": CameraInfo(id="rs:9:color", name="D435I Color"),
        "/dev/video0": CameraInfo(id="/dev/video0", name="HD Webcam")})
    cams = T.resolve(_ckpt(tmp_path, "org/old"))["cameras"]
    assert cams["top"]["model"] == "D435I Color" and "model" not in cams["side"]


# ── 2. 대조 ─────────────────────────────────────────────────────────────

TRAIN = {"top": {"width": 640, "height": 480, "fps": 15, "model": "D435I Color"},
         "hand": {"width": 640, "height": 480, "fps": 15, "model": "D405 Color"}}


def test_a_different_aspect_blocks_and_names_both_sizes():
    """2026-10-26 의 그 조합 — 4:3 으로 학습, 16:9 로 추론."""
    rows = T.compare(TRAIN, {"top": {"width": 1280, "height": 720, "model": "D435I Color"},
                             "hand": {"width": 848, "height": 480, "model": "D405 Color"}})
    assert rows["top"]["status"] == rows["hand"]["status"] == T.ASPECT
    errors, warnings = T.messages(rows)
    assert len(errors) == 2 and not warnings
    hand, top = errors                                     # 이름순
    assert "학습 640x480" in hand and "지금 848x480" in hand
    assert "학습 640x480" in top and "지금 1280x720" in top


def test_the_same_aspect_at_another_size_only_warns():
    rows = T.compare(TRAIN, {"top": {"width": 1280, "height": 960}, "hand": {"width": 640, "height": 480}})
    assert rows["top"]["status"] == T.SIZE and rows["hand"]["status"] == T.OK
    errors, warnings = T.messages(rows)
    assert not errors and len(warnings) == 1 and "실행은 됩니다" in warnings[0]


def test_d405_848x480_counts_as_16_9():
    rows = T.compare({"hand": {"width": 1280, "height": 720}}, {"hand": {"width": 848, "height": 480}})
    assert rows["hand"]["status"] == T.SIZE


def test_another_camera_model_warns_but_does_not_block():
    rows = T.compare(TRAIN, {"top": {"width": 640, "height": 480, "model": "USB Camera: USB Camera"},
                             "hand": {"width": 640, "height": 480, "model": "D405 Color"}})
    errors, warnings = T.messages(rows)
    assert not errors and len(warnings) == 1 and "기종" in warnings[0]
    assert rows["top"]["model_differs"] and not rows["hand"]["model_differs"]


def test_nothing_to_say_when_either_side_is_unknown():
    rows = T.compare({}, {"top": {"width": 1280, "height": 720}})
    assert rows["top"]["status"] == T.UNKNOWN
    rows = T.compare(TRAIN, {"top": {"model": "D435I Color"}})
    assert rows["top"]["status"] == T.NO_LIVE and rows["hand"]["status"] == T.NO_LIVE
    assert T.messages(rows) == ([], [])


def test_match_plan_reproduces_a_widened_capture():
    assert T.match_plan({"width": 640, "height": 480, "fps": 15}) == ((640, 480, 15), None, None)
    assert T.match_plan({"width": 640, "height": 480, "fps": 15, "capture_width": 1280,
                         "capture_height": 960}) == ((640, 480, 15), [1280, 960], [640, 480])


def test_validate_compares_training_with_what_is_published_now(tmp_path, lerobot_home, monkeypatch):
    """옛 검사는 config(256×256)와 요청값(None)을 봐서 울리지 않았다 — 이제 학습 데이터셋과 세그먼트 크기다."""
    from app.routers import inference as I

    _dataset(lerobot_home, "org/apple", {"top": (640, 480), "hand": (640, 480)})
    ck = _ckpt(tmp_path, "org/apple")
    monkeypatch.setattr(I, "get_model", lambda mid: {"path": str(ck), "requirements": {
        "required_cameras": [{"name": "top", "width": 256, "height": 256},
                             {"name": "hand", "width": 256, "height": 256}],
        "state_dim": 7, "action_dim": 7}})
    monkeypatch.setattr(T, "live_cameras", lambda mapping: {
        "top": {"width": 1280, "height": 720}, "hand": {"width": 640, "height": 480}})
    body = I.ValidateRequest(model_id="m", follower_iface="can0", camera_mapping={"top": "top_1", "hand": "left"})
    out = asyncio.run(I.validate_config(body))
    assert out["cameras"]["top"]["status"] == T.ASPECT and out["cameras"]["hand"]["status"] == T.OK
    assert not out["valid"]
    assert any("화면 비율" in e for e in out["errors"])
    assert not any("256" in w for w in out["warnings"]), "config 입력 크기로 경고하면 안 된다"


# ── 3. 요청 해상도 유지 ────────────────────────────────────────────────────

class _Hub:
    def __init__(self):
        self.calls: list[tuple] = []

    def connect(self, cam_id, width=0, height=0, fps=0, controls=None, capture=None, output=None):
        self.calls.append((width, height, fps, capture, output))
        return True, "OK"


@pytest.fixture()
def rs_cam(tmp_path, monkeypatch):
    from app.services import camera_manager as M

    hub = _Hub()
    monkeypatch.setattr(M, "realsense_hub", hub)
    monkeypatch.setattr(M.CameraManager, "CAMERA_SESSION_PATH", tmp_path / "camera_session.json")
    monkeypatch.setattr(M.CameraInfo, "push_stream", lambda self: True)
    monkeypatch.setattr(M.CameraInfo, "push_idle", lambda self: None)
    cam = M.CameraInfo(id="rs:9:color", name="D435I Color", cam_type="realsense", stream="top_1")
    cam.ready = True
    monkeypatch.setattr(M.camera_manager, "cameras", {cam.id: cam})
    return cam, hub, tmp_path / "camera_session.json"


def test_an_explicit_request_is_saved_and_reused_by_a_bare_reconnect(rs_cam):
    """녹화가 640×480@15 로 연 뒤 데몬이 재시작하면, 다음 [연결]은 해상도 없이 온다 — 그래도 640×480@15."""
    cam, hub, session = rs_cam
    assert cam.connect(640, 480, 15) == (True, "OK")
    saved = json.loads(session.read_text())[0]["config"]
    assert (saved["width"], saved["height"], saved["fps"]) == (640, 480, 15)

    cam.connect()
    assert hub.calls[-1][:3] == (640, 480, 15), "저장된 요청 해상도로 열지 않았다 — 장치 기본값으로 돌아간다"


def test_without_a_saved_request_the_device_default_is_still_used(rs_cam):
    cam, hub, _ = rs_cam
    cam.connect()
    assert hub.calls[-1][:3] == (0, 0, 0)
    assert cam.requested_mode() is None


def test_a_gateway_restart_restores_the_request(rs_cam, monkeypatch):
    from app.services import camera_manager as M

    cam, hub, session = rs_cam
    cam.connect(640, 480, 15)
    fresh = M.CameraInfo(id="rs:9:color", name="D435I Color", cam_type="realsense")
    m = M.CameraManager()
    monkeypatch.setattr(M.CameraManager, "scan", lambda self: self.cameras.update({fresh.id: fresh}))
    assert m.restore_session()
    fresh.connect()
    assert hub.calls[-1][:3] == (640, 480, 15)


def test_the_sidecar_records_the_model_and_the_capture_size(monkeypatch):
    from app.services import camera_config
    from app.services.camera_manager import CameraInfo, camera_manager

    cam = CameraInfo(id="rs:9:color", name="D435I Color", cam_type="realsense", stream="top_1")
    cam.ready = True
    monkeypatch.setattr(camera_manager, "cameras", {cam.id: cam})
    monkeypatch.setattr(CameraInfo, "running_profile", lambda self: {
        "width": 640, "height": 480, "fps": 15, "capture_width": 1280, "capture_height": 960})
    entry = camera_config.camera_sidecar({"top": "top_1"})["cameras"]["top"]
    assert entry["model"] == "D435I Color"
    assert (entry["capture_width"], entry["capture_height"]) == (1280, 960)
