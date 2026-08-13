"""
정책 서버(gRPC policy server) 프로세스 관리.
start_policy_server.py를 subprocess로 실행.
"""

import logging
from pathlib import Path

from app.services.process_manager import ProcessManager, ProcessState
from app.core.config import settings

logger = logging.getLogger(__name__)

POLICY_SERVER_SCRIPT = str(Path(__file__).resolve().parents[3] / "wrapper" / "start_policy_server.py")


def _make_process():
    """설정이 고른 프로세스 소유자. **못 쓰면 조용히 떨어지지 않고 말한다.**"""
    from app.services.systemd_process import SystemdProcess, available

    if settings.process_runner != "systemd":
        return ProcessManager()
    ok, why = available()
    if not ok:
        logger.warning("systemd 를 쓸 수 없어 자식 프로세스로 돕니다 — %s "
                       "(게이트웨이를 재시작하면 정책 서버가 화면에서 사라집니다)", why)
        return ProcessManager()
    return SystemdProcess("piper-policysrv")


class PolicyServerManager:
    def __init__(self) -> None:
        # 정책 서버는 오래 돈다 — 게이트웨이를 재시작해도 살아있어야 한다.
        # `systemd_process` 는 `ProcessManager` 와 같은 표면이라 그대로 갈아끼워진다.
        self.pm = _make_process()
        self.host: str = "127.0.0.1"
        self.port: int = 8088
        self.fps: int = 30

    @property
    def state(self) -> ProcessState:
        return self.pm.state

    @property
    def is_running(self) -> bool:
        return self.pm.state in (ProcessState.RUNNING, ProcessState.STARTING)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def start(self, host: str = "127.0.0.1", port: int = 8088, fps: int = 30) -> None:
        self.host = host
        self.port = port
        self.fps = fps
        cmd = [
            settings.grpc_python, "-u", POLICY_SERVER_SCRIPT,
            f"--host={host}",
            f"--port={port}",
            f"--fps={fps}",
        ]
        await self.pm.start(cmd)

    async def stop(self) -> None:
        await self.pm.stop()

    def get_status(self) -> dict:
        return {
            "state": self.pm.state.value,
            "pid": self.pm.pid,
            "address": self.address,
            "host": self.host,
            "port": self.port,
            "fps": self.fps,
        }


policy_server_manager = PolicyServerManager()
