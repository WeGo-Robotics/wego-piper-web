"""
E-stop watchdog.
독립적으로 heartbeat를 감시하고, 타임아웃 시 LeRobot 프로세스를 강제 종료.
"""

import asyncio
import logging
import time

from app.core.config import settings
from app.services.process_manager import process_manager

logger = logging.getLogger(__name__)


class EStopWatchdog:
    def __init__(self) -> None:
        self._last_heartbeat: float = 0.0
        self._active = False
        self._task: asyncio.Task | None = None
        self._triggered = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def triggered(self) -> bool:
        return self._triggered

    def heartbeat(self) -> None:
        """클라이언트로부터 heartbeat 수신."""
        self._last_heartbeat = time.monotonic()
        self._triggered = False

    def start(self) -> None:
        """watchdog 루프 시작."""
        if self._active:
            return
        self._active = True
        self._last_heartbeat = time.monotonic()
        self._triggered = False
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("E-stop watchdog started")

    def stop(self) -> None:
        """watchdog 루프 중지."""
        self._active = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("E-stop watchdog stopped")

    async def trigger(self) -> None:
        """E-stop 수동 트리거."""
        logger.warning("E-STOP TRIGGERED")
        self._triggered = True
        await process_manager.kill()

    async def _watch_loop(self) -> None:
        interval = settings.estop_heartbeat_interval_ms / 1000.0
        timeout = settings.estop_timeout_ms / 1000.0

        try:
            while self._active:
                await asyncio.sleep(interval)
                elapsed = time.monotonic() - self._last_heartbeat
                if elapsed > timeout:
                    logger.warning(
                        "Heartbeat timeout (%.1fs > %.1fs)", elapsed, timeout
                    )
                    await self.trigger()
        except asyncio.CancelledError:
            pass


estop_watchdog = EStopWatchdog()
