"""so101d 로 요청을 넘기는 껍데기 — realsense_manager 와 같은 골격.

외부 로봇 데몬의 게이트웨이 쪽 통로는 이 파일 하나다 (feature/so101d.md §0):
프론트는 `/api/robots/*` 만 보고, 데몬과는 여기서만 버스 RPC 로 말한다.
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


def so101_available() -> bool:
    """so101d 생존 여부. 화면이 시리얼 패널을 그릴지 정하는 근거."""
    try:
        return bool(_bus().is_alive(C.SO101D))
    except Exception:
        return False


class So101Client:
    """RPC 클라이언트. **실패해도 게이트웨이를 죽이지 않는다** — 데몬이 없어도
    웹은 떠 있어야 한다는 격리 규칙 그대로 (rsd·robotd 클라이언트와 동일)."""

    def _call(self, method: str, *args, default=None, timeout: int = C.RPC_TIMEOUT_S):
        # 죽은 데몬을 기다리지 않는다 — 생존 표시가 없으면 즉시 포기
        # (안 그러면 폴링마다 타임아웃을 통째로 기다린다. rsd 에서 실측했다).
        try:
            if not _bus().is_alive(C.SO101D):
                return default
        except Exception:
            return default
        try:
            return _bus().rpc_call(C.SO101D, method, list(args), timeout=timeout)
        except TimeoutError:
            logger.warning("so101d 응답 없음 (%s) — 데몬이 떠 있나요?", method)
            return default
        except Exception as exc:
            logger.warning("so101d.%s 실패: %s", method, exc)
            return default

    def scan(self) -> list[dict]:
        return self._call("scan", default=[]) or []

    def attach(self, by_id: str, arm_name: str, calib: str | None = None) -> dict:
        """실패 사유를 그대로 올린다 — attach 는 사람이 누른 버튼이라
        '왜 안 되는지'(케이블 힌트·캘리브레이션 거부)가 곧 화면 문구다."""
        try:
            if not _bus().is_alive(C.SO101D):
                raise RuntimeError("so101d 가 떠 있지 않습니다")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("버스에 연결할 수 없습니다")
        return _bus().rpc_call(C.SO101D, "attach", [by_id, arm_name, calib],
                               timeout=15)

    def release(self, arm_name: str) -> bool:
        return bool(self._call("release", arm_name, default=False))

    def estop(self, arm_name: str = "") -> list[str]:
        return self._call("estop", arm_name, default=[]) or []

    def info(self) -> dict:
        return self._call("info", default={"arms": []}) or {"arms": []}

    def lost(self) -> list[dict]:
        return self._call("lost", default=[]) or []

    def set_side(self, arm: str, side: str) -> dict:
        return _bus().rpc_call(C.SO101D, "set_side", [arm, side], timeout=10)

    def calib(self, verb: str, arm: str) -> dict:
        """캘리브레이션 위저드 — begin/status/save/cancel.
        실패 사유(안 움직인 관절 등)가 곧 화면 문구라 attach 처럼 예외를 올린다."""
        return _bus().rpc_call(C.SO101D, f"calib_{verb}", [arm], timeout=15)


so101_client = So101Client()
