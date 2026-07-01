"""
FastAPI ↔ LeRobot 래퍼 간 ZMQ 브릿지.
PUSH 소켓으로 래퍼에 파라미터 전송.
"""

import logging

import zmq
import zmq.asyncio

from app.core.config import settings

logger = logging.getLogger(__name__)

# Safe 파라미터: 실시간 변경 가능
SAFE_PARAMS = {
    "max_guidance_weight": {"min": 0.0, "max": 50.0},
    "execution_horizon": {"min": 1, "max": 100},
    "temporal_ensemble_coeff": {"min": 0.0, "max": 1.0},
    "n_action_steps": {"min": 1, "max": 100},
    "fps": {"min": 1, "max": 60},
    "max_velocity": {"min": 0, "max": 1000},
    "max_gripper_velocity": {"min": 0, "max": 500},
    "lowpass_alpha": {"min": 0.05, "max": 1.0},
    "max_jerk": {"min": 0, "max": 5000},
    "interpolation_steps": {"min": 0, "max": 10},
    "use_chunk_size": {"min": 0, "max": 200},
    "refill_threshold_pct": {"min": 0, "max": 100},
}

# Unsafe 파라미터: 재시작 필요
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
