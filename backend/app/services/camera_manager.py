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
            "cam_type": self.cam_type,
            "serial": self.serial,
            "stream_type": self.stream_type,
            "connected": self.connected,
            "ready": self.ready,
            "has_preview": self._hub.has_frame(self.id),
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

    def connect(self) -> tuple[bool, str]:
        ok, msg = self._hub.connect(self.id)
        self.connected = ok
        return ok, msg

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
            cam_id = d["id"]
            if cam_id not in self.cameras:
                self.cameras[cam_id] = CameraInfo(
                    id=cam_id, name=d["name"], usb_port=d.get("usb_port", "")
                )
            else:
                self.cameras[cam_id].name = d["name"]
                self.cameras[cam_id].usb_port = d.get("usb_port", "")

        # RealSense — rsd. 디바이스당 color/depth/infrared 스트림 엔트리
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
