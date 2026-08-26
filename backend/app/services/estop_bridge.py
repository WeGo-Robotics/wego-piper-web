"""게이트웨이 ↔ estopd 연결.

E-stop 감시 자체는 **독립 프로세스**(`daemons/estopd.py`)가 한다. 게이트웨이는 두 가지만 한다:

1. heartbeat 와 활동 PID 를 버스에 **올린다** (estopd 가 읽는다)
2. estopd 가 죽인 결과를 **읽어** UI 에 표시한다

정지 자체는 estopd 가 PID 를 직접 kill 하므로 **게이트웨이가 멈춰 있어도 팔은 선다.**
이게 in-process 워치독과의 결정적 차이다 (refactor/daemon-inventory.md #1).

버스가 없어도 게이트웨이는 떠야 한다 — Redis 미실행 시 조용히 비활성으로 동작하고
`bus_available=False` 로 알린다.
"""

import logging
import time
import os

from app.core.config import settings
from app.services import exclusivity
from app.services.exclusivity import Activity

logger = logging.getLogger(__name__)

# estopd 가 죽일 대상 = 로봇을 물리적으로 움직이는 활동
_TRACKED: tuple[Activity, ...] = tuple(exclusivity.ESTOP_TARGETS)


class EstopBridge:
    def __init__(self) -> None:
        self._bus = None
        self._available = False
        self._last_beat: float | None = None
        self._last_seq: int | None = None
        self._log_next = False
        self._gap_seq: int | None = None

    def connect(self) -> bool:
        try:
            from piper_bus import Bus

            bus = Bus()
            if not bus.ping():
                raise RuntimeError("ping 실패")
            self._bus, self._available = bus, True
            logger.info("E-stop 버스 연결됨 — 감시는 estopd 가 한다")
        except Exception as e:
            self._bus, self._available = None, False
            logger.warning(
                "E-stop 버스에 연결할 수 없습니다 (%s). "
                "estopd 가 없으면 heartbeat 타임아웃 정지가 동작하지 않는다.", e
            )
        return self._available

    @property
    def available(self) -> bool:
        return self._available

    # ── 게이트웨이 → 버스 ──

    # heartbeat 가 이 간격보다 벌어지면 남긴다. estopd 한도(2.0s)보다 낮게 잡아
    # **터지기 전의 조짐**까지 본다 — 실측 실패가 2.1s 였으므로 2.0s 로는 늦다.
    GAP_WARN_S = 1.0

    def heartbeat(self, client: dict | None = None) -> None:
        """브라우저 생존 신호. **도착 간격을 잰다.**

        게이트웨이는 이 요청에 0.3s 넘게 걸린 적이 없는데도 estopd 가 2.1s 공백을
        봤다. 그렇다면 늦은 것은 여기가 아니라 **보내는 쪽**이므로, 서버가 본
        간격과 클라이언트가 스스로 잰 간격을 나란히 남겨 어디서 벌어졌는지 가른다:

        - 둘 다 크다 → 브라우저 타이머가 멎었다 (탭 throttle, 메인 스레드 정체)
        - 서버만 크다 → 전송 구간 (네트워크, 프록시, 서버 큐)
        - `hidden=True` → 탭이 백그라운드였다
        """
        now = time.monotonic()
        prev, self._last_beat = self._last_beat, now
        info = client or {}
        seq = info.get("seq")
        prev_seq, self._last_seq = self._last_seq, seq if seq is not None else self._last_seq

        if prev is not None and (now - prev) >= self.GAP_WARN_S:
            # 빠진 번호 = 그 사이 요청이 **아예 안 왔다**. 번호가 이어지는데 간격만
            # 벌어졌다면 늦게 온 것이다 — 둘은 고칠 곳이 다르다.
            missed = ""
            if seq is not None and prev_seq is not None:
                lost = seq - prev_seq - 1
                missed = f", 유실 {lost}건" if lost > 0 else ", 유실 없음"
            logger.warning(
                "heartbeat 간격 %.2fs (%s, 타이머 %sms, 왕복 %sms, hidden=%s%s)",
                now - prev, info.get("via", "?"), info.get("gap", "?"),
                info.get("rtt", "?"), info.get("hidden", "?"), missed,
            )
            # ⚠ 위 `왕복` 은 **직전** 요청 것이다 — 방금 늦은 그 요청의 왕복은
            #   클라이언트가 아직 모른다(보낸 뒤에야 안다). 그래서 바로 다음 비트를
            #   한 줄 더 찍는다. 거기 실려 오는 값이 **늦은 요청 자신의 왕복**이다.
            self._log_next = True
            self._gap_seq = seq
        elif self._log_next:
            self._log_next = False
            # ⚠ 짝이 맞을 때만 의미가 있다. 겹친 요청 때문에 다른 순번의 왕복이
            #   실려 오면 그건 늦었던 그 요청의 값이 아니다 — 그걸 모른 채 읽어서
            #   같은 숫자를 두 번 찍고 있었다.
            got, want = info.get("rttSeq"), self._gap_seq
            if got is not None and want is not None and got != want:
                logger.warning("  ↑ 늦었던 요청(#%s)의 왕복을 못 받았다 (받은 것: #%s)",
                               want, got)
            else:
                logger.warning("  ↑ 늦었던 요청(#%s)의 왕복 %sms",
                               want, info.get("rtt", "?"))

        if self._bus:
            try:
                self._bus.beat()
            except Exception as e:
                logger.debug("heartbeat 전송 실패: %s", e)

    def sync_activities(self) -> None:
        """지금 실행 중인 활동의 PID 를 버스에 올린다.

        estopd 는 이 PID 를 직접 죽인다. 상태가 바뀔 때마다 부른다.
        """
        if not self._bus:
            return
        try:
            any_running = False
            for activity in _TRACKED:
                pid = _pid_of(activity)
                self._bus.set_activity_pid(activity.value, pid)
                any_running = any_running or pid is not None
            # 로봇을 움직이는 게 하나도 없으면 감시를 끈다 (오탐 방지)
            self._bus.set_armed(any_running)
            if any_running:
                self._bus.beat()  # arm 직후 즉시 타임아웃 나지 않도록
        except Exception as e:
            logger.debug("활동 동기화 실패: %s", e)

    # ── 버스 → 게이트웨이 ──

    def status(self) -> dict:
        armed, last = False, None
        if self._bus:
            try:
                armed = self._bus.is_armed()
                last = self._bus.last_estop()
            except Exception:
                pass
        return {
            "bus_available": self._available,
            "armed": armed,
            "last_trigger": last,
            "timeout_ms": settings.estop_timeout_ms,
            "heartbeat_interval_ms": settings.estop_heartbeat_interval_ms,
        }

    async def trigger_manual(self) -> list[str]:
        """수동 E-stop.

        게이트웨이가 살아 있는 경우이므로 여기서 바로 죽인다 (버스 왕복을 기다리지 않는다).
        버스에도 기록해 estopd 와 UI 가 같은 것을 본다.
        """
        stopped = await exclusivity.estop_all()
        if self._bus:
            try:
                from piper_bus import contract as C

                self._bus.record_estop(
                    C.ESTOP_REASON_MANUAL, [a.value for a in stopped], []
                )
                self.sync_activities()
            except Exception as e:
                logger.debug("E-stop 기록 실패: %s", e)
        return [a.value for a in stopped]


def _pid_of(activity: Activity) -> int | None:
    """활동이 실행 중이면 그 subprocess PID."""
    if not exclusivity.is_running(activity):
        return None
    from app.services.process_manager import process_manager
    from app.services.record_manager import record_manager

    pm = {Activity.INFERENCE: process_manager, Activity.RECORDING: record_manager.pm}.get(activity)
    return pm.pid if pm else None


def env_for_daemon() -> dict[str, str]:
    """estopd 에 넘길 환경변수 — 타임아웃이 두 곳에서 갈리지 않게 한다."""
    return {
        "PIPER_ESTOP_TIMEOUT_S": str(settings.estop_timeout_ms / 1000.0),
        "PIPER_ESTOP_POLL_S": str(
            max(0.05, settings.estop_heartbeat_interval_ms / 1000.0 / 2)
        ),
        **({"PIPER_REDIS_URL": os.environ["PIPER_REDIS_URL"]}
           if "PIPER_REDIS_URL" in os.environ else {}),
    }


estop_bridge = EstopBridge()
