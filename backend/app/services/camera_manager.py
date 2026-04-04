"""
카메라 관리 서비스.
시스템 카메라 스캔, 연결 테스트, 설정, 등록, 프리뷰.
"""

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


@dataclass
class CameraInfo:
    id: str           # "/dev/video0" 또는 인덱스
    name: str         # "HD Webcam" 등
    cam_type: str = "opencv"  # opencv | realsense | zmq
    connected: bool = False
    ready: bool = False
    # 설정값
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    color_mode: str = "rgb"
    rotation: int = 0
    fourcc: str | None = None
    # 내부
    _cap: object = field(default=None, repr=False)
    _last_frame: object = field(default=None, repr=False)  # 최신 프레임 (numpy array)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _capture_thread: threading.Thread | None = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cam_type": self.cam_type,
            "connected": self.connected,
            "ready": self.ready,
            "has_preview": self._last_frame is not None,
            "config": {
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "color_mode": self.color_mode,
                "rotation": self.rotation,
                "fourcc": self.fourcc,
            },
        }

    def update_config(self, cfg: dict) -> None:
        for key in ("width", "height", "fps", "color_mode", "rotation", "fourcc"):
            if key in cfg:
                setattr(self, key, cfg[key])

    def _open_cap(self):
        """VideoCapture를 열고 1프레임 읽기. (내부용)"""
        import cv2
        idx = self.id
        if isinstance(idx, str) and idx.startswith("/dev/video"):
            idx = int(idx.replace("/dev/video", ""))
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            return None, None
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            return None, None
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = int(cap.get(cv2.CAP_PROP_FPS)) or 30
        return cap, frame

    def probe(self) -> tuple[bool, str]:
        """연결 테스트 + 프리뷰 1장 → 즉시 해제. 스캔 시 사용."""
        try:
            import cv2
        except ImportError:
            return False, "opencv-python not installed"
        try:
            cap, frame = self._open_cap()
            if cap is None:
                return False, f"Cannot open {self.id}"
            with self._lock:
                self._last_frame = frame
            cap.release()
            self.connected = False  # probe는 연결 유지 안 함
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def connect(self) -> tuple[bool, str]:
        """연결 + 백그라운드 캡처 시작. 등록 시 사용."""
        try:
            import cv2
        except ImportError:
            return False, "opencv-python not installed"
        try:
            cap, frame = self._open_cap()
            if cap is None:
                return False, f"Cannot open {self.id}"
            self._cap = cap
            with self._lock:
                self._last_frame = frame
            self.connected = True
            self._running = True
            self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._capture_thread.start()
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def disconnect(self) -> None:
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=2)
            self._capture_thread = None
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._last_frame = None
        self.connected = False

    def _capture_loop(self) -> None:
        """백그라운드에서 계속 프레임을 읽어 _last_frame에 저장."""
        while self._running and self._cap:
            try:
                ret, frame = self._cap.read()
                if ret and frame is not None:
                    with self._lock:
                        self._last_frame = frame
            except Exception:
                break

    def capture_preview(self) -> bytes | None:
        """마지막 캡처된 프레임을 JPEG로 즉시 반환."""
        with self._lock:
            frame = self._last_frame
        if frame is None:
            return None
        try:
            import cv2
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes()
        except Exception:
            return None


def scan_cameras() -> list[dict]:
    """시스템 카메라 스캔 (/dev/video* + v4l2-ctl)."""
    results = []
    seen_names: dict[str, int] = {}

    for dev in sorted(Path("/dev").glob("video*")):
        dev_path = str(dev)
        name = dev_path

        # v4l2-ctl로 이름 추출
        rc, out, _ = _run_cmd(["v4l2-ctl", f"--device={dev_path}", "--info"])
        if rc == 0:
            for line in out.splitlines():
                if "Card type" in line:
                    name = line.split(":", 1)[1].strip()
                    break

        results.append({"id": dev_path, "name": name})

    return results


class CameraManager:
    def __init__(self) -> None:
        self.cameras: dict[str, CameraInfo] = {}

    def scan(self) -> list[dict]:
        devs = scan_cameras()
        for d in devs:
            cam_id = d["id"]
            if cam_id not in self.cameras:
                self.cameras[cam_id] = CameraInfo(id=cam_id, name=d["name"])
            else:
                self.cameras[cam_id].name = d["name"]
        return [c.to_dict() for c in self.cameras.values()]

    def probe_camera(self, cam_id: str) -> tuple[bool, str]:
        """연결 테스트 + 프리뷰 1장 → 즉시 해제."""
        cam = self.cameras.get(cam_id)
        if not cam:
            return False, f"Unknown camera: {cam_id}"
        return cam.probe()

    def connect_camera(self, cam_id: str) -> tuple[bool, str]:
        """백그라운드 캡처 시작 (등록 시 사용)."""
        cam = self.cameras.get(cam_id)
        if not cam:
            return False, f"Unknown camera: {cam_id}"
        return cam.connect()

    def disconnect_camera(self, cam_id: str) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.disconnect()
        return True

    def update_config(self, cam_id: str, cfg: dict) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.update_config(cfg)
        return True

    def register_camera(self, cam_id: str) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        # 아직 연결 안 되었으면 자동 connect (백그라운드 캡처 시작)
        if not cam.connected:
            ok, _ = cam.connect()
            if not ok:
                return False
        cam.ready = True
        return True

    def unregister_camera(self, cam_id: str) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.ready = False
        return True

    def get_ready_cameras(self) -> list[dict]:
        return [c.to_dict() for c in self.cameras.values() if c.ready]

    def get_preview(self, cam_id: str) -> bytes | None:
        cam = self.cameras.get(cam_id)
        if not cam:
            return None
        return cam.capture_preview()

    def get_current(self) -> dict:
        return {"cameras": [c.to_dict() for c in self.cameras.values()]}


camera_manager = CameraManager()
