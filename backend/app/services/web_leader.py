"""웹 리더 — 키보드·마우스가 가리키는 자세를 **가상 리더 세그먼트**로 발행한다
(feature/web-leader.md). 팔로워는 릴레이가 움직인다 — 새 조종 경로가 아니다.

## 브라우저가 아니라 여기서 적분한다

브라우저는 "지금 눌린 것"과 마우스 델타를 30Hz 로 보낸다. 자세는 여기서 속도를
적분해 만든다. 입력이 INPUT_DEADMAN_S 동안 안 오면 속도가 0 이 된다 — 탭이 멈추거나
이벤트가 씹혀도 팔은 선다. 그 뒤로 릴레이 데드맨(0.5초)·robotd 데드맨이 두 겹 더 있다.

## EE 모드는 릴레이 POSE 와 같은 IK

목표 6D 를 적분하고 `ArmModel.ik`(시드 = 직전 해)로 관절 목표를 만든다. 못 풀거나
관절이 튀거나 바닥 아래면 **목표를 되돌리고** 이유를 남긴다. 리더가 Piper 관절 그
자체라 SO-101 때의 크로스 모델 문제가 없다.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

LEADER_NAME = "web_leader1"
PARKING = {"joint1": 0.0, "joint2": -100.0, "joint3": 100.0, "joint4": 0.0,
           "joint5": 0.0, "joint6": 0.0, "gripper": 0.0}
HZ = 30.0
INPUT_DEADMAN_S = 0.1          # 입력이 이만큼 안 오면 속도 0
JOINT_SPEED = 30.0             # norm/s
JOINT_FAST, JOINT_FINE = 3.0, 0.25
GRIPPER_SPEED = 60.0           # norm/s (0..100)
MOVE_SPEED = 0.08              # m/s
ROT_SPEED = math.radians(30)   # rad/s
MOUSE_JOINT_PER_PX = 20.0 / 300     # norm / px
MOUSE_MOVE_PER_PX = 0.06 / 300      # m / px
MOUSE_ROT_PER_PX = math.radians(30) / 300
WHEEL_JOINT = 2.0              # norm / 눈금
WHEEL_Z = 0.005                # m / 눈금
WHEEL_ROLL = math.radians(3)   # rad / 눈금
MAX_JOINT_STEP_DEG = 25.0      # IK 해가 이보다 튀면 거절 (릴레이 POSE 와 같다)
EE_RESYNC_DEG = 12.0           # 통합기 믿음이 실제 팔과 이만큼 벌어지면 실제로 재동기
JOINT_RESYNC_NORM = 15.0       # 관절 모드: 명령 자세가 실제와 이만큼 벌어지면 실제로 되맞춤(장애물·리셋)
HOME_SPEED_NORM = 60.0         # 로봇 위치 초기화: 원점 복귀 속도(norm/s) — 순간이동 대신 램프(복귀도 학습)
EE_ACCEPT_MM, EE_ACCEPT_DEG = 5.0, 3.0   # 조종에서 '해'로 치는 잔차

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")
#: 관절 모드 키 — 위 줄 +, 아래 줄 − (손가락 하나가 관절 하나)
JOINT_KEYS = {"q": ("joint1", 1), "a": ("joint1", -1), "w": ("joint2", 1), "s": ("joint2", -1),
              "e": ("joint3", 1), "d": ("joint3", -1), "r": ("joint4", 1), "f": ("joint4", -1),
              "y": ("joint6", 1), "h": ("joint6", -1)}    # joint5 는 T 를 팔 리셋에 내주고 가운데 드래그·휠선택으로
#: EE 모드 키 — **회전만** (이동은 마우스가 한다 → 마우스 드래그하며 키를 눌러 6D 동시).
#  위 줄 +, 아래 줄 − (관절 모드와 같은 기억법): q/a=roll, w/s=pitch, e/d=yaw.
EE_KEYS = {"q": ("roll", 1), "a": ("roll", -1), "w": ("pitch", 1), "s": ("pitch", -1),
           "e": ("yaw", 1), "d": ("yaw", -1)}
GRIPPER_KEYS = {"[": -1, "]": 1}


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _rot(axis: str, a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


class Integrator:
    """순수 상태 기계 — 입력을 속도로, 속도를 자세로. 시간은 밖에서 준다(테스트)."""

    def __init__(self, pose: dict[str, float], mode: str = "joint", speed_scale: float = 1.0) -> None:
        self.pose = dict(pose)                    # 정규화: joint1..6 ±100, gripper 0..100
        self.mode = mode
        self.speed_scale = speed_scale            # 실기는 0.5 — 처음 두 번은 느리게
        self.selected = "joint1"
        self.pose_lock = False
        self.last_input = 0.0
        self.blocked = ""
        # 마지막 입력 — 키 집합과 마우스 델타(한 틱 분)
        self.keys: set[str] = set()
        self.shift = self.ctrl = False
        self.mouse = {"dx": 0.0, "dy": 0.0, "wheel": 0.0, "buttons": []}
        self.gripper_snap: float | None = None
        self.homing: dict[str, float] | None = None   # 원점 복귀 램프 목표 (진행 중이면 자세)
        # EE
        self.model = None
        self.q_rad: np.ndarray | None = None
        self._follower_q: np.ndarray | None = None   # 매 틱 읽는 실제 팔 관절 (IK 시드·접지)
        self.T: np.ndarray | None = None
        self.floor_cm: float | None = None

    # ── 입력 ──
    def feed(self, now: float, keys=None, mouse: dict | None = None, shift=False, ctrl=False,
             click: str | None = None, toggle_lock=False, select: str | None = None) -> None:
        self.keys = {str(k).lower() for k in (keys or [])}
        self.shift, self.ctrl = bool(shift), bool(ctrl)
        m = mouse or {}
        self.mouse = {"dx": float(m.get("dx", 0)), "dy": float(m.get("dy", 0)),
                      "wheel": float(m.get("wheel", 0)), "buttons": list(m.get("buttons", []))}
        self.last_input = now
        # 사용자가 움직이면 복귀 램프를 즉시 취소한다 — 인계
        if self.homing is not None and (self.keys or self.mouse["dx"] or self.mouse["dy"]
                                        or self.mouse["wheel"] or self.mouse["buttons"] or click):
            self.homing = None
        if select in JOINTS or select == "gripper":
            self.selected = select
        if toggle_lock:
            self.pose_lock = not self.pose_lock
        if click in ("close", "open"):
            self.gripper_snap = 0.0 if click == "close" else 100.0

    def set_mode(self, mode: str) -> None:
        if mode not in ("joint", "ee") or mode == self.mode:
            return
        if mode == "ee":
            self._ensure_ee()
        self.mode = mode
        self.blocked = ""

    def _ensure_ee(self) -> None:
        from piper_robot import kinematics as K
        from piper_robot.armmodel import ArmModel
        if self.model is None:
            self.model = ArmModel.load("piper")
        self.q_rad = K.norm_to_rad(np.array([[self.pose[j] for j in JOINTS]], float))[0]
        self.T = self.model.fk(self.q_rad)

    # ── 적분 ──
    def start_homing(self, target: dict[str, float]) -> None:
        """원점(파킹) 복귀를 **램프로** 시작한다 — 순간이동이 아니라 자세를 목표까지 서서히
        옮긴다. 리더 자세가 램프되면 팔로워가 따라오고, 수집 중이면 복귀가 액션으로 기록된다
        (사용자 요청 2026: 돌아가는 것도 학습, 손으로 되돌리기는 어렵다)."""
        self.homing = dict(target)
        self.blocked = ""

    def step(self, now: float, dt: float) -> dict[str, float]:
        if self.homing is not None:
            reached = True
            rate = HOME_SPEED_NORM * dt
            for k, tgt in self.homing.items():
                cur = self.pose.get(k, 0.0)
                d = tgt - cur
                if abs(d) <= rate:
                    self.pose[k] = tgt
                else:
                    self.pose[k] = cur + rate * (1.0 if d > 0 else -1.0)
                    reached = False
            if reached:
                self.homing = None
                if self.mode == "ee":
                    self._ensure_ee()          # 복귀 끝났으니 EE 목표를 현재 자세로 재정합
            return dict(self.pose)
        alive = (now - self.last_input) <= INPUT_DEADMAN_S
        keys = self.keys if alive else set()
        mouse = self.mouse if alive else {"dx": 0.0, "dy": 0.0, "wheel": 0.0, "buttons": []}
        fine = (JOINT_FINE if self.ctrl else 1.0) * self.speed_scale
        # 그리퍼 — 두 모드 공통 (키·버튼·휠+Ctrl·스냅)
        g = 0.0
        for k, sgn in GRIPPER_KEYS.items():
            if k in keys:
                g += sgn
        if self.mode == "ee" or True:
            if 0 in mouse["buttons"]:
                g -= 1
            if 2 in mouse["buttons"]:
                g += 1
        if g:
            self.pose["gripper"] = _clamp(self.pose["gripper"] + g * GRIPPER_SPEED * fine * dt, 0.0, 100.0)
        if self.gripper_snap is not None:
            self.pose["gripper"] = self.gripper_snap
            self.gripper_snap = None
        if self.mode == "joint":
            self._step_joint(keys, mouse, dt, fine)
        else:
            self._step_ee(keys, mouse, dt, fine)
        # 마우스 델타는 한 틱 분이다 — 먹었으면 비운다
        self.mouse = {**self.mouse, "dx": 0.0, "dy": 0.0, "wheel": 0.0}
        return dict(self.pose)

    def _step_joint(self, keys, mouse, dt, fine) -> None:
        speed = JOINT_SPEED * (JOINT_FAST if self.shift else 1.0) * fine
        vel = {j: 0.0 for j in JOINTS}
        for k, (j, sgn) in JOINT_KEYS.items():
            if k in keys:
                vel[j] += sgn * speed
        for j in JOINTS:
            if vel[j]:
                self.pose[j] = _clamp(self.pose[j] + vel[j] * dt, -100.0, 100.0)
        # 휠: 선택 관절 한 눈금 (Ctrl+휠은 그리퍼)
        if mouse["wheel"]:
            if self.ctrl:
                self.pose["gripper"] = _clamp(self.pose["gripper"] - mouse["wheel"] * WHEEL_JOINT, 0.0, 100.0)
            elif self.selected == "gripper":
                self.pose["gripper"] = _clamp(self.pose["gripper"] - mouse["wheel"] * WHEEL_JOINT, 0.0, 100.0)
            else:
                self.pose[self.selected] = _clamp(self.pose[self.selected] - mouse["wheel"] * WHEEL_JOINT * fine, -100.0, 100.0)
        # 드래그: 우버튼 → j1/j2 (Shift: j4/j3), 가운데 → j6/j5
        dx, dy = mouse["dx"] * MOUSE_JOINT_PER_PX * fine, mouse["dy"] * MOUSE_JOINT_PER_PX * fine
        if 2 in mouse["buttons"] and (dx or dy):
            jx, jy = ("joint4", "joint3") if self.shift else ("joint1", "joint2")
            self.pose[jx] = _clamp(self.pose[jx] + dx, -100.0, 100.0)
            self.pose[jy] = _clamp(self.pose[jy] - dy, -100.0, 100.0)
        elif 1 in mouse["buttons"] and (dx or dy):
            self.pose["joint6"] = _clamp(self.pose["joint6"] + dx, -100.0, 100.0)
            self.pose["joint5"] = _clamp(self.pose["joint5"] - dy, -100.0, 100.0)

    def _step_ee(self, keys, mouse, dt, fine) -> None:
        if self.T is None:
            self._ensure_ee()
        move = np.zeros(3)
        rot = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
        for k, (ax, sgn) in EE_KEYS.items():
            if k in keys:
                if ax in ("x", "y", "z"):
                    move["xyz".index(ax)] += sgn * MOVE_SPEED * fine * dt
                else:
                    rot[ax] += sgn * ROT_SPEED * fine * dt
        # 마우스는 **항상 이동** — 세로 위쪽(dy<0)=앞(+x), 가로=좌우(±y), 휠=위아래(z).
        # 회전은 키(EE_KEYS)에서만 오므로 마우스를 끌며 키를 눌러 6D 를 동시에 움직인다.
        move[1] += -mouse["dx"] * MOUSE_MOVE_PER_PX * fine
        move[0] += -mouse["dy"] * MOUSE_MOVE_PER_PX * fine
        move[2] += -mouse["wheel"] * (WHEEL_Z * 0.2 if self.ctrl else WHEEL_Z)
        if self.pose_lock:
            rot = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
        if not move.any() and not any(rot.values()):
            return
        T = self.T.copy()
        T[:3, 3] += move
        R = T[:3, :3]
        if rot["yaw"]:
            R = _rot("z", rot["yaw"]) @ R
        if rot["pitch"]:
            R = _rot("y", rot["pitch"]) @ R
        if rot["roll"]:
            R = _rot("x", rot["roll"]) @ R
        T[:3, :3] = R
        # ⚠ IK 시드는 **실제 팔 관절**이다 — 시뮬이 장애물에 막혀 안 움직이면, 옛 해에서
        #   시드하면 목표가 실제보다 앞서 달아나 매 스텝 되돌린다(막힘). 실제에서 시드하면
        #   막힌 만큼 step 이 커져 자연히 상한에 걸려 목표가 실제에 멈춘다 — 풀리면 이어진다.
        seed = self._follower_q if self._follower_q is not None else self.q_rad
        sol = self.model.ik(T, seed)
        if not sol.ok and (sol.pos_mm > EE_ACCEPT_MM or sol.rot_deg > EE_ACCEPT_DEG):
            self.blocked = f"도달 불가 (위치 오차 {sol.pos_mm:.0f}mm, 자세 {sol.rot_deg:.0f}°)"
            return
        step_deg = float(np.degrees(np.abs(sol.q - seed)).max())
        if step_deg > MAX_JOINT_STEP_DEG:
            self.blocked = f"관절이 {step_deg:.0f}° — 막혀 있거나 특이점 근처, 목표를 실제에 붙잡음"
            return
        if self.floor_cm is not None:
            low_cm = float(self.model.lowest_z(sol.q)) * 100.0
            if low_cm < self.floor_cm:
                self.blocked = f"바닥 한계 아래 ({low_cm:.1f}cm < {self.floor_cm:.0f}cm)"
                return
        from app.services.relay import _norm_from_rad
        self.T, self.q_rad = T, sol.q
        self.pose.update(_norm_from_rad(sol.q))
        self.blocked = ""

    def observe(self, follower_norm) -> None:
        """매 틱 **실제 시뮬 관절값**을 받아 통합기를 접지한다 (사용자 지적 2026: 시뮬은
        장애물에 막히면 안 움직이니 통합기가 실제를 들고 있어야 한다).

        - EE: IK 시드로 쓸 실제 관절을 저장하고, 믿음(q_rad·T)이 크게 벌어지면 재동기.
        - 관절 모드: 명령 자세(self.pose)가 실제와 크게 벌어지면(막힘·리셋) 실제로 되맞춰
          장애물에 대고 계속 밀지 않는다. 풀리면 거기서 이어진다."""
        import numpy as np

        if not follower_norm or self.homing is not None:
            return
        if self.mode == "joint":
            if max(abs(self.pose.get(j, 0.0) - float(follower_norm.get(j, self.pose.get(j, 0.0))))
                   for j in JOINTS) > JOINT_RESYNC_NORM:
                for j in JOINTS:
                    if j in follower_norm:
                        self.pose[j] = float(follower_norm[j])   # 그리퍼는 사용자 것 유지
            return
        try:
            from piper_robot import kinematics as K
            q = K.norm_to_rad(np.array([[float(follower_norm[j]) for j in JOINTS]], float))[0]
        except Exception:
            return
        self._follower_q = q
        self.ee_resync(q)

    def ee_resync(self, follower_q_rad) -> None:
        """EE 통합기의 목표·시드를 **실제 팔로워 상태**로 되맞춘다.

        ⚠ EE 는 목표 T 와 시드 q 를 내부에서 개루프로 누적한다. 팔이 밖에서 움직이면
        (T 키 리셋·데드맨·물리 튐·수집 인계) 통합기의 믿음이 실제와 어긋나, IK 가 옛
        시드에서 옛 T 로 풀려 매 스텝 "도달 불가/관절 튐"으로 되돌려 **영영 막힌다**
        (사용자 보고 2026: 집다 먹통→리셋→어디로도 못 감). 실제와 크게 벌어지면 실제로
        스냅한다 — 그때부터 사용자가 있는 자리에서 다시 움직인다."""
        import numpy as np

        if self.mode != "ee" or follower_q_rad is None or self.model is None:
            return
        if self.q_rad is None:
            self._ensure_ee()
        if float(np.degrees(np.abs(np.asarray(follower_q_rad) - self.q_rad)).max()) > EE_RESYNC_DEG:
            self.q_rad = np.asarray(follower_q_rad, dtype=float).copy()
            self.T = self.model.fk(self.q_rad)
            self.blocked = ""

    def ee_readout(self) -> dict | None:
        if self.T is None:
            return None
        from piper_robot import kinematics as K
        r, p, y = K.rpy_from_matrix(self.T[:3, :3])          # 이미 도(°) 다 — 실측 4873° 는 두 번 바꾼 것
        return {"x": round(float(self.T[0, 3]), 4), "y": round(float(self.T[1, 3]), 4),
                "z": round(float(self.T[2, 3]), 4), "roll": round(float(r), 1),
                "pitch": round(float(p), 1), "yaw": round(float(y), 1)}


class WebLeader:
    """세그먼트 발행 + 릴레이 시작/정지. 한 번에 하나."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writer = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.integ: Integrator | None = None
        self.follower: str | None = None
        self._reader = None            # 팔로워 상태(EE 재동기용)
        self.started = 0.0

    @property
    def is_running(self) -> bool:
        return self._writer is not None

    def start(self, follower: str, mode: str = "joint", relay: bool = True) -> dict:
        """`relay=False` 는 **발행만** 한다 — 수집이 녹화 프로세스(piper_leader_shm →
        piper_follower_shm)로 팔을 움직일 때다. 릴레이와 녹화가 같은 팔로워 명령 세그먼트를
        쥘 수 없다(텔레옵은 수집과 배타)."""
        from piper_shm import arm as shm_arm
        from app.services.relay import RelayError, relay_session
        from app.services.robot_manager import _call

        with self._lock:
            if self.is_running:
                raise RuntimeError(f"이미 {self.follower} 를 조종 중입니다 — 먼저 끝내세요")
            # 앵커 = 팔로워의 지금 자세 (SO-101 정합과 같다 — 점프 0)
            try:
                reader = shm_arm.StateReader(follower)
            except Exception as exc:
                raise RuntimeError(f"{follower} 의 상태를 읽을 수 없습니다 — 연결돼 있나요? ({exc})")
            rec = reader.read()
            if rec is None:
                reader.close()
                raise RuntimeError(f"{follower} 가 아직 관절값을 발행하지 않습니다")
            integ = Integrator(dict(rec["values"]), "joint",
                               speed_scale=1.0 if follower.startswith("sim_") else 0.5)
            floor = _call("get_safety") or {}
            if floor.get("enabled") and floor.get("min_z_cm") is not None:
                integ.floor_cm = float(floor["min_z_cm"])
            writer = shm_arm.StateWriter(LEADER_NAME)
            writer.publish(integ.pose)              # 릴레이가 시작 전에 한 번 읽어 본다
            self._writer, self.integ, self.follower = writer, integ, follower
            self._reader = reader          # EE 재동기: 실제 팔로워 상태를 매 틱 읽는다
            self.started = time.time()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="web-leader")
            self._thread.start()
        if relay:
            try:
                relay_session.start(LEADER_NAME, follower, "joint", "piper", "piper")
            except RelayError as exc:
                self.stop()
                raise RuntimeError(str(exc))
        if mode == "ee":
            integ.set_mode("ee")
        logger.info("웹 리더 시작 → %s", follower)
        return self.status()

    def input(self, **kw) -> None:
        integ = self.integ
        if integ is None or not self.is_running:
            raise RuntimeError("조종 중이 아닙니다 — 창을 클릭해 시작하세요")
        with self._lock:
            mode = kw.pop("mode", None)
            integ.feed(time.monotonic(), **kw)
            if mode:
                integ.set_mode(mode)

    def ramp_home(self) -> bool:
        """리더 자세를 파킹으로 **램프**한다(순간이동 아님). 팔로워가 따라오고, 수집 중이면
        복귀가 액션으로 기록된다. 사용자가 조작하면 램프는 취소된다(feed)."""
        with self._lock:
            if self.integ is None:
                return False
            self.integ.start_homing(dict(PARKING))
        return True

    def release_follower(self) -> bool:
        """릴레이만 끝낸다 — 세그먼트는 계속 발행한다. 수집이 팔을 넘겨받을 때."""
        from app.services.relay import relay_session
        if relay_session.is_running and relay_session.status().get("leader") == LEADER_NAME:
            relay_session.stop()
            logger.info("웹 리더: 팔로워를 놓음 (발행은 계속) — 수집이 움직인다")
            return True
        return False

    def relaying(self) -> bool:
        from app.services.relay import relay_session
        return bool(relay_session.is_running and relay_session.status().get("leader") == LEADER_NAME
                    and relay_session.holding)

    def stop(self) -> None:
        from app.services.relay import relay_session
        with self._lock:
            writer, self._writer = self._writer, None
            reader, self._reader = self._reader, None
            self._stop.set()
            follower, self.follower = self.follower, None
            self.integ = None                   # 끝난 뒤의 입력은 거절이다 — 조용히 먹으면 안 된다
        if reader is not None:
            try: reader.close()
            except Exception: pass
        if relay_session.is_running and relay_session.status().get("leader") == LEADER_NAME:
            relay_session.stop()
        if writer is not None:
            try:
                writer.close()                  # 세그먼트를 지운다 — 남기면 낡은 리더가 된다
            except Exception as exc:
                logger.warning("웹 리더 세그먼트 닫기 실패: %s", exc)
        if follower:
            logger.info("웹 리더 끝: %s", follower)

    def status(self) -> dict:
        from app.services.relay import relay_session
        integ = self.integ
        if not self.is_running or integ is None:
            return {"running": False}
        rs = relay_session.status()
        mine = rs.get("leader") == LEADER_NAME
        return {"running": True, "follower": self.follower, "mode": integ.mode,
                "publishing": True, "relaying": bool(mine and rs.get("holding")),
                "speed_scale": integ.speed_scale,
                "pose": {k: round(v, 1) for k, v in integ.pose.items()},
                "ee": integ.ee_readout(), "selected": integ.selected, "pose_lock": integ.pose_lock,
                "homing": integ.homing is not None,
                "blocked": integ.blocked or rs.get("blocked") or "",
                "input_age": round(time.monotonic() - integ.last_input, 2) if integ.last_input else None,
                "relay": {"running": bool(mine and rs.get("running")), "holding": bool(mine and rs.get("holding")),
                          "sent": rs.get("sent") if mine else 0, "stale": bool(mine and rs.get("stale"))},
                "started": self.started}

    def _loop(self) -> None:
        period = 1.0 / HZ
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt, last = now - last, now
            try:
                with self._lock:
                    integ, writer, reader = self.integ, self._writer, self._reader
                    if integ is None or writer is None:
                        return
                    if reader is not None:
                        integ.observe(_follower_norm(reader))   # 실제 시뮬 관절값으로 접지(양 모드)
                    pose = integ.step(now, min(dt, 0.1))
                    writer.publish(pose)
            except Exception as exc:
                logger.warning("웹 리더 발행 실패: %s", exc)
            time.sleep(max(0.0, period - (time.monotonic() - now)))


def _follower_norm(reader):
    """팔로워 상태 세그먼트 → 정규화 관절 dict (통합기 접지). 못 읽으면 None."""
    try:
        rec = reader.read()
        return dict(rec["values"]) if rec else None
    except Exception:
        return None


web_leader = WebLeader()


def reset_sim_world(follower: str, arm_only: bool = False) -> dict:
    """환경 리셋 — 시뮬 월드와, 조종 중이면 리더 자세까지 파킹으로. arm_only 면 팔만(큐브
    유지, T 키). 실기 팔은 순간이동이 없다 — 시뮬만."""
    from app.services import sim_robot_client as sim

    if not follower.startswith("sim_"):
        raise RuntimeError("실기 팔은 리셋할 수 없습니다 — 시뮬에서만 됩니다")
    # ⚠ 팔은 **순간이동하지 않는다** — 리더 자세를 파킹으로 램프해 팔로워가 따라오게 한다
    #   (복귀도 학습, 사용자 요청 2026). 큐브만(환경 리셋) 시작 위치로 텔레포트한다.
    if not arm_only:
        sim.call("reset_cube", default=None, timeout=15)
    if web_leader.is_running and web_leader.follower == follower:
        web_leader.ramp_home()                       # 리더 램프 → 팔로워 따라옴(기록됨)
    else:
        sim.call("go_to", follower, dict(PARKING), default=None, timeout=15)   # 리더 없으면 데몬이 램프
    return {"arm_only": arm_only, "homing": True}
