"""v4l2 — **camerad 데몬의 얇은 클라이언트**.

구현은 `cam/piper_cam/` 이고 `daemons/camerad.py` 가 돌린다.
`realsense_manager` 와 **같은 메서드 이름**이라 `CameraInfo` 가 `cam_type` 으로
허브만 고르면 된다 — 호출부에 분기가 흩어지지 않는다.
"""

import logging

from piper_bus import contract as C
from piper_bus.client import Bus
from piper_shm import SegmentError, Subscriber, segment_for_camera

logger = logging.getLogger(__name__)

_bus_singleton: Bus | None = None


def _bus() -> Bus:
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = Bus()
    return _bus_singleton


class V4l2Client:
    def _call(self, method: str, *args, default=None, timeout: int = C.RPC_TIMEOUT_S):
        """**실패해도 게이트웨이를 죽이지 않는다** — 카메라만 안 보이는 것으로 격리된다."""
        # ⚠ **죽은 데몬을 기다리지 않는다.** 생존 표시가 없으면 즉시 포기한다 —
        # 안 그러면 호출마다 RPC 타임아웃(스캔은 30초)을 통째로 기다린다.
        # 카메라 하나당 `has_frame`·`info` 까지 도니 스캔 한 번이 분 단위로 멈췄다
        # (실제로 그랬다). 격리하려고 프로세스를 나눈 건데 그러면 의미가 없다 —
        # `robot_manager._call` 이 같은 이유로 먼저 이 단축을 갖고 있다.
        try:
            if not _bus().is_alive(C.CAMERAD):
                return default
        except Exception:
            return default
        try:
            return _bus().rpc_call(C.CAMERAD, method, list(args), timeout=timeout)
        except TimeoutError:
            logger.warning("camerad 응답 없음 (%s) — 데몬이 떠 있나요?", method)
            return default
        except Exception as exc:
            logger.warning("camerad.%s 실패: %s", method, exc)
            return default

    def available(self) -> bool:
        try:
            return _bus().is_alive(C.CAMERAD)
        except Exception:
            return False

    def scan(self) -> list[dict]:
        return self._call("scan", default=[], timeout=30) or []

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0, controls: dict | None = None) -> tuple[bool, str]:
        return _pair(
            self._call("connect", cam_id, width, height, fps, controls or {}, timeout=30),
            "camerad 연결 실패")

    def apply_controls(self, cam_id: str, wanted: dict) -> dict:
        return self._call("apply_controls", cam_id, wanted, default={}, timeout=30) or {}

    def lost(self) -> list[dict]:
        """**데몬이 판정한** 사라진 장치. 게이트웨이가 세그먼트로 추론하지 않는다.

        ⚠ 타임아웃이 **짧다.** 이건 2초마다 도는 상태 조회지 작업이 아니다.
        기본값(20초)이면 데몬이 막 죽었을 때 생존 표시가 아직 만료 안 된 창에서
        감시자가 통째로 20초를 기다린다 — 그동안 다른 장치의 사라짐도 못 본다.
        """
        return self._call("lost", default=[], timeout=2) or []

    def last_apply_report(self, cam_id: str) -> dict:
        return self._call("last_apply_report", cam_id, default={}) or {}

    def disconnect(self, cam_id: str) -> None:
        self._call("disconnect", cam_id)

    def release_all(self) -> bool:
        return bool(self._call("release_all", default=False))

    def probe(self, cam_id: str) -> tuple[bool, str]:
        return _pair(self._call("probe", cam_id, timeout=30), "probe 실패")

    def list_controls(self, cam_id: str) -> list[dict]:
        return self._call("list_controls", cam_id, default=[]) or []

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        return bool(self._call("set_control", cam_id, name, value, default=False))

    def info(self, cam_id: str) -> dict:
        return self._call("info", cam_id, default={}) or {}

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
    """JSON 왕복에서 tuple 이 list 가 되므로 복원한다."""
    if isinstance(result, (list, tuple)) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return False, fallback


v4l2_hub = V4l2Client()
