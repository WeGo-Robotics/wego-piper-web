"""simd 의 카메라 허브 클라이언트 — v4l2_client 와 같은 어휘 (feature/sim-env.md §4).

`camera_manager._hub()` 의 세 번째 가지(`cam_type == "sim"`). RPC 이름은
simd 에서 `cam_` 접두사로 나르고 여기서 어휘를 되돌린다 — 호출부는 세 허브를
구분하지 않는다 (test_camerad_daemon 의 "한 어휘" 규칙).
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


def _pair(result, fallback: str) -> tuple[bool, str]:
    if isinstance(result, (list, tuple)) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return False, fallback


class SimCameraClient:
    def _call(self, method: str, *args, default=None, timeout: int = C.RPC_TIMEOUT_S):
        # 죽은 데몬을 기다리지 않는다 (v4l2_client 와 같은 단축)
        try:
            if not _bus().is_alive(C.SIMD):
                return default
        except Exception:
            return default
        try:
            return _bus().rpc_call(C.SIMD, f"cam_{method}", list(args), timeout=timeout)
        except TimeoutError:
            logger.warning("simd 응답 없음 (cam_%s) — 데몬이 떠 있나요?", method)
            return default
        except Exception as exc:
            logger.warning("simd.cam_%s 실패: %s", method, exc)
            return default

    def available(self) -> bool:
        try:
            return bool(_bus().is_alive(C.SIMD))
        except Exception:
            return False

    def scan(self) -> list[dict]:
        return self._call("scan", default=[], timeout=30) or []

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0, controls: dict | None = None) -> tuple[bool, str]:
        return _pair(self._call("connect", cam_id, width, height, fps, controls or {}, timeout=30),
                     "simd 연결 실패")

    def apply_controls(self, cam_id: str, wanted: dict) -> dict:
        return self._call("apply_controls", cam_id, wanted, default={}, timeout=30) or {}

    def lost(self) -> list[dict]:
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

    def set_light(self, scale: float) -> float:
        """시뮬 전용 — 조명 배율 (조명 감시 시험)."""
        try:
            return float(_bus().rpc_call(C.SIMD, "set_light", [scale], timeout=5))
        except Exception as exc:
            raise RuntimeError(f"조명 조절 실패: {exc}") from exc

    # ── 프레임은 shm 에서 직접 (RPC 아님) — v4l2_client 와 동일 ──

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
        from app.services.shm_snapshot import segment_jpeg

        return segment_jpeg(cam_id)


sim_camera_hub = SimCameraClient()
