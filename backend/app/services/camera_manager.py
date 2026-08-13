"""
카메라 관리 서비스.
시스템 카메라 스캔, 연결 테스트, 설정, 등록, 프리뷰.
"""

import logging
from dataclasses import dataclass

from app.core.config import settings
from app.services.realsense_manager import realsense_hub
from app.services.v4l2_client import v4l2_hub

logger = logging.getLogger(__name__)


# USB 3.0 = 5000Mbps. 그 미만이면 RealSense 두 대나 color+depth 를 감당 못 한다.
USB3_MBPS = 5000


def _usb_warning(speed_mbps: int) -> str | None:
    """USB 대역폭 경고. 없으면 `None`.

    ⚠ **여기 한 곳에서만 판정한다.** 화면이 임계값을 따로 적으면 한쪽만 고쳐져
    어긋난다. 속도를 못 읽었으면(0) 경고하지 않는다 — 모르는 것과 느린 것은 다르다.
    """
    if not speed_mbps or speed_mbps >= USB3_MBPS:
        return None
    return (
        f"USB {speed_mbps}Mbps (2.0) 로 연결돼 있습니다. 대역폭이 모자라 "
        "카메라를 여러 대 쓰거나 depth 를 켜면 프레임이 끊깁니다 — "
        "USB 3 포트로 옮기세요."
    )


@dataclass
class CameraInfo:
    """카메라 **기록**. 장치 I/O 는 데몬이 한다.

    예전에는 이 클래스가 `cv2.VideoCapture` 를 직접 열고 캡처 스레드를 돌렸다.
    이제 장치는 camerad(v4l2) / rsd(RealSense) 가 소유하고, 여기는 게이트웨이 상태만
    들고 있다 — 등록(`ready`)·별칭(`label`)·요청 해상도.

    두 허브가 **같은 메서드 이름**을 쓰므로 분기는 `_hub` 한 줄이다.
    """

    id: str           # "/dev/video0" / "rs:<serial>:<stream>"
    name: str         # 하드웨어가 말하는 이름 — "D435 Color", "HD Webcam" 등
    # 사람이 붙이는 별칭 — "탑뷰", "손목". 등록할 때 지정한다.
    #
    # ⚠ **LeRobot 카메라 키와는 다른 값이다.** 데이터셋 피처는
    # `observation.images.<키>` 로 굳고 정책도 그 키로 학습되므로, 여기서 이름을
    # 바꾸면 학습된 정책이 안 열린다. 별칭은 화면 표시 전용이다.
    label: str = ""
    usb_port: str = ""
    # USB 링크 속도(Mbps). 0 이면 못 읽은 것 — **빠르다고 가정하지 않는다.**
    # 480 이면 USB 2.0 이고, 카메라 두 대나 depth 를 같이 쓰기엔 대역폭이 모자란다.
    usb_speed_mbps: int = 0
    cam_type: str = "opencv"  # opencv | realsense
    serial: str = ""
    stream_type: str = ""
    connected: bool = False
    ready: bool = False       # 등록 완료 → 사용 가능 리스트에 올라감
    # 설정값 (요청 해상도 — 실제 값은 데몬이 정한다)
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    color_mode: str = "rgb"
    rotation: int = 0
    fourcc: str | None = None

    @property
    def _hub(self):
        """장치를 소유한 데몬의 클라이언트. **이 한 줄이 유일한 분기다.**"""
        return realsense_hub if self.cam_type == "realsense" else v4l2_hub

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            # 화면에 한 줄로 쓰기 좋은 표시명 — 각 화면이 `label || name` 을
            # 따로 적으면 규칙이 갈린다.
            "display_name": self.label or self.name,
            "usb_port": self.usb_port,
            "usb_speed_mbps": self.usb_speed_mbps,
            # 화면이 임계값을 따로 적지 않게 판정까지 여기서 한다 —
            # 두 곳에 적으면 한쪽만 고쳐져 어긋난다.
            "usb_warning": _usb_warning(self.usb_speed_mbps),
            "cam_type": self.cam_type,
            "serial": self.serial,
            "stream_type": self.stream_type,
            "connected": self.connected,
            "ready": self.ready,
            "has_preview": self._hub.has_frame(self.id),
            # 깊이 인코딩은 **데이터셋 해석의 근거**다 — 화면에서 현재 값을 보고
            # 고칠 수 있어야 한다.
            # ⚠ depth 일 때만 묻는다. `to_dict` 는 스캔에서 카메라마다 불리므로
            # 무조건 물으면 RPC 가 카메라 수만큼 늘어 스캔이 그만큼 느려진다.
            "depth_encoding": (self.running_profile().get("depth_encoding")
                               if self.stream_type == "depth" else None),
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

    def probe(self, timeout: float = 3.0) -> tuple[bool, str]:
        return self._hub.probe(self.id)

    def connect(self, width: int = 0, height: int = 0, fps: int = 0) -> tuple[bool, str]:
        """셋 다 주면 그 프로파일로 연다. 하나라도 0이면 장치 기본값에 맡긴다.

        장치가 못 내는 조합이면 데몬이 가장 가까운 것으로 낮춰서 연다 —
        `running_profile()` 로 실제 값을 확인할 수 있다.
        """
        ok, msg = self._hub.connect(self.id, width, height, fps)
        self.connected = ok
        return ok, msg

    def running_profile(self) -> dict:
        """지금 **실제로** 돌고 있는 `width/height/fps`. 요청값이 아니다."""
        return self._hub.info(self.id) or {}

    def disconnect(self) -> None:
        self._hub.disconnect(self.id)
        self.connected = False

    def get_controls(self) -> list[dict]:
        return self._hub.list_controls(self.id)

    def set_control(self, name: str, value: int) -> bool:
        return self._hub.set_control(self.id, name, value)

    def capture_preview(self) -> bytes | None:
        """최신 프레임 JPEG. 세그먼트에서 직접 읽는다 — RPC 가 아니다."""
        return self._hub.get_jpeg(self.id)


class CameraManager:
    def __init__(self) -> None:
        self.cameras: dict[str, CameraInfo] = {}

    def scan(self) -> list[dict]:
        """두 데몬의 스캔 결과를 합친다.

        **소유가 겹치지 않는다** — camerad 는 RealSense 노드를 건너뛰고
        rsd 는 v4l2 를 안 본다. 데몬이 죽어 있으면 그쪽만 빠지고 나머지는 보인다.
        """
        # 일반 웹캠 — camerad
        for d in v4l2_hub.scan():
            self._absorb(d)

        # RealSense — rsd. 디바이스당 color/depth/infrared 스트림 엔트리
        for d in realsense_hub.scan():
            self._absorb(d, cam_type="realsense")

        return [c.to_dict() for c in self.cameras.values()]

    def _absorb(self, d: dict, cam_type: str = "opencv") -> None:
        """스캔 엔트리를 흡수한다. **두 데몬이 같은 경로를 탄다.**

        ⚠ 예전에는 v4l2 와 RealSense 가 각자 갱신했고, 한쪽은
        `self.cameras[id].x = ...`, 다른 쪽은 `cam.x = ...` 모양이었다.
        필드를 하나 추가하면서 한쪽만 고쳐져, USB 3 으로 바꿔 꽂아도
        **RealSense 쪽 속도만 옛 값에 머물렀다.** 같은 사실은 한 곳에서 갱신한다.

        등록 정보(label·ready·config)는 건드리지 않는다 — 그건 사람이 정한 것이고
        스캔은 장치가 말해주는 것만 가져온다.
        """
        cam_id = d["id"]
        cam = self.cameras.get(cam_id)
        if cam is None:
            cam = self.cameras[cam_id] = CameraInfo(id=cam_id, name=d["name"],
                                                    cam_type=cam_type)
        cam.name = d["name"]
        cam.usb_port = d.get("usb_port", "")
        cam.usb_speed_mbps = int(d.get("usb_speed_mbps") or 0)
        # ⚠ `cam_type` 으로 분기하지 않는다 — "어느 데몬 소유인가"는 `_hub` 한 곳에서만
        # 판단한다(테스트가 강제한다). 여기서는 **엔트리에 있는 것만** 가져오면 된다:
        # v4l2 엔트리에는 serial/stream_type 이 없으므로 자연히 건너뛴다.
        cam.serial = d.get("serial", cam.serial)
        cam.stream_type = d.get("stream_type", cam.stream_type)

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
