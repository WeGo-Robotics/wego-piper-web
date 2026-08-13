"""RealSense — **rsd 데몬의 얇은 클라이언트** (daemon-inventory.md #4).

구현은 `rs/piper_rs/hub.py` 로 옮겨졌고 `daemons/rsd.py` 가 그걸 돌린다.
여기는 공개 인터페이스만 그대로 두고 내부를 버스 RPC 로 바꾼 껍데기다 —
ZMQ 브리지를 Redis 로 갈아끼울 때와 같은 방식이라 **라우터는 한 줄도 안 바뀐다.**

## 왜 프로세스를 나눴나

D405 의 UVC 컨트롤 질의가 커널 D-state 로 이벤트 루프 전체를 먹통으로 만든 전례가 있다.
게이트웨이 안에 있으면 그때 웹도 같이 멈춘다. 이제는 rsd 만 멈춘다.

## 프레임은 RPC 로 오지 않는다

`has_frame`/`get_jpeg` 는 `/dev/shm` 세그먼트에서 **직접 읽는다.** 픽셀을 요청/응답으로
나르면 그게 곧 예전 base64-JPEG 왕복이다. 버스는 제어, shm 은 픽셀 —
이 분담 덕에 `preview_bridge` 도 필요 없어진다.
"""

import logging

from piper_bus import contract as C
from piper_bus.client import Bus
from piper_shm import SegmentError, Subscriber, segment_for_camera

logger = logging.getLogger(__name__)


def rs_available() -> bool:
    """pyrealsense2 를 **게이트웨이가** 쓸 수 있는지가 아니라, rsd 가 떠 있는지."""
    try:
        return _bus().is_alive(C.RSD)
    except Exception:
        return False


_bus_singleton: Bus | None = None


def _bus() -> Bus:
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = Bus()
    return _bus_singleton


class RealSenseHub:
    """rsd 로 요청을 넘기는 껍데기. 메서드 이름·시그니처는 옛것 그대로다."""

    def _call(self, method: str, *args, default=None, timeout: int = C.RPC_TIMEOUT_S):
        """RPC 한 번. **실패해도 게이트웨이를 죽이지 않는다.**

        rsd 가 없거나 죽어 있어도 웹은 떠 있어야 한다 — 카메라만 안 보이는 것으로
        격리되는 게 프로세스를 나눈 이유다.
        """
        try:
            return _bus().rpc_call(C.RSD, method, list(args), timeout=timeout)
        except TimeoutError:
            logger.warning("rsd 응답 없음 (%s) — 데몬이 떠 있나요?", method)
            return default
        except Exception as exc:
            logger.warning("rsd.%s 실패: %s", method, exc)
            return default

    def scan(self) -> list[dict]:
        return self._call("scan", default=[]) or []

    def is_d405(self, serial: str) -> bool:
        return bool(self._call("is_d405", serial, default=False))

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0) -> tuple[bool, str]:
        r = self._call("connect", cam_id, width, height, fps, timeout=30)
        return _pair(r, "rsd 연결 실패")

    def set_depth_encoding(self, cam_id: str, near_mm: int, far_mm: int) -> tuple[bool, str]:
        """깊이 인코딩 범위 변경. 작업 공간에 맞춰 좁힐수록 해상도가 오른다."""
        return _pair(self._call("set_depth_encoding", cam_id, near_mm, far_mm),
                     "인코딩 변경 실패")

    def info(self, cam_id: str) -> dict:
        """지금 돌고 있는 프로파일. 요청값이 아니라 **장치가 연 값**이다."""
        return self._call("info", cam_id, default={}) or {}

    def disconnect(self, cam_id: str) -> None:
        self._call("disconnect", cam_id)

    def release_all(self) -> bool:
        return bool(self._call("release_all", default=False))

    def hardware_reset(self, cam_id: str) -> tuple[bool, str]:
        # 펌웨어 파워사이클 — 수 초 걸린다
        return _pair(self._call("hardware_reset", cam_id, timeout=60), "리셋 실패")

    def probe(self, cam_id: str) -> tuple[bool, str]:
        return _pair(self._call("probe", cam_id, timeout=30), "probe 실패")

    def list_controls(self, cam_id: str) -> list[dict]:
        return self._call("list_controls", cam_id, default=[]) or []

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        return bool(self._call("set_control", cam_id, name, value, default=False))

    # ── 프레임은 shm 에서 직접 (RPC 아님) ──

    def has_frame(self, cam_id: str) -> bool:
        try:
            sub = Subscriber(segment_for_camera(cam_id))
        except SegmentError:
            return False
        try:
            return sub.read() is not None
        finally:
            sub.close()

    def get_jpeg(self, cam_id: str) -> bytes | None:
        """세그먼트의 최신 프레임을 JPEG 로. 인코딩은 **여기서** 한다.

        데몬이 인코딩해서 보내면 그게 예전 base64-JPEG 왕복이 된다.
        """
        try:
            sub = Subscriber(segment_for_camera(cam_id))
        except SegmentError:
            return None
        try:
            got = sub.read()
            if got is None:
                return None
            import cv2

            ok, buf = cv2.imencode(".jpg", got[0], [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes() if ok else None
        except Exception as exc:
            logger.warning("프리뷰 인코딩 실패 (%s): %s", cam_id, exc)
            return None
        finally:
            sub.close()


def _pair(result, fallback: str) -> tuple[bool, str]:
    """RPC 결과를 `(ok, msg)` 로. JSON 왕복에서 tuple 이 list 가 되므로 여기서 복원한다."""
    if isinstance(result, (list, tuple)) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return False, fallback


realsense_hub = RealSenseHub()
