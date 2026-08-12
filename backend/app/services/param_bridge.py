"""게이트웨이 → wrapper 실시간 파라미터 채널.

ZMQ PUSH(`tcp://127.0.0.1:5555`) 를 Redis 큐로 교체했다
(refactor/daemon-split.md 3단계). **프로세스 경계는 그대로고 전송만 바뀐다** —
클램프 규칙도 호출부도 그대로다.

## 왜 큐(리스트)인가

pub/sub 은 구독자가 없으면 조용히 버린다. wrapper 가 정책을 로드하는 수 초 동안
보낸 값이 사라지는 레이스가 생긴다. PUSH/PULL 이 큐였으므로 리스트가 같은 시맨틱이다.

## ⚠ 세션 격리

ZMQ 는 소켓을 닫으면 큐도 사라졌지만 **Redis 리스트는 남는다.** 추론 시작 때
비우지 않으면 지난 세션 끝에 민 슬라이더 값이 다음 추론 시작 직후에 적용된다.
`clear()` 를 시작 경로에서 부르는 이유다.
"""

import asyncio
import logging

from piper_bus.client import Bus

from app.core import inference_params

logger = logging.getLogger(__name__)

# 실시간 변경 가능한 파라미터와 클램프 범위 — **PARAM_SPEC 에서 파생한다.**
# 이전에는 이 목록이 프론트 기본값·슬라이더·override_keys 와 따로 적혀 있어서
# 하나만 빠져도 조용히 값이 유실됐다 (refactor/01-inference-params.md).
SAFE_PARAMS: dict[str, dict[str, float]] = inference_params.bounds()

# Boolean 파라미터: 클램핑 대신 bool 변환
BOOL_PARAMS: set[str] = inference_params.bool_params()

# Unsafe 파라미터: 재시작 필요 (모델 아키텍처라 스펙에 없다)
UNSAFE_PARAMS = {"chunk_size", "dim_model", "n_obs_steps", "use_vae"}


class ParamBridge:
    def __init__(self, bus: Bus | None = None) -> None:
        self._bus = bus

    async def connect(self) -> None:
        """버스에 붙는다. **실패해도 서버는 뜬다.**

        Redis 가 없다고 웹이 안 뜨면 진단조차 못 한다. 대신 크게 남기고,
        `available()` 로 상태를 노출한다.
        """
        try:
            bus = Bus()
            if not await asyncio.to_thread(bus.ping):
                raise RuntimeError("ping 실패")
            self._bus = bus
            logger.info("파라미터 브리지 연결됨 (Redis)")
        except Exception as exc:
            self._bus = None
            logger.error(
                "파라미터 브리지 연결 실패 — 추론 중 실시간 파라미터 변경이 동작하지 않습니다: %s",
                exc,
            )

    async def close(self) -> None:
        self._bus = None

    def available(self) -> bool:
        return self._bus is not None

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
        if self._bus is None:
            raise RuntimeError("파라미터 브리지가 연결되지 않았습니다")
        # 이벤트 루프를 막지 않는다 — 여기서 멈추면 heartbeat 이 끊겨 E-stop 이 돈다.
        await asyncio.to_thread(self._bus.push_params, params)
        logger.debug("Sent params: %s", params)

    async def clear(self) -> int:
        """지난 세션의 잔여 파라미터를 버린다. 추론 시작 경로에서 부른다."""
        if self._bus is None:
            return 0
        dropped = await asyncio.to_thread(self._bus.clear_params)
        if dropped:
            logger.info("이전 세션의 파라미터 큐를 비웠습니다")
        return dropped


param_bridge = ParamBridge()
