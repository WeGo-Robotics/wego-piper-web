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


def _active_controls(cam) -> dict:
    """활성 프로파일에서 이 카메라의 컨트롤 값. 없으면 빈 dict.

    지연 import 다 — `camera_profiles` 는 카메라 목록이 필요할 때 인자로 받으므로
    이쪽을 import 하지 않지만, 순환의 씨앗을 아예 두지 않는다.
    실패해도 연결을 막지 않는다: 프로파일 때문에 카메라가 안 열리면 본말전도다.
    """
    try:
        from app.services import camera_profiles

        return camera_profiles.controls_for(cam)
    except Exception as exc:
        logger.warning("프로파일 조회 실패 (%s): %s", getattr(cam, "id", "?"), exc)
        return {}



def _forget(kind: str, ident: str) -> None:
    """사용자가 **일부러** 끊었다 — 사라진 것이 아니므로 감시 기억에서 지운다.

    안 지우면 끊을 때마다 "사라졌습니다" 경보가 뜬다.
    """
    try:
        from app.services.device_watch import device_watch

        device_watch.forget(kind, ident)
    except Exception:
        pass


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
    # 마지막 스캔에서 데몬이 이 장치를 **봤는가**. `connected` 와 다른 사실이다:
    # `present && !connected` = 꽂혀 있는데 안 열었다 (정상)
    # `!present`              = 아예 없다 (뽑혔다)
    # 예전엔 둘 다 `connected: false` 라 화면이 구분할 수 없었고, 뽑아둔 카메라가
    # 스캔 목록에 그대로 남아 "왜 계속 나오지" 가 됐다.
    present: bool = True
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

    @property
    def profile_key(self) -> str:
        """재열거·리셋에도 안 바뀌는 식별자. 설정을 이 키에 매단다.

        `/dev/videoN` 은 **키가 아니다.** USB 컨트롤러가 죽어 리바인딩하면
        (이 저장소에서 실제로 겪었다) 번호가 바뀌고, id 로 매칭하던 설정은
        "스캔에 없다"며 조용히 버려진다.

        - RealSense: `rs:<시리얼>:<스트림>` 인 `id` 가 이미 안정적이다 —
          별도 키를 만들면 같은 사실을 두 벌 갖게 된다
        - v4l2: 물리 포트(`usb:4-3:1.0`). 같은 모델 두 대도 구분된다
        - 포트를 못 읽으면 이름 폴백 — 후보가 하나일 때만 쓴다(아래 `match_saved`)

        ⚠ `cam_type` 으로 갈라 묻지 않는다. 장치 종류 분기는 `_hub` 한 곳뿐이라는
        규칙이 있고(테스트가 강제한다), 여기서 물어야 하는 건 종류가 아니라
        **"이 id 가 재열거로 바뀌는 종류인가"** 다. 바뀌는 건 `/dev/videoN` 뿐이다.
        """
        if not self.id.startswith("/dev/"):
            return self.id
        if self.usb_port:
            return f"usb:{self.usb_port}"
        return f"name:{self.name}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            # 화면에 한 줄로 쓰기 좋은 표시명 — 각 화면이 `label || name` 을
            # 따로 적으면 규칙이 갈린다.
            "display_name": self.label or self.name,
            "usb_port": self.usb_port,
            # 설정이 매달리는 안정 키. 화면이 "이 카메라가 프로파일에 있나"를
            # 판단하려면 id 가 아니라 이걸 봐야 한다.
            "profile_key": self.profile_key,
            "usb_speed_mbps": self.usb_speed_mbps,
            # 화면이 임계값을 따로 적지 않게 판정까지 여기서 한다 —
            # 두 곳에 적으면 한쪽만 고쳐져 어긋난다.
            "usb_warning": _usb_warning(self.usb_speed_mbps),
            "cam_type": self.cam_type,
            "serial": self.serial,
            "stream_type": self.stream_type,
            "connected": self.connected,
            "present": self.present,
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

        ⚠ **활성 프로파일의 컨트롤을 여기서 함께 넘긴다.** 장치가 열리는 순간은
        이 한 곳뿐이라(데몬이 유일한 소유자다) 트리거를 여기저기 배선할 필요가 없다.
        노출·화이트밸런스가 초기화되는 경로가 여럿이었던 건 여는 주체가 여럿이라서다.
        """
        ok, msg = self._hub.connect(self.id, width, height, fps,
                                    _active_controls(self))
        self.connected = ok
        return ok, msg

    def running_profile(self) -> dict:
        """지금 **실제로** 돌고 있는 `width/height/fps`. 요청값이 아니다."""
        return self._hub.info(self.id) or {}

    def disconnect(self) -> None:
        self._hub.disconnect(self.id)
        self.connected = False
        _forget("camera", self.id)

    def get_controls(self) -> list[dict]:
        return self._hub.list_controls(self.id)

    def set_control(self, name: str, value: int) -> bool:
        return self._hub.set_control(self.id, name, value)

    def mark_absent(self) -> None:
        """데몬이 이 카메라를 모른다 — **장치 사실만** 지운다.

        등록(`ready`)·별칭·요청 해상도는 사람이 정한 것이라 남긴다. 하지만
        `connected` 는 장치 사실이고, USB 가 빠진 동안 살아남으면 **화면은
        "연결됨"인데 세그먼트가 없어 녹화·추론이 죽는다.** 팔에서 똑같은 구멍을
        먼저 겪었다 (robot_manager.ArmInfo.mark_absent).
        """
        self.connected = False
        self.present = False

    def apply_controls(self, wanted: dict) -> dict:
        """프로파일 컨트롤 적용. **순서와 검증은 데몬이 한다** — 자동 모드
        종속성 때문에 dict 순회로는 조용히 실패한다."""
        return self._hub.apply_controls(self.id, wanted)

    def last_apply_report(self) -> dict:
        return self._hub.last_apply_report(self.id)

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
        seen: set[str] = set()
        # 일반 웹캠 — camerad
        for d in v4l2_hub.scan():
            seen.add(d["id"])
            self._absorb(d)

        # RealSense — rsd. 디바이스당 color/depth/infrared 스트림 엔트리
        for d in realsense_hub.scan():
            seen.add(d["id"])
            self._absorb(d, cam_type="realsense")

        # ⚠ **안 보인 카메라는 없는 것으로 표시한다.** 예전에는 보고된 것만 순회해서,
        # USB 가 빠져도 목록이 마지막 상태에 머물렀다 — 사용자가 스캔을 눌러도
        # 아무것도 안 바뀌었다.
        #
        # 등록된 카메라는 **남긴다** — 별칭·매핑을 사람이 정했고, 다시 꽂으면
        # 그대로 돌아와야 한다. 대신 `present: false` 로 표시해 화면이 "없음"을
        # 보여줄 수 있게 한다.
        #
        # 등록 안 된 카메라는 **지운다.** 보존할 사람의 결정이 없고, 남겨두면
        # 뽑아둔 장치가 "미등록" 목록에 계속 떠서 고를 수 있는 것처럼 보인다.
        for cam_id in [c for c in self.cameras if c not in seen]:
            cam = self.cameras[cam_id]
            cam.mark_absent()
            if not cam.ready:
                del self.cameras[cam_id]

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
        cam.present = True
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
                    # 복원 매칭의 **진짜 키**. `id` 는 참고용으로만 남긴다 —
                    # v4l2 는 재열거로 번호가 바뀐다.
                    "key": cam.profile_key,
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

    def match_saved(self, saved: dict):
        """저장된 항목을 지금 보이는 카메라에 맞춘다. **키 → id → 이름** 순.

        이름 폴백은 **후보가 하나일 때만** 쓴다. 같은 모델 두 대를 서로 다른 포트에
        꽂아 뒀는데 이름으로 맞추면 설정이 엉뚱한 카메라에 붙는다 —
        틀리게 복원하느니 복원 안 하는 게 낫다.
        """
        key = saved.get("key") or ""
        if key:
            for cam in self.cameras.values():
                if cam.profile_key == key:
                    return cam
        cam_id = saved.get("id") or ""
        if cam_id in self.cameras:
            return self.cameras[cam_id]
        name = saved.get("name") or ""
        if name:
            hits = [c for c in self.cameras.values() if c.name == name]
            if len(hits) == 1:
                return hits[0]
        return None

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
            cam = self.match_saved(cam_data)
            if cam is None:
                logger.warning("  Session camera %s(%s) not found in scan, skipping",
                               cam_data.get("id", ""), cam_data.get("key", ""))
                continue
            cam_id = cam.id
            cam.cam_type = cam_data.get("cam_type", "opencv")
            cam.label = cam_data.get("label", "")
            cam.update_config(cam_data.get("config", {}))
            cam.ready = True
            restored += 1
            logger.info("  Restored camera %s (%s)", cam_id, cam.label or cam.name)

        logger.info("Camera session restored: %d/%d cameras", restored, len(data))
        return restored > 0


camera_manager = CameraManager()
