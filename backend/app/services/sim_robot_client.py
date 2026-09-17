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


def call_strict(method: str, *args, timeout: int = C.RPC_TIMEOUT_S):
    """**오류를 그대로 올린다** — 사람이 고칠 수 있는 실패에 쓴다.

    ⚠ `call()` 은 무엇이 잘못돼도 `default` 를 돌려준다. 그건 폴링·표시처럼 "없으면 없는
    대로"인 자리에 맞고, 사람이 고칠 수 있는 일(장면 JSON 이 규칙을 어겼다)에는 **정확히
    틀린 도구**다 — 화면에 "실패했습니다"만 뜨고 무엇이 왜 틀렸는지가 사라진다. 이 저장소가
    같은 실수를 여러 번 했다(카메라 RPC 의 `_why`, camerad "알 수 없는 메서드").
    """
    if not _bus().is_alive(C.SIMD):
        raise RuntimeError("simd 가 응답하지 않습니다 — 시뮬 데몬이 떠 있나요?")
    return _bus().rpc_call(C.SIMD, method, list(args), timeout=timeout)


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
