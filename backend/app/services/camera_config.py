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

    # fps 는 세그먼트 헤더에 없다(발행 주기는 장치가 정한다). 데몬에 물어본다 —
    # ⚠ **요청값을 그대로 쓰면 데이터셋이 거짓말을 한다.** D405 는 848x480 에서
    # 10fps 가 상한이라, 15 를 요청해도 실제로는 10 으로 돈다. 그 상태로 15 라고
    # 적으면 LeRobot 이 매 프레임 "루프가 느리다" 경고를 뱉고 타임스탬프도 어긋난다.
    actual_fps = 0
    cam = camera_manager.cameras.get(cam_id)
    if cam is not None:
        try:
            actual_fps = int(cam.running_profile().get("fps") or 0)
        except Exception as exc:
            logger.warning("실제 fps 조회 실패 (%s): %s", cam_id, exc)
    return {
        "width": w or 640,
        "height": h or 480,
        "fps": actual_fps or fps or 30,
    }


# 첫 프레임 대기 상한. 파이프라인 재시작 + RealSense 안정화(SETTLE_S)를 덮어야 한다.
FIRST_FRAME_TIMEOUT_S = 10.0


def _wait_for_frame(cam_id: str, timeout_s: float = FIRST_FRAME_TIMEOUT_S,
                    want_wh: tuple[int, int] | None = None) -> bool:
    """세그먼트가 생기고 **요청한 크기의 프레임이 들어올 때까지** 기다린다.

    세그먼트 파일이 생기는 것과 프레임이 담기는 것은 다르다 — 파일만 보고 넘기면
    소비자가 빈 세그먼트를 읽어 `None` 을 받는다.

    ⚠ **크기까지 봐야 한다.** 해상도를 바꿔 다시 여는 경우, 재시작이 끝나기 전에도
      세그먼트에는 **직전 해상도의 프레임**이 그대로 남아 있다. 그걸 보고 넘기면
      뒤따르는 `build_cameras_json` 이 옛 크기를 읽어 CLI 에 박고, 정작 녹화가
      붙을 때는 새 크기라 LeRobot 이 첫 프레임에서 죽는다:

          WARNING PiperShmCamera(rs_335122270699_color):
                  세그먼트가 640x480 인데 설정은 848x480 — 세그먼트 값을 따릅니다
          ValueError: The feature 'observation.images.right_hand' of shape
                      '(480, 640, 3)' does not have the expected shape '(480, 848, 3)'

      카메라는 세그먼트를 따라가는데 **데이터셋 스키마는 못 따라간다** — 그쪽은
      시작할 때 한 번 정해진다. 그래서 여기서 맞을 때까지 기다린다.
    """
    import time

    from piper_shm import SegmentError, Subscriber, segment_for_camera

    name = segment_for_camera(cam_id)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            sub = Subscriber(name)
        except SegmentError:
            time.sleep(0.05)
            continue
        try:
            if sub.read() is not None:
                if want_wh is None:
                    return True
                h, w = sub.shape[0], sub.shape[1]
                if (w, h) == want_wh:
                    return True
        finally:
            sub.close()
        time.sleep(0.05)
    return False


def prepare_cameras(camera_mapping: dict[str, str], *, purpose: str,
                    width: int = 0, height: int = 0, fps: int = 0) -> None:
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

    # 쓸 카메라를 붙잡는다. 요청 프로파일을 **여기서** 넘긴다 — 데몬이 그걸 받아야
    # UI 에서 고른 해상도·fps 가 실제 장치에 반영된다. 예전에는 요청이 데몬까지
    # 가지 않아 librealsense 기본값(D405 는 848x480@10)으로만 돌았고, 녹화 루프가
    # 그 10Hz 에 묶여 매 프레임 "루프가 느리다" 경고가 떴다.
    for name, cam_id in camera_mapping.items():
        if not cam_id:
            continue
        cam = camera_manager.cameras.get(cam_id)
        if cam is None:
            raise CameraPrepareError(f"카메라를 찾을 수 없습니다: {cam_id}")
        # ⚠ **없는 장치를 열려고 시도하지 않는다.** 시도하면 "카메라 연결 실패" 라는
        # 애매한 문구가 나오는데, 원인은 설정이 아니라 **케이블**이다.
        # 사용자가 겪은 그대로다: 뽑힌 채로 녹화를 시작하면 그냥 에러가 났다.
        if not cam.present:
            raise CameraPrepareError(
                f"카메라 '{cam.label or cam.name}'({cam_id})가 연결돼 있지 않습니다. "
                "USB 를 확인하고 카메라 페이지에서 스캔하세요.")
        got = cam.running_profile()
        # 같은 요청을 이미 반영했으면 건드리지 않는다. 끊었다 붙이면 그 사이
        # 소비자가 프레임을 잃고, rsd 쪽은 refcount 만 늘어난다.
        # ⚠ 실행 중인 프로파일이 아니라 **요청**을 비교한다 — 장치가 못 내는 조합은
        # 근사로 열리므로 실행값끼리 비교하면 영원히 "다르다"가 되어 매번 재연결한다.
        want = [int(width), int(height), int(fps)] if all((width, height, fps)) else []
        # 요청이 없으면(추론처럼 해상도를 안 따지는 경우) 지금 돌고 있는 무엇이든 좋다.
        # 여기서 want 까지 비교하면, 녹화가 남긴 요청 때문에 추론이 시작될 때마다
        # 쓸데없이 재연결한다.
        satisfied = got.get("connected") and (not want or got.get("want") == want)
        if not satisfied:
            ok, msg = cam.connect(width, height, fps)
            if not ok:
                raise CameraPrepareError(f"카메라 연결 실패 ({cam_id}): {msg}")
            got = cam.running_profile()
        # ⚠ **첫 프레임까지 기다린다.** `connect` 는 파이프라인을 시작시킬 뿐이고,
        # 세그먼트는 첫 프레임이 발행될 때 생긴다. 여기서 안 기다리면 곧바로 뜨는
        # subprocess 가 아직 없는 세그먼트를 열어 `SegmentError` 로 죽는다 —
        # 실제로 파이프라인을 다시 세워야 했던 D435 만 이 경합에 걸렸다.
        # 요청한 크기가 있으면 **그 크기가 실제로 나올 때까지** 기다린다.
        # 장치가 못 내는 조합이면 근사로 열리므로 실행값(`got`)을 기준으로 본다 —
        # 요청값을 기준으로 하면 영원히 안 맞아 타임아웃이 난다.
        want_wh = None
        if got.get("width") and got.get("height"):
            want_wh = (int(got["width"]), int(got["height"]))
        if not _wait_for_frame(cam_id, want_wh=want_wh):
            raise CameraPrepareError(
                f"카메라가 프레임을 내지 않습니다 ({cam_id}): "
                f"{FIRST_FRAME_TIMEOUT_S}초 안에 세그먼트가 "
                f"{want_wh[0]}x{want_wh[1]} 로 채워지지 않았습니다"
                if want_wh else
                f"카메라가 프레임을 내지 않습니다 ({cam_id}): "
                f"{FIRST_FRAME_TIMEOUT_S}초 안에 세그먼트가 채워지지 않았습니다"
            )
        logger.info("shm 발행 중(%s): %s ← %s (%sx%s@%s)", purpose, name, cam_id,
                    got.get("width"), got.get("height"), got.get("fps"))
        # 장치가 요청을 못 맞췄으면 **말한다.** 조용히 낮춰 열면 녹화 루프가
        # 느린 카메라에 묶여 매 프레임 경고를 뱉는데, 원인이 어디인지 안 보인다.
        if want and [got.get("width"), got.get("height"), got.get("fps")] != want:
            logger.warning(
                "%s: %dx%d@%d 를 요청했지만 장치가 %sx%s@%s 로 열렸습니다. "
                "녹화 루프는 가장 느린 카메라에 맞춰집니다.",
                cam_id, want[0], want[1], want[2],
                got.get("width"), got.get("height"), got.get("fps"),
            )


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


# ── 데이터셋 사이드카 ──
#
# ⚠ **LeRobot 은 카메라 설정을 `meta/info.json` 에 안 적는다.** 거기엔 feature 모양만
# 있고, 깊이 인코딩 파라미터처럼 "이 픽셀값이 무슨 거리였나"를 아는 데 필요한 값은
# 어디에도 안 남는다. 그러면 나중에 클리핑을 바꿔 녹화한 데이터와 섞였을 때
# 구별할 방법이 없다 — 에러 없이 정책만 나빠지는 종류의 오염이다.
#
# LeRobot 을 고치지 않는다는 원칙이 있으므로, 우리가 아는 것을 옆에 적는다.
SIDECAR_NAME = "piper_cameras.json"


def camera_sidecar(camera_mapping: dict[str, str]) -> dict:
    """녹화에 쓴 카메라의 **해석에 필요한 값**. 데이터셋 옆에 남긴다.

    프레임 자체로는 알 수 없는 것만 담는다 — 해상도는 비디오에 있지만
    깊이 인코딩 범위는 어디에도 없다.
    """
    out: dict = {"cameras": {}}
    for name, cam_id in camera_mapping.items():
        if not cam_id:
            continue
        cam = camera_manager.cameras.get(cam_id)
        info = cam.running_profile() if cam else {}
        entry = {k: info.get(k) for k in ("width", "height", "fps") if info.get(k)}
        entry["id"] = cam_id
        if info.get("depth_encoding"):
            entry["depth_encoding"] = info["depth_encoding"]
        # ⚠ 배경이 지워진 데이터인지는 **프레임만 봐서는 못 가린다** — 어두운 배경과
        #   지워진 배경이 똑같이 검다. 여기 안 남기면 영영 모른다.
        if info.get("background_mask"):
            entry["background_mask"] = info["background_mask"]
        out["cameras"][name] = entry
    return out


def write_camera_sidecar(dataset_root, camera_mapping: dict[str, str]) -> bool:
    """`meta/piper_cameras.json` 을 쓴다. 실패해도 녹화 흐름을 막지 않는다."""
    import json
    from pathlib import Path

    try:
        meta = Path(dataset_root) / "meta"
        if not meta.exists():
            logger.warning("데이터셋 meta 가 없어 카메라 정보를 못 남깁니다: %s", meta)
            return False
        (meta / SIDECAR_NAME).write_text(
            json.dumps(camera_sidecar(camera_mapping), indent=2, ensure_ascii=False)
        )
        logger.info("카메라 정보 기록: %s", meta / SIDECAR_NAME)
        return True
    except Exception as exc:
        logger.warning("카메라 정보 기록 실패: %s", exc)
        return False
