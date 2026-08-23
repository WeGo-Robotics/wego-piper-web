"""웹 조그 — 추론 없이 팔을 움직인다 (feature/manual-control.md §2, teleoperation §3-B).

```
[조그 패널] ─REST→ [JogSession] ─shm ActionWriter→ [robotd ArmBridge]
                                        → filter_goal → JointCtrl
```

## 안전 코드가 여기 없는 이유

전부 이미 있다. 필터는 **CAN 을 쥔 쪽**(robotd)에 살고, 이 세션은 그 앞에 목표를
놓을 뿐이다 — 범위 클램프도, 변화율 램프도, 데드맨도 그쪽 것이다
([robotd-safety](../../../refactor/robotd-safety.md) 의 배당금).

## 재발행하는 이유

목표 한 번에 팔이 1초쯤 움직인다. 그동안 아무것도 안 보내면 데드맨(기본 300ms)이
중간에 팔을 세워 슬라이더가 뚝뚝 끊긴다. 재발행은 **"의도가 살아있다"는 신호**다.

브라우저가 죽어도 팔은 마지막 목표까지 가서 선다 — 위치 모드라 유한한 동작이다.
게이트웨이가 죽으면 그때 데드맨이 잡는다.
"""

from __future__ import annotations

import logging
import threading
import time

from app.services.teleop import teleop_session

logger = logging.getLogger(__name__)

# 재발행 주기. 데드맨(300ms)보다 넉넉히 빨라야 한다.
REPUBLISH_HZ = 10.0
# 소비자가 선언하는 자기 주기 상한. 재발행이 이보다 늦으면 robotd 가 팔을 세운다.
DEADMAN_MS = 500
# 목표 갱신이 이만큼 없으면 세션을 닫는다. 열어둔 채 잊어버리는 것을 막는다 —
# 열려 있는 동안 추론·녹화가 막힌다.
IDLE_TIMEOUT_S = 300.0


class JogError(RuntimeError):
    """시작을 막는 이유. 호출부가 그대로 사용자에게 보여준다."""


class JogSession:
    """한 팔의 조그 수명. **한 번에 하나다** — `teleop_session` 이 그걸 지킨다."""

    def __init__(self) -> None:
        self._writer = None
        self._iface: str | None = None
        self._goal: dict[str, float] = {}
        self._last_goal_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def iface(self) -> str | None:
        return self._iface

    @property
    def is_running(self) -> bool:
        return self._writer is not None

    # ── 수명 ──

    def start(self, iface: str, current: dict[str, float]) -> None:
        """조그를 연다. 못 열면 `JogError` 로 **이유를 말한다.**"""
        from piper_shm import arm as shm_arm

        with self._lock:
            if self.is_running:
                raise JogError(f"이미 {self._iface} 를 조종 중입니다")

            # ⚠ **라이터는 이중 열기를 막아주지 않는다.** `O_CREAT` 라 기존 세그먼트를
            #   조용히 덮는다 — 추론 프록시가 조종 중이면 그 명령 경로를 가로채는 셈이다.
            #   "세그먼트 존재 = 조종 중"은 관례지 강제가 아니므로 여기서 확인한다.
            name = shm_arm.segment_name(iface, shm_arm.KIND_ACTION)
            if name in set(shm_arm.list_segments()):
                raise JogError(
                    f"{iface} 의 명령 세그먼트를 누가 이미 쥐고 있습니다 — "
                    "추론이나 녹화가 도는 중인지 보세요")

            ok, why = teleop_session.start(iface, "joint")
            if not ok:
                raise JogError(why)

            try:
                self._writer = shm_arm.ActionWriter(iface, deadman_ms=DEADMAN_MS)
            except Exception as exc:
                teleop_session.stop()
                raise JogError(f"명령 경로를 열지 못했습니다: {exc}") from exc

            self._iface = iface
            # 시작 목표는 **지금 자세**다. 0 으로 채우면 정규화 좌표의 "가운데"라
            # 그럴듯해 보이는데, 그게 첫 명령이 되면 팔이 튄다.
            self._goal = dict(current)
            self._last_goal_at = time.time()
            self._stop.clear()
            self._thread = threading.Thread(target=self._republish, daemon=True,
                                            name=f"jog-{iface}")
            self._thread.start()
            logger.info("조그 시작: %s", iface)

    def stop(self) -> None:
        """닫고 세그먼트를 지운다 — 브리지가 "소비자 종료"로 처리한다."""
        from piper_shm import arm as shm_arm

        with self._lock:
            self._stop.set()
            writer, iface = self._writer, self._iface
            self._writer, self._iface, self._goal = None, None, {}
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:
                logger.warning("조그 라이터 닫기 실패: %s", exc)
        if iface:
            # ⚠ 지운다. 남겨두면 발행자 없는 세그먼트가 되어, 게이트웨이의
            #   장치 감시가 "발행이 멈췄다"로 읽는다.
            shm_arm.unlink(shm_arm.segment_name(iface, shm_arm.KIND_ACTION))
            logger.info("조그 정지: %s", iface)
        teleop_session.stop()

    # ── 목표 ──

    def set_goal(self, values: dict[str, float]) -> dict[str, float]:
        """목표를 갱신한다. **부분 목표는 직전 값과 병합**한다.

        `ActionWriter.publish` 는 전 관절을 요구한다 — 안 온 관절을 0 으로 채우면
        그게 명령이 되어 팔이 튄다.
        """
        with self._lock:
            if not self.is_running:
                raise JogError("조그가 시작되지 않았습니다")
            self._goal.update({k: float(v) for k, v in values.items()})
            self._last_goal_at = time.time()
            goal = dict(self._goal)
        self._publish(goal)
        return goal

    def _publish(self, goal: dict[str, float]) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.publish(goal)
        except Exception as exc:
            logger.warning("조그 목표 발행 실패: %s", exc)

    def _republish(self) -> None:
        period = 1.0 / REPUBLISH_HZ
        while not self._stop.wait(period):
            with self._lock:
                if not self.is_running:
                    return
                idle = time.time() - self._last_goal_at
                goal = dict(self._goal)
            if idle > IDLE_TIMEOUT_S:
                # 열어둔 채 잊어버리면 추론·녹화가 계속 막힌다
                logger.info("조그 유휴 %.0f초 — 자동 종료", idle)
                self.stop()
                return
            self._publish(goal)

    def status(self) -> dict:
        return {"running": self.is_running, "iface": self._iface,
                "goal": dict(self._goal),
                "idle_s": round(time.time() - self._last_goal_at, 1)
                if self._last_goal_at else None}


jog_session = JogSession()
