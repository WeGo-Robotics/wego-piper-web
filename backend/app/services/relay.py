"""리더 팔 → 팔로워 팔 릴레이 (feature/teleoperation.md §3-A).

```
robotd ─shm state→ [리더]  ──읽기──  [RelaySession]  ──쓰기──  [팔로워] ←shm action─ robotd
                                                                    → filter_goal → JointCtrl
```

## 왜 소프트웨어가 옮기나

⚠ **펌웨어 연동이 아니다.** `can0`·`can1` 이 서로 다른 USB-CAN 어댑터에 물려 있어
(`3-11.2` / `3-11.1`) 물리 버스가 다르다 — 리더가 팔로워에게 직접 못 보낸다.

## 왜 RPC 가 아니라 shm 인가

릴레이는 초당 수십 번 읽는다. RPC 로 하면 그만큼 왕복이 생기는데, 리더의 관절값은
이미 robotd 가 shm 에 발행하고 있다. **읽는 쪽이 하나 더 붙을 뿐이다.**

마스터 팔이라 피드백(0x2Ax)을 안 보내지만, robotd 가
[`read_joints_normalized`](../../../robot/piper_robot/arm.py) 의 지령(0x15x) 폴백으로
발행하므로 shm 상태는 **사람이 끄는 대로** 따라온다.

## 안전 코드가 여기 없는 이유

조그와 같다 — 범위·변화율·데드맨은 CAN 을 쥔 robotd 것이다. 이 루프는 그 앞에
목표를 놓을 뿐이라 **robotd 변경 0줄**이다.
"""

from __future__ import annotations

import logging
import threading
import time

from app.services.teleop import (
    ArmBusyError, close_action_writer, open_action_writer, teleop_session,
)

logger = logging.getLogger(__name__)

# 릴레이 주기. 사람 손의 속도에는 넉넉하고, 데드맨보다 훨씬 빠르다.
RELAY_HZ = 30.0
DEADMAN_MS = 500

# 리더 상태가 이보다 오래되면 **보내지 않는다.** 얼어붙은 자세를 계속 밀면
# 팔로워는 그게 사람의 의도인 줄 안다.
STALE_S = 0.5


class RelayError(RuntimeError):
    """시작을 막는 이유. 호출부가 그대로 사용자에게 보여준다."""


class RelaySession:
    def __init__(self) -> None:
        self._writer = None
        self._reader = None
        self._leader: str | None = None
        self._follower: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sent = 0
        self._stale_since = 0.0

    @property
    def is_running(self) -> bool:
        return self._writer is not None

    def start(self, leader: str, follower: str) -> None:
        from piper_shm import arm as shm_arm

        with self._lock:
            if self.is_running:
                raise RelayError(f"이미 {self._leader} → {self._follower} 릴레이 중입니다")
            if leader == follower:
                raise RelayError("리더와 팔로워가 같은 팔입니다")

            try:
                reader = shm_arm.StateReader(leader)
            except Exception as exc:
                raise RelayError(
                    f"{leader} 의 상태를 읽을 수 없습니다 — 연결돼 있나요? ({exc})") from exc
            # 시작 전에 **한 번 읽어 본다.** 리더가 발행하지 않는 상태로 열면
            # 릴레이는 조용히 아무것도 안 하고, 사용자는 팔이 고장난 줄 안다.
            if reader.read() is None:
                reader.close()
                raise RelayError(f"{leader} 가 아직 관절값을 발행하지 않습니다")

            ok, why = teleop_session.start(follower, "leader")
            if not ok:
                reader.close()
                raise RelayError(why)
            try:
                self._writer = open_action_writer(follower, DEADMAN_MS)
            except ArmBusyError as exc:
                reader.close(); teleop_session.stop()
                raise RelayError(str(exc)) from exc
            except Exception as exc:
                reader.close(); teleop_session.stop()
                raise RelayError(f"명령 경로를 열지 못했습니다: {exc}") from exc

            self._reader, self._leader, self._follower = reader, leader, follower
            self._sent, self._stale_since = 0, 0.0
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name=f"relay-{leader}-{follower}")
            self._thread.start()
            logger.info("릴레이 시작: %s → %s", leader, follower)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            writer, reader = self._writer, self._reader
            leader, follower = self._leader, self._follower
            self._writer = self._reader = None
            self._leader = self._follower = None
        close_action_writer(writer, follower)
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
        if leader:
            logger.info("릴레이 정지: %s → %s (%d회 전송)", leader, follower, self._sent)
        teleop_session.stop()

    async def kill(self) -> None:
        self.stop()

    def _loop(self) -> None:
        period = 1.0 / RELAY_HZ
        while not self._stop.wait(period):
            reader, writer = self._reader, self._writer
            if reader is None or writer is None:
                return
            try:
                rec = reader.read()
            except Exception as exc:
                logger.warning("리더 상태 읽기 실패: %s", exc)
                continue
            if rec is None:
                continue

            age = (time.time_ns() - rec.get("can_wall_ns", 0)) / 1e9
            if age > STALE_S:
                # ⚠ **보내지 않는다.** 얼어붙은 자세를 계속 밀면 팔로워는 그게
                #   사람의 의도인 줄 알고 거기 서 있는다. 안 보내면 robotd 의
                #   데드맨이 팔을 그 자리에 세운다 — 그쪽이 정직하다.
                if not self._stale_since:
                    self._stale_since = time.time()
                    logger.warning("리더 %s 상태가 %.1f초 낡음 — 릴레이 중단",
                                   self._leader, age)
                continue
            if self._stale_since:
                logger.info("리더 %s 상태 복구 — 릴레이 재개", self._leader)
                self._stale_since = 0.0

            try:
                writer.publish(rec["values"])
                self._sent += 1
            except Exception as exc:
                logger.warning("릴레이 발행 실패: %s", exc)

    def status(self) -> dict:
        return {"running": self.is_running, "leader": self._leader,
                "follower": self._follower, "sent": self._sent,
                "stale": bool(self._stale_since)}


relay_session = RelaySession()
