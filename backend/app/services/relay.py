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

## 두 가지 모드

| | 관절 복제 (`joint`) | 6D 자세 (`pose`) |
|---|---|---|
| 리더에서 읽는 것 | 관절값 | 관절값 → **FK** → 말단 6D |
| 팔로워에 주는 것 | 관절 목표 (shm) | 말단 목표 (`EndPoseCtrl`, MOVE P) |
| 팔로워 관절을 정하는 것 | 우리 | **팔의 온보드 IK** |
| robotd 안전 필터 | ✅ 탄다 | ❌ **안 탄다** |

⚠ **리더는 자기 말단 자세를 안 알려준다.** 마스터로 설정된 팔은 피드백(0x2Ax)을
내지 않아 `GetArmEndPoseMsgs` 가 0,0,0 을 돌려준다(실측). 그래서 POSE 모드는
FK 로 구한다 — 팔로워에서 대조한 결과 위치 0.08mm·자세 0.002° 로 맞는다.

## 안전 코드가 여기 없는 이유 — `joint` 모드에 한해서다

관절 복제는 조그와 같다: 범위·변화율·데드맨은 CAN 을 쥔 robotd 것이고, 이 루프는
그 앞에 목표를 놓을 뿐이라 **robotd 변경 0줄**이다.

**`pose` 모드는 다르다.** 관절을 우리가 안 정하므로 `filter_goal` 이 걸 자리가
없다 — 바닥 필터도, 관절 범위도, 변화율 상한도 전부 빠진다. 그래서 막는 것을
**여기 다 둔다**: 작업공간 상자, 한 걸음 상한, 짐벌락, 바닥 근사 검사.
"""

from __future__ import annotations

import logging
import threading
import time

from app.services.teleop import (
    ArmBusyError, close_action_writer, enable_torque, open_action_writer,
    require_healthy_bus, teleop_session,
)

logger = logging.getLogger(__name__)

# 릴레이 주기. 사람 손의 속도에는 넉넉하고, 데드맨보다 훨씬 빠르다.
RELAY_HZ = 30.0
DEADMAN_MS = 500

# 리더 상태가 이보다 오래되면 **보내지 않는다.** 얼어붙은 자세를 계속 밀면
# 팔로워는 그게 사람의 의도인 줄 안다.
STALE_S = 0.5

# ── POSE 모드 전용 ──
#
# 말단 목표는 MOVE P 로 나가고 팔이 스스로 보간한다. 관절 스트림보다 느려도
# 되고, 오히려 너무 빠르면 팔이 직전 목표에 닿기 전에 다음 것이 덮어써 떨린다.
POSE_HZ = 15.0

# 한 주기에 허용할 말단 이동. 리더가 튀거나(낡은 상태 복구 직후) 짐벌 근처를
# 지날 때 **큰 MoveP 한 방**이 나가는 것을 막는다.
POSE_MAX_STEP_MM = 30.0
POSE_MAX_STEP_DEG = 20.0


#: POSE 모드에는 명령 세그먼트가 없다. `_writer` 자리에 이걸 넣어 "열려 있음"만 표시한다.
_POSE_MODE = object()


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
        self._mode = "joint"
        self._last_pose: dict | None = None
        #: POSE 모드에서 왜 안 보내고 있나. 화면이 그대로 보여준다.
        self._blocked = ""

    @property
    def is_running(self) -> bool:
        return self._writer is not None

    def start(self, leader: str, follower: str, mode: str = "joint") -> None:
        from piper_shm import arm as shm_arm

        with self._lock:
            if self.is_running:
                raise RelayError(f"이미 {self._leader} → {self._follower} 릴레이 중입니다")
            if leader == follower:
                raise RelayError("리더와 팔로워가 같은 팔입니다")
            if mode not in ("joint", "pose"):
                raise RelayError(f"모르는 모드입니다: {mode}")

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
            # ⚠ **POSE 모드에서는 명령 세그먼트를 열지 않는다.** 열어 놓고 관절
            #   목표를 안 쓰면 robotd 의 데드맨이 "현재 자세 유지"를 JointCtrl 로
            #   내려보내고, 그게 우리 MoveP 와 힘겨루기를 한다.
            if mode == "joint":
                try:
                    self._writer = open_action_writer(follower, DEADMAN_MS)
                except ArmBusyError as exc:
                    reader.close(); teleop_session.stop()
                    raise RelayError(str(exc)) from exc
                except Exception as exc:
                    reader.close(); teleop_session.stop()
                    raise RelayError(f"명령 경로를 열지 못했습니다: {exc}") from exc
            else:
                # 세션이 열려 있다는 표시가 필요하다 — `is_running` 이 이걸 본다
                self._writer = _POSE_MODE

            # 버스가 죽어 있으면 여기서 말한다 — 안 그러면 슬라이더는
            # 움직이는데 팔만 안 움직여 소프트웨어를 의심하게 된다
            require_healthy_bus(follower)
            # 토크부터 켠다 — 안 켜면 명령이 나가도 팔이 힘을 안 쓴다
            enable_torque(follower)
            self._reader, self._leader, self._follower = reader, leader, follower
            self._mode = mode
            self._last_pose = None
            self._blocked = ""
            self._sent, self._stale_since = 0, 0.0
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name=f"relay-{leader}-{follower}")
            self._thread.start()
            logger.info("릴레이 시작: %s → %s (%s 모드)", leader, follower, mode)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            writer, reader = self._writer, self._reader
            leader, follower = self._leader, self._follower
            self._writer = self._reader = None
            self._leader = self._follower = None
        if writer is not _POSE_MODE:
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
        period = 1.0 / (RELAY_HZ if self._mode == "joint" else POSE_HZ)
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

            if self._mode == "joint":
                try:
                    writer.publish(rec["values"])
                    self._sent += 1
                except Exception as exc:
                    logger.warning("릴레이 발행 실패: %s", exc)
            else:
                self._send_pose(rec["values"])

    # ── POSE 모드 ───────────────────────────────────────────────────────────

    def _send_pose(self, values: dict) -> None:
        """리더 관절 → FK → 말단 6D → 팔로워 MoveP.

        ⚠ **막는 것이 전부 여기 있다.** 이 경로는 관절을 팔의 온보드 IK 가
          정하므로 `filter_goal` 이 걸 자리가 없다 — 바닥 필터도 관절 범위도
          변화율 상한도 안 걸린다. 하나라도 여기서 빠뜨리면 아무것도 안 막는다.
        """
        import numpy as np
        from piper_robot import kinematics as K
        from piper_robot.endpose import WorkspaceBox

        from app.services.robot_manager import _call

        try:
            q = K.norm_to_rad(np.array([[values[j] for j in K.ARM_JOINTS]], float))[0]
        except (KeyError, ValueError) as exc:
            self._block(f"리더 관절값을 읽지 못했습니다: {exc}")
            return

        # 1. 짐벌락 — 여기서 나온 rx·rz 를 보내면 팔이 홱 돈다
        if K.near_gimbal_lock(q):
            self._block("리더가 짐벌락 근처입니다 — 손목을 조금 돌리세요")
            return

        # 2. 바닥. **근사다** — 팔로워의 관절은 온보드 IK 가 정하므로 우리가 모른다.
        #    같은 팔이 같은 자세를 만드는 관절값이 리더의 것이니, 그게 바닥을
        #    뚫으면 팔로워도 뚫을 가능성이 높다. 보장은 아니고 유일하게 가능한 검사다.
        floor = _call("get_safety") or {}
        if floor.get("enabled") and floor.get("min_z_cm") is not None:
            low_cm = float(K.lowest_z(q[None, :])[0]) * 100.0
            if low_cm < float(floor["min_z_cm"]):
                self._block(f"리더 자세가 바닥 한계 아래입니다 "
                            f"({low_cm:.1f}cm < {floor['min_z_cm']}cm)")
                return

        target = K.end_pose(q)

        # 3. 작업 공간 상자 — 말단 조그와 **같은 상자**를 쓴다
        ok, why = WorkspaceBox().contains(target["x"] / 1000.0, target["y"] / 1000.0,
                                          target["z"] / 1000.0)
        if not ok:
            self._block(why)
            return

        # 4. 한 걸음 상한. 리더가 튀면(낡은 상태 복구 직후 등) 큰 MoveP 한 방이
        #    나가는데, 그건 사람이 반응할 수 없는 속도로 팔이 도는 것이다.
        if self._last_pose is not None:
            step_mm = max(abs(target[a] - self._last_pose[a]) for a in "xyz") / 1000.0
            step_deg = max(abs(target[a] - self._last_pose[a])
                           for a in ("rx", "ry", "rz")) / 1000.0
            if step_mm > POSE_MAX_STEP_MM or step_deg > POSE_MAX_STEP_DEG:
                self._block(f"리더가 한 번에 너무 많이 움직였습니다 "
                            f"({step_mm:.0f}mm / {step_deg:.0f}°) — 천천히 움직이세요")
                self._last_pose = None      # 다음 프레임부터 다시 기준을 잡는다
                return

        out = _call("stream_end_pose", self._follower, target)
        if not out or not out.get("ok"):
            self._block((out or {}).get("error") or "robotd 가 응답하지 않습니다")
            return
        self._last_pose = target
        self._blocked = ""
        self._sent += 1

    def _block(self, why: str) -> None:
        """보내지 않고 이유를 남긴다. **바뀔 때만** 로그에 쓴다 — 15Hz 로 뱉으면 묻힌다."""
        if why != self._blocked:
            logger.warning("POSE 릴레이 중단 (%s): %s", self._follower, why)
        self._blocked = why

    def status(self) -> dict:
        return {"running": self.is_running, "leader": self._leader,
                "follower": self._follower, "sent": self._sent,
                "stale": bool(self._stale_since),
                "mode": self._mode, "blocked": self._blocked}


relay_session = RelaySession()
