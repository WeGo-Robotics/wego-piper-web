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
| 가운데 | — | **6D 자세만 건너간다** |
| 팔로워 관절을 정하는 것 | 리더가 (그대로 복제) | **우리 IK** (`armmodel`) |
| 팔로워에 주는 것 | 관절 목표 (shm) | 관절 목표 (shm) |
| robotd 안전 필터 | ✅ 탄다 | ✅ **탄다** |
| 양쪽 팔이 달라도 되나 | ❌ | ✅ |

**6D 자세만 건너가는 것이 요점이다.** 관절 구성이 다른 팔(SO-101 등)을 팔로워로
붙이려면 관절값을 직접 대입할 수 없다 — 축 수도 길이도 다르다. 자세는 건너간다.

IK 를 **우리가** 푸는 이유가 하나 더 있다: 팔의 온보드 IK 를 쓰면 관절을 우리가
안 정하므로 `filter_goal` 이 걸 자리가 없었다(바닥·범위·변화율이 전부 빠졌다).
이제는 우리가 관절을 정하므로 예전 경로로 되돌아온다.

⚠ **리더는 자기 말단 자세를 안 알려준다.** 마스터로 설정된 팔은 피드백(0x2Ax)을
내지 않아 `GetArmEndPoseMsgs` 가 0,0,0 을 돌려준다(실측). 그래서 POSE 모드는
FK 로 구한다 — 팔로워에서 대조한 결과 위치 0.08mm·자세 0.002° 로 맞는다.

## 안전 코드가 여기 없는 이유 — `joint` 모드에 한해서다

관절 복제는 조그와 같다: 범위·변화율·데드맨은 CAN 을 쥔 robotd 것이고, 이 루프는
그 앞에 목표를 놓을 뿐이라 **robotd 변경 0줄**이다.

`pose` 모드도 이제 같다 — IK 로 관절을 정해서 같은 세그먼트에 쓰므로 robotd 의
필터를 그대로 탄다. 여기 남은 검사는 **IK 이전** 문제들이다: 리더가 특이점에
있는가, 해가 안 나오는가, 한 번에 너무 많이 움직였는가.
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
# IK 한 번이 실측 2.8ms 라 관절 모드와 같은 30Hz 도 되지만, 여유를 둔다 —
# 리더가 빨리 움직일 때 IK 반복이 늘고, 그때 주기를 못 지키면 목표가 밀린다.
POSE_HZ = 20.0

# 한 주기에 허용할 **말단** 이동. 리더가 튀면(낡은 상태 복구 직후) IK 가 먼
# 목표를 풀고, 그 관절 목표는 robotd 의 변화율 상한에 잘려 팔이 엉뚱하게 기어간다.
# 여기서 먼저 막는 편이 정직하다.
POSE_MAX_STEP_MM = 30.0
POSE_MAX_STEP_DEG = 20.0


#: POSE 모드에는 명령 세그먼트가 없다. `_writer` 자리에 이걸 넣어 "열려 있음"만 표시한다.
_POSE_MODE = object()


def _transport_mismatch(model) -> str:
    """팔로워 모델이 지금 전송 계층에 실릴 수 있나. 못 실으면 이유를 돌려준다."""
    from piper_robot import kinematics as K

    if model.dof != len(K.ARM_JOINTS):
        return (f"{model.name} 은 {model.dof}축인데 팔로워 명령 경로는 "
                f"{len(K.ARM_JOINTS)}축(Piper)입니다. 기구학 모델은 준비돼 있지만 "
                f"그 팔의 전송 계층이 아직 없습니다 — 지금은 관절 수가 같은 "
                f"모델만 쓸 수 있습니다.")
    return ""


def _norm_from_rad(q_rad) -> dict:
    """라디안 관절각 → 정규화 dict. `kinematics.norm_to_rad` 의 역이다.

    변환은 저장소 정본(`piper_robot.joints`)을 쓴다 — 여기서 식을 다시 적으면
    캘리브레이션이 두 벌이 된다.
    """
    import numpy as np

    from piper_robot import kinematics as K
    from piper_robot.joints import JOINT_CALIBRATION, normalize_joint

    out = {}
    for i, name in enumerate(K.ARM_JOINTS):
        raw = float(np.degrees(q_rad[i]) * K.MILLIDEG_PER_DEG)
        out[name] = float(normalize_joint(name, raw))
        _ = JOINT_CALIBRATION
    return out


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
        #: 직전 IK 해. 다음 번 시드가 되어 **같은 가지에 머무르게** 한다.
        self._seed = None
        #: 직전 목표(4x4). 걸음 상한을 재는 기준이다.
        self._last_target = None
        self._ik_iters = 0
        self._leader_model = None
        self._follower_model = None
        #: POSE 모드에서 왜 안 보내고 있나. 화면이 그대로 보여준다.
        self._blocked = ""

    @property
    def is_running(self) -> bool:
        return self._writer is not None

    def start(self, leader: str, follower: str, mode: str = "joint",
              leader_arm: str = "piper", follower_arm: str = "piper") -> None:
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
            # 두 모드 다 **관절 목표**로 끝나므로 같은 세그먼트를 쓴다.
            # (온보드 IK 로 MoveP 를 쏘던 때는 pose 모드가 이걸 안 열었다 —
            #  데드맨이 JointCtrl 로 힘겨루기를 했기 때문이다. 이제는 아니다.)
            try:
                self._writer = open_action_writer(follower, DEADMAN_MS)
            except ArmBusyError as exc:
                reader.close(); teleop_session.stop()
                raise RelayError(str(exc)) from exc
            except Exception as exc:
                reader.close(); teleop_session.stop()
                raise RelayError(f"명령 경로를 열지 못했습니다: {exc}") from exc

            # 버스가 죽어 있으면 여기서 말한다 — 안 그러면 슬라이더는
            # 움직이는데 팔만 안 움직여 소프트웨어를 의심하게 된다
            require_healthy_bus(follower)
            # 토크부터 켠다 — 안 켜면 명령이 나가도 팔이 힘을 안 쓴다
            enable_torque(follower)
            self._reader, self._leader, self._follower = reader, leader, follower
            self._mode = mode
            self._seed = self._last_target = None
            self._ik_iters = 0
            self._blocked = ""
            if mode == "pose":
                from piper_robot.armmodel import ArmModel
                try:
                    self._leader_model = ArmModel.load(leader_arm)
                    self._follower_model = ArmModel.load(follower_arm)
                except (FileNotFoundError, ValueError) as exc:
                    reader.close(); teleop_session.stop()
                    raise RelayError(str(exc)) from exc
                # ⚠ **모델과 하드웨어가 맞는지 시작할 때 본다.** 안 보면 루프
                #   안에서 터지는데, 그때는 이미 세션이 열려 있고 사용자는
                #   "릴레이가 죽었다"만 본다.
                #
                #   ⚠ 쓰기 경로는 아직 **Piper 전용**이다 — 명령 세그먼트가
                #     `joints.JOINT_ORDER`(Piper 6축+그리퍼) 로 되어 있다.
                #     다른 팔을 실제로 붙이려면 그 팔의 전송 계층이 따로 필요하다.
                #     기구학 모델만 준비된 상태다.
                why = _transport_mismatch(self._follower_model)
                if why:
                    reader.close(); teleop_session.stop()
                    raise RelayError(why)
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
        """리더 관절 → FK → 말단 6D → **우리 IK** → 팔로워 관절 → shm.

        가운데 6D 자세만 건너가므로 양쪽 팔이 달라도 된다. 관절 목표로 끝나므로
        robotd 의 `filter_goal`(바닥·범위·변화율·데드맨)을 그대로 탄다.
        """
        import numpy as np
        from piper_robot import kinematics as K

        from app.services.robot_manager import _call

        lm, fm = self._leader_model, self._follower_model
        try:
            q_lead = K.norm_to_rad(
                np.array([[values[j] for j in K.ARM_JOINTS]], float))[0]
        except (KeyError, ValueError) as exc:
            self._block(f"리더 관절값을 읽지 못했습니다: {exc}")
            return

        # 1. 리더가 특이점에 있으면 **6D 자세 자체가 못 미덥다** — joint4·joint6 이
        #    같은 축이라 자세가 관절을 결정하지 못한다. IK 이전 문제라 여기서 본다.
        if lm.near_gimbal_lock(q_lead):
            self._block("리더가 짐벌락 근처입니다 (RPY pitch ≈ ±90°) — 손목을 조금 돌리세요")
            return

        target = lm.fk(q_lead)

        # 2. 한 걸음 상한 — 리더가 튀면 IK 가 먼 목표를 풀고, 그 관절 목표는
        #    robotd 변화율 상한에 잘려 팔이 엉뚱하게 기어간다. 먼저 막는다.
        if self._last_target is not None:
            d = target[:3, 3] - self._last_target[:3, 3]
            step_mm = float(np.linalg.norm(d)) * 1000.0
            rot = target[:3, :3] @ self._last_target[:3, :3].T
            import math
            step_deg = math.degrees(
                math.acos(max(-1.0, min(1.0, (np.trace(rot) - 1.0) / 2.0))))
            if step_mm > POSE_MAX_STEP_MM or step_deg > POSE_MAX_STEP_DEG:
                self._block(f"리더가 한 번에 너무 많이 움직였습니다 "
                            f"({step_mm:.0f}mm / {step_deg:.0f}°) — 천천히 움직이세요")
                self._last_target = None
                self._seed = None
                return

        # 3. IK. **직전 해에서 출발한다** — 그게 같은 가지를 유지해 손목이 홱
        #    뒤집히지 않게 한다(실측: 특이점을 지나도 한 스텝 최대 1.15°).
        #
        #    첫 프레임에는 직전 해가 없다. 그때는 **팔로워가 지금 있는 자세**에서
        #    출발한다 — 거기서 이어가는 것이 자연스럽고, 한계 가운데(`home()`)에서
        #    출발하면 먼 곳에서 시작해 엉뚱한 가지로 수렴한다.
        seed = self._seed
        if seed is None:
            seed = self._follower_seed()
        if seed is None:
            seed = fm.home()
        sol = fm.ik(target, seed)
        if not sol.ok:
            # ⚠ **리더가 그 자세에 서 있다는 것은 도달 가능하다는 증거이지,
            #   팔로워가 갈 수 있다는 뜻이 아니다.** 팔이 다르면 작업공간도 다르고,
            #   같은 팔이어도 관절 한계 경계에서는 해가 없다.
            self._block(f"{fm.name} 로는 그 자세에 못 갑니다 — {sol.reason}")
            return

        # 4. 바닥. 이제는 **근사가 아니다** — 팔로워 관절을 우리가 알기 때문이다.
        #    (온보드 IK 를 쓰던 때는 몰라서 리더 자세로 대신 봤다.)
        floor = _call("get_safety") or {}
        if floor.get("enabled") and floor.get("min_z_cm") is not None:
            low_cm = fm.lowest_z(sol.q) * 100.0
            if low_cm < float(floor["min_z_cm"]):
                self._block(f"팔로워가 바닥 한계 아래로 갑니다 "
                            f"({low_cm:.1f}cm < {floor['min_z_cm']}cm)")
                return

        goal = _norm_from_rad(sol.q)
        # 그리퍼는 IK 를 안 탄다 — 자세와 무관하게 리더 것을 그대로 준다
        if "gripper" in values:
            goal["gripper"] = float(values["gripper"])
        try:
            self._writer.publish(goal)
        except Exception as exc:
            self._block(f"목표 발행 실패: {exc}")
            return
        self._seed = sol.q
        self._last_target = target
        self._blocked = ""
        self._sent += 1
        self._ik_iters = sol.iters

    def _follower_seed(self):
        """팔로워의 지금 관절각 (라디안). 첫 IK 의 출발점이다."""
        import numpy as np
        from piper_shm import arm as shm_arm

        from piper_robot import kinematics as K

        try:
            reader = shm_arm.StateReader(self._follower)
            try:
                rec = reader.read()
            finally:
                reader.close()
            if rec is None:
                return None
            v = rec["values"]
            q = K.norm_to_rad(np.array([[v[j] for j in K.ARM_JOINTS]], float))[0]
            # 모델과 축 수가 다르면 시드로 못 쓴다 — `home()` 으로 떨어진다
            return q if len(q) == self._follower_model.dof else None
        except Exception as exc:
            logger.debug("팔로워 시드 읽기 실패: %s", exc)
            return None

    def _block(self, why: str) -> None:
        """보내지 않고 이유를 남긴다. **바뀔 때만** 로그에 쓴다 — 15Hz 로 뱉으면 묻힌다."""
        if why != self._blocked:
            logger.warning("POSE 릴레이 중단 (%s): %s", self._follower, why)
        self._blocked = why

    def status(self) -> dict:
        return {"running": self.is_running, "leader": self._leader,
                "follower": self._follower, "sent": self._sent,
                "stale": bool(self._stale_since),
                "mode": self._mode, "blocked": self._blocked,
                "ik_iters": self._ik_iters,
                "leader_arm": getattr(self._leader_model, "name", None),
                "follower_arm": getattr(self._follower_model, "name", None)}


relay_session = RelaySession()
