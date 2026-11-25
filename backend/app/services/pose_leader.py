"""PoseLeader — 6자유도 자세 소스(`piper.pose.*`)로 PIPER 팔로워를 조종한다 (feature/pika.md §5).

[웹 리더](web_leader.py)와 같은 자리에 선다: 목표 자세를 **가상 리더 세그먼트**
`piper.arm.pose_<팔로워>.state` 로 내고, 팔은 릴레이(관절 복제, 30Hz)가 움직인다. 웹 리더가
"속도 → 적분 → IK" 라면 여기는 "**절대 자세 → 정합 → IK**" 다. 소스는 무엇이든 — PIKA 를 모른다.

    T_목표 = T_f0 · R · S(T_s0⁻¹ · T_s) · R⁻¹          정합 상대 자세 + 축 정렬 R + 위치 배율 S
           → 따라잡기 (한 틱 STEP_MM·STEP_DEG 까지 — 직선으로)
           → ArmModel.ik(시드 = 직전 해) → 잔차·관절 걸음·바닥 검사 (걸리면 그 자리에 붙잡는다)
           → OneEuro (관절마다, rad — PR #18 원본) → 가상 리더 → 릴레이 → robotd filter_goal

## 몸체 좌표 상대 매핑

`T_s0⁻¹ · T_s` 는 손의 움직임을 **정합 순간 손의 좌표계**로 본 것이다. 그걸 R 로 팔 끝 좌표계에 옮겨 정합
순간 팔 끝에 얹는다. 그래서 정합 때 손잡이를 팔 끝과 **같은 방향으로** 쥐어야 "앞" 이 팔에서도 앞이다 —
화면이 정합 전에 둘의 기울기를 나란히 보여 준다(`align`). 트래커 세계 축 매핑은 캘리브레이션마다 "앞" 을
다시 재야 해서 v1 에서는 안 한다.

소스의 몸체 축은 REP-103 (x 앞·y 왼·z 위)을 따른다고 본다 — PIKA Sense 가 그렇다. 팔 끝 축은 팔 모델이
정한다(PIPER: z 앞·y 왼·x 아래) → `AXES`.

## 무엇을 붙잡고 무엇을 끊나

- 추적 상실(`tracking_ok` 거짓 · 소스가 STALE_S 넘게 낡음 · 세그먼트 없음): **그 자리에 붙잡는다.** 가상 리더는
  계속 발행한다 — 팔은 마지막 목표에 서 있고, 녹화는 멈춘 리더를 읽는다. 이 루프가 죽으면 그때는 릴레이가
  낡은 리더를 보고 전송을 멈춘다(데드맨 두 겹).
- 한 틱에 GLITCH 를 넘는 소스 점프(가림 뒤 다른 자리에서 다시 잡힘·반사): **해제한다.** 따라잡기로 기어가면
  사람이 의도하지 않은 곳으로 팔이 간다 — 다시 정합해야 한다.
- 도달 불가·관절 튐·바닥: 붙잡고 이유를 남긴다. 손이 닿는 곳으로 돌아오면 거기서 이어진다.

막힌 동안은 확장에 `feedback` 을 보내 손잡이가 떨린다 — 사람은 화면이 아니라 팔을 보고 있다.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

from app.services.one_euro import OneEuro

logger = logging.getLogger(__name__)

HZ = 30.0
#: 소스가 이보다 오래되면 추적 상실 — 릴레이 STALE_S 와 같다
SOURCE_STALE_S = 0.5
#: 따라잡기 — 한 틱(1/30초)에 목표가 움직일 수 있는 만큼. 릴레이 POSE 상한과 같다(0.9m/s · 600°/s)
STEP_MM, STEP_DEG = 30.0, 20.0
#: 한 틱에 소스가 이만큼 뛰면 사람 손이 아니다(3m/s · 1350°/s) — 해제
GLITCH_MM, GLITCH_DEG = 100.0, 45.0
#: IK 해가 직전보다 이만큼 튀면 붙잡는다 (릴레이 POSE_MAX_JOINT_STEP_DEG 와 같다)
MAX_JOINT_STEP_DEG = 25.0
#: 조종에서 '해'로 치는 잔차 — 웹 리더와 같다 (한계 근처에서 ok 는 2mm 로도 거짓이다)
ACCEPT_MM, ACCEPT_DEG = 5.0, 3.0
#: 진동 — 막힌 순간 한 번, 계속 막혀 있으면 이 간격으로
BUZZ_EVERY_S = 2.0

#: OneEuro 기본 = PR #18 `--smooth 2 2`. 녹화 161건 오프라인: 거칠기 0.38→0.23, 지연 ~42ms
DEFAULT_MIN_CUTOFF, DEFAULT_BETA, DEFAULT_SCALE = 2.0, 2.0, 1.0
PARAM_RANGES = {"min_cutoff": (0.1, 10.0), "beta": (0.0, 10.0), "scale": (0.25, 2.0)}


def _ry(deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    T = np.eye(4)
    T[:3, :3] = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    return T


#: 소스 몸체 축(x 앞·y 왼·z 위) → 팔 끝 축. PIPER 끝단은 z 앞·y 왼·x 아래라 y 축 −90° —
#: 소스 x → 끝단 z, 소스 z(위) → 끝단 −x. PikaAnyArm 의 `handle_pose_pitch: 1.57` 과 같은 회전이다.
AXES = {"piper": _ry(-90.0)}


def leader_name(follower: str) -> str:
    """가상 리더 세그먼트 이름. 팔로워마다 하나 — 양팔이면 둘이다."""
    return f"pose_{follower}"


# ── 자세 수학 ────────────────────────────────────────────────────────────────

def pose_matrix(pos, quat) -> np.ndarray:
    """위치(m) + 쿼터니언 xyzw → 4x4."""
    x, y, z, w = (float(v) for v in quat)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    T = np.eye(4)
    T[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                 [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                 [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    T[:3, 3] = [float(v) for v in pos]
    return T


def _quat(R: np.ndarray) -> np.ndarray:
    """회전 행렬 → 쿼터니언 wxyz (Shepperd — 180° 근처에서도 안 끊긴다)."""
    t = float(np.trace(R))
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]
    q = np.asarray(q, float)
    return q / np.linalg.norm(q)


def _rot_from_quat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def step_size(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """두 자세 사이 (mm, °)."""
    mm = float(np.linalg.norm(b[:3, 3] - a[:3, 3])) * 1000.0
    rel = a[:3, :3].T @ b[:3, :3]
    deg = math.degrees(math.acos(max(-1.0, min(1.0, (float(np.trace(rel)) - 1.0) / 2.0))))
    return mm, deg


def toward(a: np.ndarray, b: np.ndarray, max_mm: float, max_deg: float) -> tuple[np.ndarray, bool]:
    """a 에서 b 로 — 위치는 max_mm, 자세는 max_deg 까지만. `(자세, 잘렸나)`. 위치는 직선, 자세는 slerp."""
    mm, deg = step_size(a, b)
    if mm <= max_mm and deg <= max_deg:
        return b.copy(), False
    out = np.eye(4)
    f = min(1.0, max_mm / mm) if mm > 0 else 1.0
    out[:3, 3] = a[:3, 3] + (b[:3, 3] - a[:3, 3]) * f
    if deg <= max_deg:
        out[:3, :3] = b[:3, :3]
    else:
        qa, qb = _quat(a[:3, :3]), _quat(b[:3, :3])
        if float(qa @ qb) < 0:
            qb = -qb
        theta = math.acos(max(-1.0, min(1.0, float(qa @ qb))))
        t = max_deg / deg
        q = (math.sin((1 - t) * theta) * qa + math.sin(t * theta) * qb) / math.sin(theta)
        out[:3, :3] = _rot_from_quat(q / np.linalg.norm(q))
    return out, True


def _elevation(v) -> float:
    """z 가 위인 좌표계에서 벡터의 올려본 각(°). 아래를 보면 음수."""
    return round(math.degrees(math.asin(max(-1.0, min(1.0, float(v[2]))))), 1)


# ── 순수 계산 — 시각은 밖에서 준다 (테스트) ─────────────────────────────────

class PoseMapper:
    """소스 자세 → 팔로워 관절 목표(정규화). 상태 기계 하나 — 스레드도 shm 도 모른다."""

    def __init__(self, model, q0_rad, gripper0: float, *, axes: np.ndarray | None = None,
                 floor_cm: float | None = None, grip_max_um: int = 68000,
                 min_cutoff: float = DEFAULT_MIN_CUTOFF, beta: float = DEFAULT_BETA,
                 scale: float = DEFAULT_SCALE) -> None:
        self.model = model
        self.axes = AXES["piper"] if axes is None else axes
        self.floor_cm = floor_cm
        self.grip_max_um = int(grip_max_um)
        self.min_cutoff, self.beta, self.scale = float(min_cutoff), float(beta), float(scale)
        q0 = np.asarray(q0_rad, float)
        self.q = q0.copy()                 # 직전에 받아들인 IK 해 (필터 전) — 다음 시드
        self.T = model.fk(q0)              # 직전에 받아들인 목표 — 따라잡기의 출발점
        self.out_q = q0.copy()             # 필터를 지난 값 — 가상 리더가 내는 것
        self.gripper = float(gripper0)
        self.engaged = False
        self.s0 = self.f0 = None           # 정합 앵커 (소스 · 팔 끝)
        self.prev_want: np.ndarray | None = None   # 직전 틱의 원하는 목표 — 튐 검사
        self.last_src: np.ndarray | None = None
        self.lost = True                   # 자세를 아직 못 받았다
        self.blocked = ""                  # 막힌 이유 (정합 중일 때만 뜻이 있다)
        self.note = ""                     # 막힘은 아닌 알림 (해제된 이유 등)
        self.catching_up = False
        self._filter = OneEuro(self.min_cutoff, self.beta)   # 첫 값은 그대로 지난다 — 출발점에서 점프 0

    # ── 정합 (클러치) ──
    def engage(self, src: np.ndarray) -> None:
        """지금 손 = 지금 팔 끝. 첫 목표가 지금 내는 값이라 점프 0 — 필터도 거기서 새로 시작한다."""
        self._hold_here()
        self.s0 = np.asarray(src, float).copy()
        self.f0 = self.T.copy()
        self.prev_want = self.f0.copy()
        self.engaged = True
        self.blocked = self.note = ""

    def disengage(self, note: str = "") -> None:
        """그 자리에 붙잡는다 — 지금 내는 값에서 멈춘다(필터가 남은 걸 끌고 가지 않게)."""
        self._hold_here()
        self.engaged = False
        self.blocked = ""
        self.note = note

    def _hold_here(self) -> None:
        self.q = self.out_q.copy()
        self.T = self.model.fk(self.q)
        self._filter = OneEuro(self.min_cutoff, self.beta)   # 새로 — 첫 값(= 지금 내는 값)이 그대로 지난다
        self.catching_up = False

    def set_params(self, *, min_cutoff=None, beta=None, scale=None) -> None:
        if min_cutoff is not None:
            self.min_cutoff = self._filter.min_cutoff = float(min_cutoff)
        if beta is not None:
            self.beta = self._filter.beta = float(beta)
        if scale is not None and float(scale) != self.scale:
            self.scale = float(scale)
            if self.engaged and self.last_src is not None:
                # 배율이 바뀌면 같은 손 자리가 다른 목표가 된다 — 지금 자리에서 다시 정합한다
                self.engage(self.last_src)

    # ── 한 틱 ──
    def target(self, src: np.ndarray) -> np.ndarray:
        """정합 상대 자세: T_f0 · R · S(T_s0⁻¹ · T_s) · R⁻¹."""
        d = np.linalg.inv(self.s0) @ src
        d[:3, 3] *= self.scale
        return self.f0 @ self.axes @ d @ np.linalg.inv(self.axes)

    def step(self, rec: dict | None, age_s: float | None, now: float) -> dict[str, float]:
        """한 틱. `rec` = `PoseReader.read()` (없으면 None), `age_s` = 소스 나이. 늘 목표를 돌려준다."""
        fresh = (rec is not None and bool(rec.get("tracking_ok"))
                 and age_s is not None and age_s <= SOURCE_STALE_S)
        if not fresh:
            self.lost = True
            if self.engaged:
                self.blocked = "추적 상실 — 그 자리에 붙잡았다"
        else:
            src = pose_matrix(rec["pos"], rec["quat"])
            self.lost = False
            self.last_src = src
            if self.engaged:
                self._track(src)
            # ⚠ _track 이 방금 해제했을 수 있다(튐) — 그 틱에 그리퍼만 손을 따라가면 안 된다(실측: 77.8 → 29)
            if self.engaged:
                op = rec.get("opening_mm")
                if op is not None and rec.get("encoder_ok"):
                    self.gripper = min(100.0, max(0.0, float(op) * 1000.0 / self.grip_max_um * 100.0))
        self.out_q = self._filter(self.q, now)
        return self.goal()

    def _track(self, src: np.ndarray) -> None:
        want = self.target(src)
        if self.prev_want is not None:
            mm, deg = step_size(self.prev_want, want)
            if mm > GLITCH_MM or deg > GLITCH_DEG:
                # 사람 손이 1/30초에 갈 거리가 아니다 — 가림 뒤 딴 자리에서 다시 잡혔거나 반사다
                self.disengage(f"자세가 한 번에 {mm:.0f}mm / {deg:.0f}° 튀어 해제했다 — 다시 정합하세요")
                return
        self.prev_want = want
        cmd, self.catching_up = toward(self.T, want, STEP_MM, STEP_DEG)
        sol = self.model.ik(cmd, self.q)
        if not sol.ok and (sol.pos_mm > ACCEPT_MM or sol.rot_deg > ACCEPT_DEG):
            self.blocked = f"도달 불가 (위치 {sol.pos_mm:.0f}mm · 자세 {sol.rot_deg:.0f}° 남음)"
            return
        jump = float(np.degrees(np.abs(sol.q - self.q)).max())
        if jump > MAX_JOINT_STEP_DEG:
            self.blocked = f"관절이 한 번에 {jump:.0f}° 튄다 — 특이점 근처, 손을 조금 물리세요"
            return
        if self.floor_cm is not None:
            low = self.model.lowest_z(sol.q) * 100.0
            # 더 내려가는 것만 막는다 — 이미 한계 아래에서 시작했으면(바닥 설정을 나중에 켰다) 올라오는 길은 연다
            if low < self.floor_cm and low < self.model.lowest_z(self.q) * 100.0 - 0.01:
                self.blocked = f"바닥 한계 아래 ({low:.1f}cm < {self.floor_cm:.0f}cm)"
                return
        self.q, self.T = sol.q, cmd
        self.blocked = ""

    # ── 읽기 ──
    def goal(self) -> dict[str, float]:
        from app.services.relay import _norm_from_rad

        g = _norm_from_rad(self.out_q)
        g["gripper"] = self.gripper
        return g

    def align(self) -> dict:
        """정합 전에 맞출 것 — 팔 끝과 손잡이의 기울기(앞 축)·옆 기울기(왼 축). 둘 다 z 가 위인 좌표계라
        중력 기준으로 비교된다(방위는 트래커와 로봇 사이를 모르므로 비교하지 않는다)."""
        T = self.model.fk(self.out_q)
        fwd_tool = self.axes[:3, :3] @ np.array([1.0, 0, 0])    # 소스 x 에 해당하는 끝단 축
        left_tool = self.axes[:3, :3] @ np.array([0, 1.0, 0])
        arm = {"pitch": _elevation(T[:3, :3] @ fwd_tool), "roll": _elevation(T[:3, :3] @ left_tool)}
        hand = None
        if self.last_src is not None:
            R = self.last_src[:3, :3]
            hand = {"pitch": _elevation(R[:, 0]), "roll": _elevation(R[:, 1])}
        return {"arm": arm, "hand": hand}

    def ee(self) -> dict:
        from piper_robot import kinematics as K

        T = self.model.fk(self.out_q)
        r, p, y = K.rpy_from_matrix(T[:3, :3])
        return {"x": round(float(T[0, 3]), 4), "y": round(float(T[1, 3]), 4), "z": round(float(T[2, 3]), 4),
                "roll": round(float(r), 1), "pitch": round(float(p), 1), "yaw": round(float(y), 1)}


# ── 확장에 조명·진동 ─────────────────────────────────────────────────────────

def extension_for(source: str) -> dict | None:
    """그 자세를 내는 확장 — `{name, title, hub, feedback}`. unitd 가 없거나 모르면 None."""
    from app.services import units

    try:
        if not units.unitd_available():
            return None
        from piper_bus import contract as C

        got = units._bus().rpc_call(C.UNITD, "ext_list", [], timeout=3)
    except Exception:
        return None
    for e in (got or {}).get("extensions", []):
        m = e.get("manifest") or {}
        if source in ((m.get("provides") or {}).get("pose") or []):
            return {"name": e.get("name"), "title": e.get("title") or e.get("name"),
                    "hub": m.get("rpc_hub"), "feedback": "feedback" in (m.get("features") or [])}
    return None


class _Feedback:
    """확장의 `feedback(id, state)` — 바뀔 때와 1초마다(확장이 다시 떠도 따라오게). 루프를 안 막게 따로 돈다.

    ⚠ 확장이 죽어 있으면 **안 보낸다** — RPC 큐에 요청이 쌓였다가 다시 뜬 확장이 낡은 상태를 한꺼번에 먹는다."""

    def __init__(self, hub: str, source: str) -> None:
        self.hub, self.source = hub, source
        self._state = {"engaged": False, "blocked": False, "buzz": False}
        self._last_buzz = 0.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._warned = False
        threading.Thread(target=self._run, daemon=True, name=f"pose-feedback-{source}").start()

    def set(self, engaged: bool, blocked: bool) -> None:
        now = time.monotonic()
        with self._lock:
            st = self._state
            buzz = blocked and (not st["blocked"] or now - self._last_buzz >= BUZZ_EVERY_S)
            if buzz:
                self._last_buzz = now
            if engaged != st["engaged"] or blocked != st["blocked"] or buzz:
                self._state = {"engaged": engaged, "blocked": blocked, "buzz": buzz or st["buzz"]}
                self._wake.set()

    def close(self) -> None:
        with self._lock:
            self._state = {"engaged": False, "blocked": False, "buzz": False}
        self._stop.set()
        self._send()                # 손잡이 불을 끈다 — 세션이 끝났다

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if not self._stop.is_set():
                self._send()

    def _send(self) -> None:
        from app.services import units

        with self._lock:
            state = dict(self._state)
            self._state["buzz"] = False
        try:
            bus = units._bus()
            if bus.daemon_info(self.hub) is None:
                return
            bus.rpc_call(self.hub, "feedback", [self.source, state], timeout=2)
            self._warned = False
        except Exception as exc:
            if not self._warned:
                logger.warning("자세 소스 %s 에 feedback 실패: %s", self.source, exc)
                self._warned = True


# ── 세션 — 팔로워 하나 ───────────────────────────────────────────────────────

class PoseLeaderSession:
    def __init__(self, source: str, follower: str, params: dict) -> None:
        self.source, self.follower = source, follower
        self.name = leader_name(follower)
        self.params = dict(params)
        self.mapper: PoseMapper | None = None
        self.started = 0.0
        self.trigger_seq: int | None = None
        self.toggles = 0
        self.ended = ""                    # 스스로 끝났으면 그 이유
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._writer = None
        self._reader = None
        self._feedback: _Feedback | None = None
        self._ext: dict | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._writer is not None

    def start(self) -> None:
        from piper_robot import kinematics as K
        from piper_robot.armmodel import ArmModel
        from piper_shm import arm as shm_arm
        from piper_shm.pose import PoseReader, PoseSegmentError

        from app.services.relay import RelayError, relays
        from app.services.robot_manager import _call

        try:
            self._reader = PoseReader(self.source)
        except PoseSegmentError as exc:
            raise RuntimeError(f"자세 소스 {self.source} 가 없습니다 — 장치 확장이 떠 있고 자세가 오나요? ({exc})")
        # 출발점 = 팔로워의 지금 자세 — 가상 리더의 첫 값이 그것이라 릴레이가 붙는 순간 점프 0
        try:
            fr = shm_arm.StateReader(self.follower)
            try:
                rec = fr.read()
            finally:
                fr.close()
        except Exception as exc:
            self._reader.close()
            raise RuntimeError(f"{self.follower} 의 상태를 읽을 수 없습니다 — 연결돼 있나요? ({exc})")
        if rec is None:
            self._reader.close()
            raise RuntimeError(f"{self.follower} 가 아직 관절값을 발행하지 않습니다")
        v = rec["values"]
        q0 = K.norm_to_rad(np.array([[v[j] for j in K.ARM_JOINTS]], float))[0]
        floor = _call("get_safety") or {}
        floor_cm = float(floor["min_z_cm"]) if floor.get("enabled") and floor.get("min_z_cm") is not None else None
        # 그리퍼는 mm → 그 팔의 mm (정규화끼리 옮기지 않는다 — .117 행정 98↔68mm 사고)
        stroke = _call("get_gripper_stroke", self.follower) or {}
        grip_max = int(stroke.get("raw_max_um") or 68000) if stroke.get("supported") else 68000
        self.mapper = PoseMapper(ArmModel.load("piper"), q0, float(v.get("gripper", 0.0)),
                                 floor_cm=floor_cm, grip_max_um=grip_max, **self.params)
        self._writer = shm_arm.StateWriter(self.name)
        self._writer.publish(self.mapper.goal())     # 릴레이가 시작 전에 한 번 읽어 본다
        try:
            relays.start(self.name, self.follower, "joint", "piper", "piper")
        except RelayError as exc:
            self._close_io()
            raise RuntimeError(str(exc))
        self._ext = extension_for(self.source)
        if self._ext and self._ext.get("feedback") and self._ext.get("hub"):
            self._feedback = _Feedback(self._ext["hub"], self.source)
        self.started = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"pose-leader-{self.follower}")
        self._thread.start()
        logger.info("PoseLeader 시작: %s → %s (가상 리더 %s)", self.source, self.follower, self.name)

    def engage(self) -> None:
        with self._lock:
            m = self.mapper
            if m is None:
                raise RuntimeError("조종 중이 아닙니다")
            if m.lost or m.last_src is None:
                raise RuntimeError("추적이 안 되는 중엔 정합할 수 없습니다 — 손잡이가 기지국에 보이나요?")
            m.engage(m.last_src)
        logger.info("PoseLeader 정합: %s → %s", self.source, self.follower)

    def disengage(self) -> None:
        with self._lock:
            if self.mapper is not None:
                self.mapper.disengage()

    def set_params(self, **kw) -> None:
        with self._lock:
            self.params.update({k: v for k, v in kw.items() if v is not None})
            if self.mapper is not None:
                self.mapper.set_params(**kw)

    def stop(self, why: str = "") -> None:
        from app.services.relay import relays

        with self._lock:
            if self._writer is None:
                return
            self._stop.set()
            self.ended = why
        mine = relays.by_leader(self.name)
        if mine is not None:
            relays.stop(mine.follower)
        self._close_io()
        if self._feedback is not None:
            self._feedback.close()
            self._feedback = None
        logger.info("PoseLeader 끝: %s → %s%s", self.source, self.follower, f" ({why})" if why else "")

    def _close_io(self) -> None:
        writer, self._writer = self._writer, None
        reader, self._reader = self._reader, None
        for obj in (reader, writer):     # 라이터 close 가 세그먼트를 지운다 — 남기면 낡은 리더가 된다
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

    def _read_source(self):
        """최신 자세와 나이. 소스가 다시 떠 세그먼트가 바뀌었으면 다시 연다."""
        from piper_shm.pose import PoseReader, PoseSegmentError

        r = self._reader
        if r is None or r.orphaned:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            try:
                r = self._reader = PoseReader(self.source)
            except PoseSegmentError:
                self._reader = None
                return None, None
        try:
            return r.read(), r.age_s()
        except Exception:
            return None, None

    def _trigger(self, rec: dict | None) -> None:
        """손잡이 두 번 쥐기 = 정합 토글 (텔레옵 중이니까 — feature/pika.md §12 Q2, 2026-11-25).
        횟수가 줄면 소스가 다시 떴다 — 새 기준으로 삼는다."""
        if rec is None:
            return
        seq = int(rec.get("trigger_seq") or 0)
        prev, self.trigger_seq = self.trigger_seq, seq
        if prev is None or seq <= prev:
            return
        if (seq - prev) % 2 == 0:
            return
        m = self.mapper
        self.toggles += 1
        if m.engaged:
            m.disengage()
            logger.info("PoseLeader 두 번 쥐기 → 해제 (%s)", self.follower)
        elif not m.lost and m.last_src is not None:
            m.engage(m.last_src)
            logger.info("PoseLeader 두 번 쥐기 → 정합 (%s)", self.follower)

    def _loop(self) -> None:
        from app.services.relay import relays

        period = 1.0 / HZ
        last_relay_check = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                with self._lock:
                    m, writer = self.mapper, self._writer
                    if m is None or writer is None:
                        return
                    rec, age = self._read_source()
                    if rec is not None and age is not None and age <= SOURCE_STALE_S:
                        self._trigger(rec)
                    goal = m.step(rec, age, t0)
                    writer.publish(goal)
                    engaged, blocked = m.engaged, bool(m.engaged and m.blocked)
                if self._feedback is not None:
                    self._feedback.set(engaged, blocked)
                # 릴레이가 사라졌다 = E-stop 이나 다른 화면이 팔로워를 놓았다. 발행만 남으면 아무도 안 읽는
                # 리더가 되고, 화면은 조종 중이라고 말한다 — 같이 끝낸다
                if t0 - last_relay_check > 0.5:
                    last_relay_check = t0
                    if relays.by_leader(self.name) is None:
                        threading.Thread(target=self.stop, args=("릴레이가 끝났다 (E-stop · 다른 조작)",),
                                         daemon=True).start()
                        return
            except Exception as exc:
                logger.warning("PoseLeader 틱 실패 (%s): %s", self.follower, exc)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def status(self) -> dict:
        from app.services.relay import relays

        m = self.mapper
        out = {"source": self.source, "follower": self.follower, "leader": self.name,
               "running": self.running, "started": self.started, "params": dict(self.params),
               "toggles": self.toggles, "ended": self.ended,
               "extension": self._ext, "feedback": self._feedback is not None}
        if m is None:
            return out
        rs = relays.status(self.follower)
        mine = rs.get("leader") == self.name
        with self._lock:
            out.update({
                "engaged": m.engaged, "lost": m.lost, "blocked": m.blocked if m.engaged else "",
                "note": m.note, "catching_up": m.catching_up,
                "goal": {k: round(v, 1) for k, v in m.goal().items()},
                "ee": m.ee(), "align": m.align(),
                "relay": {"holding": bool(mine and rs.get("holding")), "sent": rs.get("sent") if mine else 0,
                          "stale": bool(mine and rs.get("stale")), "blocked": rs.get("blocked") if mine else ""},
            })
        return out


class PoseLeaderPool:
    """팔로워마다 하나. 한 소스로 두 팔을 끌지 않는다 — 손 하나가 두 팔이 되면 사람이 못 따라간다."""

    def __init__(self) -> None:
        self._sessions: dict[str, PoseLeaderSession] = {}
        self._lock = threading.Lock()
        self.params = {"min_cutoff": DEFAULT_MIN_CUTOFF, "beta": DEFAULT_BETA, "scale": DEFAULT_SCALE}

    def sessions(self) -> list[PoseLeaderSession]:
        return [s for s in list(self._sessions.values()) if s.running]

    @property
    def is_running(self) -> bool:
        return bool(self.sessions())

    def get(self, follower: str) -> PoseLeaderSession:
        s = self._sessions.get(follower)
        if s is None or not s.running:
            raise RuntimeError(f"{follower} 를 자세로 조종하고 있지 않습니다")
        return s

    def start(self, source: str, follower: str) -> dict:
        from piper_shm.pose import PoseSegmentError, _check_id

        try:
            _check_id(source)
        except PoseSegmentError as exc:
            raise ValueError(str(exc))
        with self._lock:
            live = {s.follower: s for s in self.sessions()}
            if follower in live:
                raise RuntimeError(f"이미 {live[follower].source} 로 {follower} 를 조종 중입니다")
            other = next((s for s in live.values() if s.source == source), None)
            if other is not None:
                raise RuntimeError(f"{source} 는 이미 {other.follower} 를 끌고 있습니다 — 손 하나로 두 팔을 끌지 않습니다")
            s = PoseLeaderSession(source, follower, self.params)
            s.start()
            self._sessions[follower] = s
        return s.status()

    def stop(self, follower: str | None = None) -> list[str]:
        """팔로워를 안 주면 전부 — E-stop 경로."""
        with self._lock:
            targets = list(self._sessions.items()) if follower is None else (
                [(follower, self._sessions[follower])] if follower in self._sessions else [])
            for f, _ in targets:
                self._sessions.pop(f, None)
        for _f, s in targets:
            try:
                s.stop()
            except Exception as exc:
                logger.error("PoseLeader 정지 실패 (%s): %s", _f, exc)
        return [f for f, _ in targets]

    async def kill(self) -> None:
        self.stop()

    def set_params(self, **kw) -> dict:
        for k, v in kw.items():
            if v is None:
                continue
            lo, hi = PARAM_RANGES[k]
            if not lo <= float(v) <= hi:
                raise ValueError(f"{k} 는 {lo}~{hi} 사이여야 합니다: {v}")
        self.params.update({k: float(v) for k, v in kw.items() if v is not None})
        for s in self.sessions():
            s.set_params(**kw)
        return dict(self.params)

    def status(self) -> dict:
        ended = [{"follower": s.follower, "source": s.source, "ended": s.ended}
                 for s in list(self._sessions.values()) if not s.running and s.ended]
        return {"sessions": [s.status() for s in self.sessions()], "params": dict(self.params),
                "ranges": PARAM_RANGES, "ended": ended}


pose_leaders = PoseLeaderPool()


def list_sources() -> list[dict]:
    """지금 있는 자세 소스 — 화면의 고르기 재료."""
    from piper_shm.pose import PoseReader, list_segments

    using = {s.source: s.follower for s in pose_leaders.sessions()}
    out = []
    for sid in list_segments():
        row = {"id": sid, "age_s": None, "tracking_ok": None, "opening_mm": None,
               "trigger_seq": None, "used_by": using.get(sid)}
        try:
            r = PoseReader(sid)
            try:
                rec = r.read()
                age = r.age_s()
            finally:
                r.close()
        except Exception:
            out.append(row)
            continue
        if rec is not None:
            row.update(age_s=None if math.isinf(age) else round(age, 3), tracking_ok=rec["tracking_ok"],
                       opening_mm=None if rec["opening_mm"] is None else round(rec["opening_mm"], 1),
                       trigger_seq=rec["trigger_seq"])
        out.append(row)
    return out
