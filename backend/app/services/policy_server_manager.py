"""
정책 서버(gRPC policy server) 프로세스 관리.
start_policy_server.py를 subprocess로 실행.
"""

import logging
from pathlib import Path

from app.services.process_manager import ProcessState
from app.services.systemd_process import make_process
from app.core.config import settings

logger = logging.getLogger(__name__)

POLICY_SERVER_SCRIPT = str(Path(__file__).resolve().parents[3] / "wrapper" / "start_policy_server.py")


class PolicyServerManager:
    def __init__(self) -> None:
        # 정책 서버는 오래 돈다 — 게이트웨이를 재시작해도 살아있어야 한다.
        # `systemd_process` 는 `ProcessManager` 와 같은 표면이라 그대로 갈아끼워진다.
        self.pm = make_process("piper-policysrv")
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

    def restore_running_process(self) -> bool:
        """게이트웨이 재시작 후 살아있는 유닛에 재부착 (`train_manager` 와 같은 계약).

        유닛은 살아 있는데 게이트웨이가 idle 로 알면 activity 에서 빠져
        **배타 모드 가드가 정책 서버를 고려하지 않는다** — 학습을 겹쳐 켜면
        GPU 경합 그대로다. 실제로 게이트웨이 재시작 뒤 이 상태를 실측했다
        (`state=idle` 인데 `pid` 는 유닛 PID).

        상태만 복구하면 주소가 기본값으로 남으므로, systemd 가 들고 있는
        ExecStart 에서 --host/--port/--fps 를 되찾는다 (`exec_argv` 의 존재 이유).
        """
        reattach = getattr(self.pm, "reattach", None)
        if reattach is None or not reattach():
            return False  # 자식 프로세스 러너거나, 유닛이 안 돈다
        for arg in self.pm.exec_argv():
            key, sep, value = arg.partition("=")
            if not sep:
                continue
            try:
                if key == "--host":
                    self.host = value
                elif key == "--port":
                    self.port = int(value)
                elif key == "--fps":
                    self.fps = int(value)
            except ValueError:
                logger.warning("정책 서버 인자 복구 실패: %s", arg)
        logger.info("정책 서버 재부착: %s (pid=%s)", self.address, self.pm.pid)
        return True

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
