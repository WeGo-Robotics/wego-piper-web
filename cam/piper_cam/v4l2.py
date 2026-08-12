"""v4l2 ioctl 계층 — **camerad 의 본체**.

`backend/app/services/camera_manager.py` 에서 그대로 옮겼다. 게이트웨이를 import 하지
않는다 — 데몬이 백엔드에 의존하면 분리한 의미가 없다 (rsd 와 같은 이유).

컨트롤 조회·설정을 `v4l2-ctl` 서브프로세스가 아니라 **ioctl 로 직접** 하는 이유는
원본 그대로다: 프로세스 기동 비용이 프리뷰 주기에 비해 크다.
"""

import ctypes
import fcntl
import logging
import os
import struct
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _run_cmd(cmd: list[str], timeout: float = 2) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


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
    # ⚠ **RealSense 노드는 무조건 건너뛴다.** rsd 가 그 장치를 독점한다 —
    # camerad 가 같이 열면 두 데몬이 같은 USB 장치를 두고 싸운다.
    # (게이트웨이 시절에는 `rs_available()` 로 조건부였다. 데몬 모델에서는
    #  소유자가 하나로 정해져 있으므로 조건이 필요 없다.)
    #
    # 반드시 `_is_capture_device`(v4l2 open/close) **전에** 검사한다 —
    # RealSense 노드는 close() 가 커널에서 블로킹되어 스캔이 멈춘다.
    if "realsense" in name.lower():
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

