"""LeRobot `--robot.cameras` JSON 생성 + 전송 방식에 맞는 카메라 준비.

## 왜 한 곳으로 모으나

같은 빌더가 **세 곳**에 있었다:

- `models.py::_build_cameras_json` (추론)
- `RecordingPage.tsx::buildCameraConfig` (녹화 — 프론트에서 조립)
- 그리고 둘 다 D405 의 `use_depth`, `warmup_s` 같은 특수사정을 각자 적고 있었다

프론트가 조립하면 백엔드 설정(`camera_transport`)을 알 수 없어서
**녹화만 계속 옛 경로를 타게 된다.** 매핑(`{키: 장치id}`)만 보내고
JSON 조립은 여기서 한 번만 한다.
"""

import logging

from app.core.config import settings
from app.services.camera_manager import camera_manager

logger = logging.getLogger(__name__)


def _wh(width: int | None, height: int | None, fps: int | None) -> dict:
    """지정된 것만 넣는다 — `None` 을 실으면 LeRobot 이 그 값으로 열려다 실패한다."""
    out = {}
    if width:
        out["width"] = width
    if height:
        out["height"] = height
    if fps:
        out["fps"] = fps
    return out


class CameraPrepareError(RuntimeError):
    """카메라를 준비하지 못했다. 라우터가 400 으로 바꾼다."""


def build_cameras_json(
    camera_mapping: dict[str, str],
    *,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
) -> dict:
    """`{"top": "/dev/video0"}` → LeRobot 카메라 설정 dict.

    `width`/`height`/`fps` 는 **`direct` 에서만 의미가 있다** — 소비자가 장치를 직접
    열면서 그 값으로 요청한다. `shm` 에서는 해상도를 발행자가 정하므로 무시한다
    (`PiperShmCamera` 가 세그먼트 값을 따르고 다르면 경고한다).

    `shm` 전송에서는 **RealSense 특수사정이 통째로 사라진다** —
    D405 의 color-only 0fps 문제(`use_depth`)도, USB 대역폭 때문에 늘린 `warmup_s` 도
    발행자 안에 갇힌다 (refactor/camera-transport.md).
    """
    if not camera_mapping:
        return {}

    if settings.camera_transport == "shm":
        # **키는 dict 키로, 세그먼트는 장치로.** 발행자는 매핑을 모른 채 항상 발행하므로
        # 세그먼트 이름이 장치에서 나와야 한다 (`piper_shm.segment_for_camera`).
        from piper_shm import segment_for_camera

        return {
            name: {
                "type": "shm",
                "segment": segment_for_camera(cam_id),
                # ⚠ LeRobot 의 `RobotConfig.__post_init__` 이 **모든 카메라에**
                # width/height/fps 를 요구한다 (없으면 draccus 파싱에서 죽는다).
                # shm 에서 진짜 해상도는 발행자가 정하므로 세그먼트에서 읽어 채운다 —
                # 요청값을 그대로 쓰면 실제와 어긋난 채로 데이터셋 메타에 박힌다.
                **_shm_dims(cam_id, width, height, fps),
            }
            for name, cam_id in camera_mapping.items() if cam_id
        }

    cameras: dict[str, dict] = {}
    for name, cam_id in camera_mapping.items():
        if not cam_id:
            continue
        if cam_id.startswith("rs:"):
            serial = cam_id.split(":")[1]
            # warmup_s 기본값(1초)은 카메라 2대가 USB 대역폭을 나눠 초기화할 때
            # 두 번째 카메라의 첫 프레임이 1초를 넘겨 connect 가 TimeoutError 로
            # 실패한다. 여유를 둬 첫 프레임 대기 상한을 늘린다.
            cfg = {"type": "intelrealsense", "serial_number_or_name": serial, "warmup_s": 5}
            cfg.update(_wh(width, height, fps))
            # D405 는 depth 가 함께 켜지지 않으면 color 프레임이 아예 안 나온다
            # (color-only=0fps → warmup TimeoutError). use_depth 는 파이프라인만
            # 켤 뿐 async_read 는 여전히 color 만 반환 → 정책/데이터셋 영향 없음.
            from app.services.realsense_manager import realsense_hub

            if realsense_hub.is_d405(serial):
                cfg["use_depth"] = True
            cameras[name] = cfg
        else:
            cameras[name] = {
                "type": "opencv", "index_or_path": cam_id, "backend": 200,
                **_wh(width, height, fps),
            }
    return cameras


# `shm` 에서 장치를 직접 여는 카메라 타입. 이게 wrapper 로 넘어가면 데몬이 쥔 장치를
# 또 열려다 `Device or resource busy` 로 죽는다 — LeRobot 3겹 안쪽에서 터져서
# 원인을 찾기 어렵다.
_DEVICE_OPENING_TYPES = {"intelrealsense", "opencv"}


def check_camera_config(cameras: dict) -> str | None:
    """전송 방식과 어긋나는 카메라 설정이면 사람이 읽을 사유, 맞으면 `None`.

    ⚠ **낡은 프론트가 보낸 설정을 그대로 넘기면 안 된다.** 실제로 vite 가 낡은
    번들을 서빙해 브라우저가 `intelrealsense` 설정을 보냈고, rsd 가 쥔 장치를
    wrapper 가 또 열려다 죽었다. 시작 전에 여기서 막는다.
    """
    if settings.camera_transport != "shm" or not cameras:
        return None
    bad = sorted(
        name for name, cfg in cameras.items()
        if isinstance(cfg, dict) and cfg.get("type") in _DEVICE_OPENING_TYPES
    )
    if not bad:
        return None
    return (
        f"카메라 전송이 shm 인데 장치를 직접 여는 설정이 왔습니다: {', '.join(bad)}. "
        "브라우저가 옛 코드를 돌고 있을 수 있습니다 — 새로고침(Ctrl+Shift+R) 후 다시 시도하세요."
    )


def _shm_dims(cam_id: str, width: int | None, height: int | None,
              fps: int | None) -> dict:
    """세그먼트의 **실제** 해상도. 아직 없으면 요청값으로 채운다.

    세그먼트가 없을 수 있는 이유: 카메라를 아직 연결하지 않았거나(미리보기 조회),
    `prepare_cameras` 보다 먼저 불렸을 때. 그 경우에도 값은 있어야 파싱이 통과한다.
    """
    from piper_shm import SegmentError, Subscriber, segment_for_camera

    w, h = width, height
    try:
        sub = Subscriber(segment_for_camera(cam_id))
    except SegmentError:
        pass
    else:
        try:
            h, w, _ = sub.shape
        finally:
            sub.close()
    return {
        "width": w or 640,
        "height": h or 480,
        # fps 는 세그먼트에 없다(발행 주기는 장치가 정한다). 요청값을 쓴다.
        "fps": fps or 30,
    }


def prepare_cameras(camera_mapping: dict[str, str], *, purpose: str) -> None:
    """전송 방식에 맞게 카메라를 준비한다. **두 방식이 정반대다.**

    - `direct`: wrapper 가 장치를 직접 여니 **웹이 쥔 것을 해제**해야 한다
    - `shm`: 발행자가 장치를 **계속 쥐고** 세그먼트에 흘려야 wrapper 가 읽는다.
      해제하면 프레임이 끊긴다

    이 뒤바뀜이 shm 전환의 핵심이다 — "해제 춤"이 사라지는 대신 소유가 명확해진다.
    """
    if settings.camera_transport != "shm":
        release_all_cameras(purpose)
        return

    # ⚠ **여기서 세그먼트를 지우지 않는다.** 소유자는 데몬(camerad/rsd)이다.
    # 예전에는 "쓰지 않는 것을 치운다"며 unlink 했는데, 데몬이 **발행 중인** 파일을
    # 지워버려 발행자는 계속 쓰고 소비자는 열 수 없는 상태가 됐다.
    # 고아 세그먼트는 각 데몬이 기동할 때 스스로 치운다.

    # 쓸 카메라를 붙잡는다. 연결돼 있으면 이미 발행 중이라 건드리지 않는다 —
    # 끊었다 붙이면 그 사이 소비자가 프레임을 잃는다.
    for name, cam_id in camera_mapping.items():
        if not cam_id:
            continue
        cam = camera_manager.cameras.get(cam_id)
        if cam is None:
            raise CameraPrepareError(f"카메라를 찾을 수 없습니다: {cam_id}")
        if not cam.connected:
            ok, msg = cam.connect()
            if not ok:
                raise CameraPrepareError(f"카메라 연결 실패 ({cam_id}): {msg}")
        logger.info("shm 발행 중(%s): %s ← %s", purpose, name, cam_id)


def release_all_cameras(purpose: str = "inference") -> bool:
    """웹 프리뷰가 점유한 카메라를 모두 해제 (`direct` 전송 전용).

    camera_manager(OpenCV)뿐 아니라 realsense_hub(RealSense 파이프라인)도 강제
    해제해야 한다. 그렇지 않으면 웹 프리뷰가 RealSense USB 디바이스를 쥔 채로
    subprocess 가 같은 디바이스를 열려다 충돌해 카메라가 먹통이 된다.
    """
    import time

    from app.services.realsense_manager import realsense_hub

    released = False
    for cam in camera_manager.cameras.values():
        if cam.connected:
            logger.info("Releasing camera %s for %s", cam.id, purpose)
            cam.disconnect()
            released = True
    if realsense_hub.release_all():
        logger.info("Released RealSense streams for %s", purpose)
        released = True
    if released:
        time.sleep(0.5)  # 커널이 디바이스를 해제할 시간 확보
    return released
