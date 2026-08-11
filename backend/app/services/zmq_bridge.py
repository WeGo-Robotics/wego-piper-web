"""
FastAPI ↔ LeRobot 래퍼 간 ZMQ 브릿지.
PUSH 소켓으로 래퍼에 파라미터 전송.
"""

import logging

import zmq
import zmq.asyncio

from app.core import inference_params
from app.core.config import settings

logger = logging.getLogger(__name__)

# 실시간 변경 가능한 파라미터와 클램프 범위 — **PARAM_SPEC 에서 파생한다.**
# 이전에는 이 목록이 프론트 기본값·슬라이더·override_keys 와 따로 적혀 있어서
# 하나만 빠져도 조용히 값이 유실됐다 (refactor/01-inference-params.md).
SAFE_PARAMS: dict[str, dict[str, float]] = inference_params.bounds()

# Boolean 파라미터: 클램핑 대신 bool 변환
BOOL_PARAMS: set[str] = inference_params.bool_params()

# Unsafe 파라미터: 재시작 필요 (모델 아키텍처라 스펙에 없다)
UNSAFE_PARAMS = {"chunk_size", "dim_model", "n_obs_steps", "use_vae"}


class ZmqBridge:
    def __init__(self) -> None:
        self._ctx: zmq.asyncio.Context | None = None
        self._socket: zmq.asyncio.Socket | None = None

    async def connect(self) -> None:
        self._ctx = zmq.asyncio.Context()
        self._socket = self._ctx.socket(zmq.PUSH)
        self._socket.connect(settings.zmq_address)
        logger.info("ZMQ bridge connected to %s", settings.zmq_address)

    async def close(self) -> None:
        if self._socket:
            self._socket.close()
        if self._ctx:
            self._ctx.term()

    def validate_params(self, params: dict) -> tuple[dict, list[str]]:
        """
        파라미터 검증 및 클램핑.
        Returns: (클램핑된 safe params, unsafe param 이름 리스트)
        """
        safe = {}
        unsafe_found = []

        for key, value in params.items():
            if key == "task":
                safe[key] = str(value)
                continue
            if key in BOOL_PARAMS:
                safe[key] = bool(value)
                continue
            if key in UNSAFE_PARAMS:
                unsafe_found.append(key)
                continue
            if key in SAFE_PARAMS:
                bounds = SAFE_PARAMS[key]
                clamped = max(bounds["min"], min(bounds["max"], value))
                safe[key] = clamped

        return safe, unsafe_found

    async def send_params(self, params: dict) -> None:
        """래퍼에 파라미터 전송."""
        if not self._socket:
            raise RuntimeError("ZMQ bridge not connected")
        await self._socket.send_json(params)
        logger.debug("Sent params: %s", params)


zmq_bridge = ZmqBridge()
