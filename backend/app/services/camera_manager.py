"""
카메라 관리 서비스.
시스템 카메라 스캔, 연결 테스트, 설정, 등록, 프리뷰.
"""

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.services.realsense_manager import realsense_hub, rs_available

logger = logging.getLogger(__name__)


def _run_cmd(cmd: list[str], timeout: float = 2) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


@dataclass
class CameraInfo:
    id: str           # "/dev/video0" / "rs:<serial>:<stream>"
    name: str         # 하드웨어가 말하는 이름 — "D435 Color", "HD Webcam" 등
    # 사람이 붙이는 별칭 — "탑뷰", "손목". 등록할 때 지정한다.
    #
    # ⚠ **LeRobot 카메라 키와는 다른 값이다.** 데이터셋 피처는
    # `observation.images.<키>` 로 굳고 정책도 그 키로 학습되므로, 여기서 이름을
    # 바꾸면 학습된 정책이 안 열린다. 그래서 별칭은 **화면 표시 전용**으로 두고,
    # 실제 키는 녹화·추론 페이지에서 따로 정한다. 대신 그 드롭다운이 별칭을 보여줘서
    # "어느 게 탑뷰인지" 를 고르는 순간에 알 수 있게 한다.
    label: str = ""
    usb_port: str = ""  # "4-3:1.0" = 연결된 USB 포트 경로 (구분용)
    cam_type: str = "opencv"  # opencv | realsense | zmq
    serial: str = ""        # realsense 디바이스 시리얼
    stream_type: str = ""   # realsense 스트림 (color|depth|infrared)
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
        if self.cam_type == "realsense":
            has_preview = realsense_hub.has_frame(self.id)
        else:
            has_preview = self._last_frame is not None
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            # 화면에 한 줄로 쓰기 좋은 표시명 — 별칭이 있으면 그것, 없으면 하드웨어 이름.
            # 각 화면이 `label || name` 을 따로 적으면 규칙이 갈린다.
            "display_name": self.label or self.name,
            "usb_port": self.usb_port,
            "cam_type": self.cam_type,
            "serial": self.serial,
            "stream_type": self.stream_type,
            "connected": self.connected,
            "ready": self.ready,
            "has_preview": has_preview,
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
        if self.cam_type == "realsense":
            return realsense_hub.probe(self.id)
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
        if self.cam_type == "realsense":
            ok, msg = realsense_hub.connect(self.id)
            self.connected = ok
            return ok, msg
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
        if self.cam_type == "realsense":
            realsense_hub.disconnect(self.id)
            self.connected = False
            self._last_frame = None
            return
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
        """카메라 컨트롤 열거 + 현재값/범위 조회 (v4l2 또는 RealSense option)."""
        if self.cam_type == "realsense":
            return realsense_hub.list_controls(self.id)
        return v4l2_list_controls(self.id)

    def set_control(self, name: str, value: int) -> bool:
        """컨트롤 값 설정 (v4l2 또는 RealSense option)."""
        if self.cam_type == "realsense":
            return realsense_hub.set_control(self.id, name, value)
        controls = v4l2_list_controls(self.id)
        for ctrl in controls:
            if ctrl["name"] == name:
                return v4l2_set_control(self.id, ctrl["cid"], int(value))
        return False

    def capture_preview(self) -> bytes | None:
        """마지막 캡처된 프레임을 JPEG로 즉시 반환."""
        if self.cam_type == "realsense":
            return realsense_hub.get_jpeg(self.id)
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


# ── VIDIOC_QUERYCAP (캡처 노드 vs 메타데이터 노드 판별) ──

class _v4l2_capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


_VIDIOC_QUERYCAP = (2 << 30) | (ord("V") << 8) | 0 | (ctypes.sizeof(_v4l2_capability) << 16)
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def _is_capture_device(dev_path: str) -> bool:
    """V4L2 device_caps에 VIDEO_CAPTURE가 있으면 True (메타데이터 노드는 False)."""
    try:
        fd = os.open(dev_path, os.O_RDWR)
    except OSError:
        return False
    try:
        cap = _v4l2_capability()
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, cap)
        # device_caps는 해당 노드 고유 능력. 0이면 전체 capabilities로 폴백.
        caps = cap.device_caps or cap.capabilities
        return bool(caps & _V4L2_CAP_VIDEO_CAPTURE)
    except OSError:
        return False
    finally:
        os.close(fd)


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


def _usb_port_path(dev_path: str) -> str:
    """디바이스가 연결된 USB 포트 경로 ("4-3:1.0" = 버스-포트:설정.인터페이스).
    동일 이름 카메라들을 물리 포트로 구분할 때 사용. 비-USB면 빈 문자열."""
    dev_name = Path(dev_path).name  # "video0"
    try:
        # /sys/class/video4linux/videoN/device → USB 인터페이스 디렉터리로 resolve
        target = Path(f"/sys/class/video4linux/{dev_name}/device").resolve()
    except Exception:
        return ""
    # 마지막 컴포넌트가 USB 인터페이스(예: "4-3:1.0"). USB가 아니면 ":" 없음.
    iface = target.name
    return iface if ":" in iface else ""


def _scan_one(dev_path: str) -> dict | None:
    # /sys/class/video4linux/videoN/name 에서 이름 읽기 (빠르고 안전)
    dev_name = Path(dev_path).name  # "video0"
    sys_name = Path(f"/sys/class/video4linux/{dev_name}/name")

    if not sys_name.exists():
        return None

    try:
        name = sys_name.read_text().strip()
    except Exception:
        name = dev_path

    # RealSense는 Depth(Z16)/IR(Y8)를 OpenCV로 못 열기 때문에 pyrealsense2
    # 경로(realsense_hub)로 처리한다. rs 사용 가능하면 v4l2 스캔에서 제외.
    # ※ 반드시 _is_capture_device(v4l2 open/close) 전에 검사한다 —
    #   RealSense 노드는 close()가 커널에서 블로킹되어 스캔/서버 startup이 멈춘다.
    if "realsense" in name.lower() and rs_available():
        return None

    # VIDEO_CAPTURE 노드만 유지 (메타데이터 노드 제외).
    # RealSense처럼 한 디바이스가 캡처/메타 노드를 번갈아 노출하므로
    # sysfs index 순번이 아니라 V4L2 device_caps로 판별해야 한다.
    if not _is_capture_device(dev_path):
        return None

    return {"id": dev_path, "name": name, "usb_port": _usb_port_path(dev_path)}


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
        # 일반 웹캠 (OpenCV/V4L2)
        for d in scan_cameras():
            cam_id = d["id"]
            if cam_id not in self.cameras:
                self.cameras[cam_id] = CameraInfo(
                    id=cam_id, name=d["name"], usb_port=d.get("usb_port", "")
                )
            else:
                self.cameras[cam_id].name = d["name"]
                self.cameras[cam_id].usb_port = d.get("usb_port", "")

        # RealSense (pyrealsense2) — 디바이스당 color/depth/infrared 스트림 엔트리
        if rs_available():
            for d in realsense_hub.scan():
                cam_id = d["id"]
                if cam_id not in self.cameras:
                    self.cameras[cam_id] = CameraInfo(
                        id=cam_id, name=d["name"], usb_port=d.get("usb_port", ""),
                        cam_type="realsense", serial=d["serial"], stream_type=d["stream_type"],
                    )
                else:
                    cam = self.cameras[cam_id]
                    cam.name = d["name"]
                    cam.usb_port = d.get("usb_port", "")
                    cam.serial = d["serial"]
                    cam.stream_type = d["stream_type"]

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

    def register_camera(self, cam_id: str, label: str | None = None) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        # 아직 연결 안 되었으면 자동 connect (백그라운드 캡처 시작)
        if not cam.connected:
            ok, _ = cam.connect()
            if not ok:
                return False
        if label is not None:
            cam.label = label.strip()
        cam.ready = True
        return True

    def set_label(self, cam_id: str, label: str) -> bool:
        """별칭만 바꾼다 — 등록 후에도 고칠 수 있어야 한다.

        빈 문자열이면 별칭을 지우고 하드웨어 이름으로 되돌아간다.
        """
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.label = label.strip()
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

    CAMERA_SESSION_PATH = settings.config_dir / "camera_session.json"

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
                    "label": cam.label,
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
            cam.label = cam_data.get("label", "")
            cam.update_config(cam_data.get("config", {}))
            cam.ready = True
            restored += 1
            logger.info("  Restored camera %s (%s)", cam_id, cam.label or cam.name)

        logger.info("Camera session restored: %d/%d cameras", restored, len(data))
        return restored > 0


camera_manager = CameraManager()
