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


def _run_cmd(cmd: list[str], timeout: float = 2) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
        # 디바이스 경로를 직접 전달 + V4L2 백엔드 명시
        cap = cv2.VideoCapture(self.id, cv2.CAP_V4L2)
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

    def probe(self, timeout: float = 3.0) -> tuple[bool, str]:
        """연결 테스트 + 프리뷰 1장 → 즉시 해제. 스캔 시 사용."""
        try:
            import cv2
        except ImportError:
            return False, "opencv-python not installed"

        import concurrent.futures
        def _do_probe():
            cap, frame = self._open_cap()
            if cap is None:
                return False, f"Cannot open {self.id}"
            with self._lock:
                self._last_frame = frame
            cap.release()
            self.connected = False
            return True, "OK"

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_do_probe)
                return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False, f"Timeout ({timeout}s) probing {self.id}"
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

    # ── v4l2 컨트롤 (밝기, 대비 등) — ioctl 직접 사용 ──

    def get_controls(self) -> list[dict]:
        """v4l2 ioctl로 카메라 컨트롤 열거 + 현재값/범위 조회."""
        return v4l2_list_controls(self.id)

    def set_control(self, name: str, value: int) -> bool:
        """v4l2 ioctl로 컨트롤 값 설정."""
        controls = v4l2_list_controls(self.id)
        for ctrl in controls:
            if ctrl["name"] == name:
                return v4l2_set_control(self.id, ctrl["cid"], int(value))
        return False

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


# ── v4l2 ioctl 헬퍼 (ctypes + EXT_CTRLS) ──

import os
import fcntl
import struct
import ctypes


def _iowr(type_ch: str, nr: int, size: int) -> int:
    return (3 << 30) | (ord(type_ch) << 8) | nr | (size << 16)


# v4l2 ioctl 번호
_VIDIOC_QUERYCTRL = _iowr("V", 36, 68)

# v4l2 플래그
_V4L2_CTRL_FLAG_NEXT_CTRL = 0x80000000
_V4L2_CTRL_FLAG_DISABLED = 0x0001

# v4l2_queryctrl struct
_QUERYCTRL_FMT = "II32siiiII8s"


# ── EXT_CTRLS (G_CTRL/S_CTRL이 실패하는 UVC 디바이스 대응) ──

class _v4l2_ext_control(ctypes.Structure):
    # 커널 구조체: union이 offset 12, 크기 8 → 총 20바이트
    # c_int64를 union에 넣으면 ctypes가 8-byte align 패딩 추가 → 깨짐
    # 대신 value(4) + reserved(4)로 동일 레이아웃 구현
    _fields_ = [
        ("id", ctypes.c_uint32),       # offset 0
        ("size", ctypes.c_uint32),     # offset 4
        ("reserved2", ctypes.c_uint32),  # offset 8
        ("value", ctypes.c_int32),     # offset 12 (커널 union.value 위치)
        ("_pad", ctypes.c_uint32),     # offset 16 (union 나머지)
    ]


class _v4l2_ext_controls(ctypes.Structure):
    _fields_ = [
        ("which", ctypes.c_uint32),
        ("count", ctypes.c_uint32),
        ("error_idx", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32 * 1),
        ("controls", ctypes.POINTER(_v4l2_ext_control)),
    ]


_VIDIOC_G_EXT_CTRLS = _iowr("V", 71, ctypes.sizeof(_v4l2_ext_controls))
_VIDIOC_S_EXT_CTRLS = _iowr("V", 72, ctypes.sizeof(_v4l2_ext_controls))
# fallback: 기본 G_CTRL/S_CTRL
_VIDIOC_G_CTRL = _iowr("V", 38, 8)
_VIDIOC_S_CTRL = _iowr("V", 39, 8)


def _v4l2_get_value(fd: int, cid: int, default: int = 0) -> int:
    """컨트롤 현재값 읽기. EXT_CTRLS → G_CTRL fallback."""
    # EXT_CTRLS 시도
    ctrl = _v4l2_ext_control()
    ctrl.id = cid
    ctrls = _v4l2_ext_controls()
    ctrls.which = 0  # V4L2_CTRL_WHICH_CUR_VAL
    ctrls.count = 1
    ctrls.controls = ctypes.pointer(ctrl)
    try:
        fcntl.ioctl(fd, _VIDIOC_G_EXT_CTRLS, ctrls)
        return ctrl.value
    except OSError:
        pass
    # G_CTRL fallback
    try:
        buf = struct.pack("Ii", cid, 0)
        res = fcntl.ioctl(fd, _VIDIOC_G_CTRL, buf)
        _, val = struct.unpack("Ii", res)
        return val
    except OSError:
        return default


def _v4l2_set_value(fd: int, cid: int, value: int) -> bool:
    """컨트롤 값 설정. EXT_CTRLS → S_CTRL fallback."""
    # EXT_CTRLS 시도
    ctrl = _v4l2_ext_control()
    ctrl.id = cid
    ctrl.value = value
    ctrls = _v4l2_ext_controls()
    ctrls.which = 0
    ctrls.count = 1
    ctrls.controls = ctypes.pointer(ctrl)
    try:
        fcntl.ioctl(fd, _VIDIOC_S_EXT_CTRLS, ctrls)
        return True
    except OSError:
        pass
    # S_CTRL fallback
    try:
        buf = struct.pack("Ii", cid, value)
        fcntl.ioctl(fd, _VIDIOC_S_CTRL, buf)
        return True
    except OSError as e:
        logger.warning("v4l2 set failed: cid=0x%x value=%d err=%s", cid, value, e)
        return False


def v4l2_list_controls(dev_path: str) -> list[dict]:
    """디바이스의 모든 v4l2 컨트롤을 열거."""
    if not isinstance(dev_path, str) or not dev_path.startswith("/dev/video"):
        return []
    try:
        fd = os.open(dev_path, os.O_RDWR)
    except OSError:
        return []

    controls = []
    ctrl_id = _V4L2_CTRL_FLAG_NEXT_CTRL

    for _ in range(200):
        buf = struct.pack(_QUERYCTRL_FMT, ctrl_id, 0, b"", 0, 0, 0, 0, 0, b"")
        try:
            result = fcntl.ioctl(fd, _VIDIOC_QUERYCTRL, buf)
        except OSError:
            break

        cid, ctype, raw_name, minimum, maximum, step, default, flags, _ = struct.unpack(
            _QUERYCTRL_FMT, result
        )
        ctrl_id = cid | _V4L2_CTRL_FLAG_NEXT_CTRL

        if flags & _V4L2_CTRL_FLAG_DISABLED:
            continue
        if ctype == 6:  # ctrl_class 헤더
            continue

        name = raw_name.split(b"\x00")[0].decode(errors="replace")
        cur_val = _v4l2_get_value(fd, cid, default)

        inactive = bool(flags & 0x0010)
        readonly = bool(flags & 0x0004)
        controls.append({
            "cid": cid,
            "name": name.lower().replace(" ", "_").replace(",", ""),
            "label": name,
            "type": ctype,  # 1=int, 2=bool, 3=menu
            "min": minimum,
            "max": maximum,
            "step": step,
            "default": default,
            "value": cur_val,
            "inactive": inactive,
            "readonly": readonly,
        })

    os.close(fd)
    return controls


def v4l2_set_control(dev_path: str, cid: int, value: int) -> bool:
    """v4l2 컨트롤 값 설정."""
    try:
        fd = os.open(dev_path, os.O_RDWR)
    except OSError:
        return False
    try:
        return _v4l2_set_value(fd, cid, value)
    finally:
        os.close(fd)


def _scan_one(dev_path: str) -> dict | None:
    # /sys/class/video4linux/videoN/name 에서 이름 읽기 (빠르고 안전)
    dev_name = Path(dev_path).name  # "video0"
    sys_name = Path(f"/sys/class/video4linux/{dev_name}/name")
    sys_index = Path(f"/sys/class/video4linux/{dev_name}/index")

    if not sys_name.exists():
        return None

    # index > 0 이면 메타데이터 노드 → 스킵
    try:
        if sys_index.exists() and int(sys_index.read_text().strip()) > 0:
            return None
    except Exception:
        pass

    try:
        name = sys_name.read_text().strip()
    except Exception:
        name = dev_path

    return {"id": dev_path, "name": name}


def scan_cameras() -> list[dict]:
    """시스템 카메라 스캔 (/dev/video* + v4l2-ctl) — 병렬. 캡처 디바이스만."""
    from concurrent.futures import ThreadPoolExecutor

    devs = sorted(str(d) for d in Path("/dev").glob("video*"))
    if not devs:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [r for r in pool.map(_scan_one, devs) if r is not None]
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

    def get_controls(self, cam_id: str) -> list[dict]:
        cam = self.cameras.get(cam_id)
        if not cam:
            return []
        return cam.get_controls()

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        return cam.set_control(name, value)

    def get_current(self) -> dict:
        return {"cameras": [c.to_dict() for c in self.cameras.values()]}


    # ── 세션 저장/복원 ──

    CAMERA_SESSION_PATH = Path.home() / ".config" / "piper-web" / "camera_session.json"

    def save_session(self) -> None:
        """등록된 카메라 상태 저장."""
        import json
        self.CAMERA_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for cam in self.cameras.values():
            if cam.ready:
                data.append({
                    "id": cam.id,
                    "name": cam.name,
                    "cam_type": cam.cam_type,
                    "config": {
                        "width": cam.width, "height": cam.height, "fps": cam.fps,
                        "color_mode": cam.color_mode, "rotation": cam.rotation, "fourcc": cam.fourcc,
                    },
                })
        self.CAMERA_SESSION_PATH.write_text(json.dumps(data, indent=2))
        logger.info("Camera session saved (%d cameras)", len(data))

    def restore_session(self) -> bool:
        """세션 파일에서 카메라 상태 복원."""
        import json
        if not self.CAMERA_SESSION_PATH.exists():
            return False
        try:
            data = json.loads(self.CAMERA_SESSION_PATH.read_text())
        except Exception:
            return False
        if not data:
            return False

        logger.info("Restoring camera session (%d cameras)...", len(data))

        # 먼저 스캔
        self.scan()

        restored = 0
        for cam_data in data:
            cam_id = cam_data.get("id", "")
            if cam_id not in self.cameras:
                logger.warning("  Session camera %s not found in scan, skipping", cam_id)
                continue
            cam = self.cameras[cam_id]
            cam.cam_type = cam_data.get("cam_type", "opencv")
            cam.update_config(cam_data.get("config", {}))
            cam.ready = True
            restored += 1
            logger.info("  Restored camera %s (%s)", cam_id, cam.name)

        logger.info("Camera session restored: %d/%d cameras", restored, len(data))
        return restored > 0


camera_manager = CameraManager()
