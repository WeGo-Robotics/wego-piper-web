"""simd 의 팔 RPC 클라이언트 — robot_manager._call 과 같은 골격 (feature/sim-env.md §3).

`SimArmInfo` 가 robotd 대신 이걸 부른다. 죽은 데몬을 기다리지 않는다.
"""

import logging

from piper_bus import contract as C
from piper_bus.client import Bus

logger = logging.getLogger(__name__)
_bus_singleton: Bus | None = None


def _bus() -> Bus:
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = Bus()
    return _bus_singleton


def sim_available() -> bool:
    try:
        return bool(_bus().is_alive(C.SIMD))
    except Exception:
        return False


def call(method: str, *args, default=None, timeout: int = C.RPC_TIMEOUT_S):
    try:
        if not _bus().is_alive(C.SIMD):
            return default
    except Exception:
        return default
    try:
        return _bus().rpc_call(C.SIMD, method, list(args), timeout=timeout)
    except TimeoutError:
        logger.warning("simd 응답 없음 (%s) — 데몬이 떠 있나요?", method)
        return default
    except Exception as exc:
        logger.warning("simd.%s 실패: %s", method, exc)
        return default
