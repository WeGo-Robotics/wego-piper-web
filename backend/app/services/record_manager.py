"""
레코딩 프로세스 관리.
lerobot-record subprocess + 에피소드 상태 파싱 + 키 주입.

## 왜 systemd 유닛이 **아닌가** (ROADMAP 3b-7)

학습·정책서버·xferd 는 유닛으로 갔는데(3b-6) 녹화와 추론은 자식 프로세스로 남는다.
유닛의 값은 "게이트웨이가 죽어도 산다"인데, **이 둘은 게이트웨이가 죽으면 죽는 게
설계된 동작이다** — heartbeat 가 끊기면 estopd 가 SIGKILL 한다(실측 2.5초,
`stopped: ['recording']`). 유닛으로 감싸도 estopd 가 MainPID 를 죽이므로 결과가 같다.

즉 유닛화는 복잡도만 늘리고 수명은 하나도 안 늘린다. `ESTOP_TARGETS` 에서 녹화를
빼기로 결정한다면 그때 다시 볼 일이다 — 그 결정이 먼저다.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.services.process_manager import ProcessManager, ProcessState

logger = logging.getLogger(__name__)

# stdout 파싱 패턴
_RE_EPISODE = re.compile(r"Recording episode (\d+)")
_RE_SAVED = re.compile(r"Saved episode (\d+)")
_RE_RESET = re.compile(r"Reset")
_RE_DONE = re.compile(r"Done recording|Recording complete|Finalize")


@dataclass
class RecordStatus:
    current_episode: int = 0
    total_episodes: int = 0
    phase: str = "idle"  # idle, recording, resetting, saving, done


class RecordManager:
    def __init__(self) -> None:
        self.pm = ProcessManager()
        self.status = RecordStatus()
        self._on_status: Callable[[dict], None] | None = None
        self._original_log_cb: Callable[[str], None] | None = None

    @property
    def state(self) -> ProcessState:
        return self.pm.state

    @property
    def is_running(self) -> bool:
        return self.pm.state in (ProcessState.RUNNING, ProcessState.STARTING)

    def set_status_callback(self, cb: Callable[[dict], None]) -> None:
        self._on_status = cb

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._original_log_cb = cb
        self.pm.set_log_callback(self._intercept_log)

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self.pm.set_state_callback(cb)

    def _intercept_log(self, line: str) -> None:
        self._try_parse_status(line)
        if self._original_log_cb:
            self._original_log_cb(line)

    def _try_parse_status(self, line: str) -> None:
        changed = False

        m = _RE_EPISODE.search(line)
        if m:
            self.status.current_episode = int(m.group(1))
            self.status.phase = "recording"
            changed = True

        m = _RE_SAVED.search(line)
        if m:
            self.status.phase = "saving"
            changed = True

        if _RE_RESET.search(line):
            self.status.phase = "resetting"
            changed = True

        if _RE_DONE.search(line):
            self.status.phase = "done"
            changed = True

        if changed and self._on_status:
            self._on_status(self.get_status())

    async def start(
        self, cmd: list[str], total_episodes: int = 0,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.status = RecordStatus(total_episodes=total_episodes)
        await self.pm.start(cmd, env_extra=env_extra)

    async def stop(self) -> None:
        await self.pm.stop()

    def send_key(self, key: str) -> None:
        """에피소드 제어 명령 전송.

        `right`=지금 마감하고 저장 → 다음 (**건너뛰기가 아니다**),
        `left`=폐기 후 재녹화, `escape`=저장하고 종료.

        헤드리스라 pynput 키 주입은 불가하므로, 버스 제어 채널로 wrapper 에 명령을
        보내 LeRobot events dict 를 직접 set 한다."""
        from app.services.control_bridge import control_bridge
        if control_bridge.send(key):
            logger.info("Sent control command: %s", key)
        else:
            logger.warning("Control command not delivered: %s", key)

    def get_status(self) -> dict:
        progress = 0.0
        if self.status.total_episodes > 0:
            progress = self.status.current_episode / self.status.total_episodes
        return {
            "state": self.pm.state.value,
            "current_episode": self.status.current_episode,
            "total_episodes": self.status.total_episodes,
            "phase": self.status.phase,
            "progress": round(progress, 4),
        }


record_manager = RecordManager()
