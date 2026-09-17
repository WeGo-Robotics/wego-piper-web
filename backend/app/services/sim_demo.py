"""스크립트 시연 — 사람 없이 에피소드를 만든다 (feature/sim-env.md 4단계).

## 무엇을 하나

큐브를 집어 통에 넣는 한 바퀴를 **리더 세그먼트로 발행한다**. 웹 리더와 정확히 같은 자리에
선다 — 릴레이가 켜져 있으면 팔로워가 따라오고, 수집 중이면(`relay=False`) 녹화 프로세스가
그 리더를 읽어 팔을 민다. 그래서 수집·추론·E-stop 경로가 **하나도 안 바뀐다.**

## 자세는 지어내지 않고 **찾아서 쓴다**

위에서 잡는 자세의 회전을 손으로 적으면 IK 가 못 푼다 — 실제로 첫 시도(툴 x = 세계 +x)는
파지·통 위 전부 실패했다(오차 87mm/75°). **캘리브레이션 한계 안**에서 무작위 표본을 떠
툴 z 가 아래를 보는 실제 해를 찾았고(30만 표본에 9개 — 좁다), 그 해의 회전을 그대로 쓴다.
그 자세를 시드로 쓰면 궤적 전체가 0.4mm 안쪽으로 풀리고 정규화 값이 ±100 을 안 넘는다.
큐브를 테이블 위 아무 데나 놓아도 20/20 이다.

## 높이는 실측에서 온다

`ArmModel.fk` 는 **link6** 프레임이다(공식 URDF 에 그리퍼가 없다 — 그리퍼는 씬이 붙인다).
손가락 끝은 툴 +z 로 **0.090m** 앞이다(MuJoCo 로 실측). 그래서 큐브를 물려면 link6 을
z≈0.095 에 둔다 — 그때 손가락 끝이 z≈0.005, 즉 4cm 큐브를 손가락 면이 감싼다.

⚠ 통 위 0.22m 는 **안 닿는다**(5.66mm/3.89° 로 실패). 0.20 이 한계 안쪽이다. 통이 y=−0.30
이나 +0.25 여도 안 닿는다 — 가상환경마다 다르므로 **미리 풀어 보고 안 되면 말한다.**
"""

import logging
import math
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

#: 리더 세그먼트 — **웹 리더와 같은 것을 쓴다.**
#:
#: ⚠ 처음엔 `sim_leader1` 로 따로 뒀다가 되돌렸다. 녹화 프로세스는 `piper_leader_shm` 으로
#:   이 세그먼트를 읽을 뿐 **누가 쓰는지는 안 본다** — 조종 창이든 시연이든 게이트웨이가
#:   리더 노릇을 하는 것은 같다. 따로 두면 수집 화면에 리더 종류가 하나 더 생기고, 사람은
#:   "웹 리더"와 "시뮬 리더" 중 무엇을 고를지 매번 판단해야 한다. 이름을 나눈 근거였던
#:   "둘이 같이 쓰면 누가 민 자세인지 모른다"는 애초에 **동시에 못 돌게** 해서 사라진다
#:   (아래 `start`, 그리고 `web_leader.start` 의 반대쪽 가드).
from app.services.web_leader import LEADER_NAME  # noqa: E402  (재수출 — 정본은 웹 리더다)

FOLLOWER = "sim_follower1"

#: 위에서 잡는 자세 (도) — 무작위 표본에서 찾은 **실제 해**다. 손으로 적은 회전은 IK 가 못 푼다.
#: ⚠ **캘리브레이션 한계 안**에서 찾은 것이다(아래 `_model` 참고). URDF 한계로 찾은 첫 해는
#:   궤적의 절반에서 joint5 가 정규화 100 을 넘었고(최대 115), 실기는 거기 못 간다 —
#:   시뮬에서만 되는 데이터가 된다. 실측: 그 궤적은 큐브를 못 집고 밀기만 했다.
GRASP_SEED_DEG = (-5.9, 121.6, -76.9, -0.8, 43.0, 81.5)
#: link6 → 손가락 끝 (m, 툴 +z). MuJoCo 실측 0.0904.
FINGER_REACH = 0.090
#: 파지 때 link6 높이 — 손가락 끝이 테이블 바로 위(z≈0.005)에 온다.
GRASP_Z = 0.095
APPROACH_Z = 0.195      # 큐브 위에서 내려오기 전
LIFT_Z = 0.20           # 들어 나르는 높이. ⚠ 0.22 는 통 위에서 안 닿는다(실측)
DROP_Z = 0.16           # 통 위에서 놓는 높이
PUBLISH_HZ = 30.0
GRIPPER_OPEN, GRIPPER_CLOSED = 100.0, 0.0

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")
PARKING = {"joint1": 0.0, "joint2": -100.0, "joint3": 100.0, "joint4": 0.0,
           "joint5": 0.0, "joint6": 0.0, "gripper": 0.0}

#: 한 바퀴의 구간 — (이름, 목표 위치를 만드는 법, 그리퍼, 초).
#: 그리퍼는 **닫는 데 시간을 준다** — 시뮬 파지는 2초 램프로 맞춰 뒀다(사용자 요청 2026-09-14).
PHASES = [
    ("접근", "cube", APPROACH_Z, GRIPPER_OPEN, 1.5),
    ("하강", "cube", GRASP_Z, GRIPPER_OPEN, 1.2),
    ("파지", "cube", GRASP_Z, GRIPPER_CLOSED, 2.0),
    ("들기", "cube", LIFT_Z, GRIPPER_CLOSED, 1.2),
    ("이송", "bin", LIFT_Z, GRIPPER_CLOSED, 1.8),
    ("하강", "bin", DROP_Z, GRIPPER_CLOSED, 0.9),
    ("놓기", "bin", DROP_Z, GRIPPER_OPEN, 0.9),
    ("복귀", "bin", LIFT_Z, GRIPPER_OPEN, 0.9),
]


#: 한 바퀴에 걸리는 시간(초) — 구간 합 + 파킹. ⚠ 수집의 `episode_time_s` 를 여기 맞춰야
#: 에피소드 하나에 한 바퀴가 담긴다. 60초로 두면 한 에피소드에 다섯 바퀴가 들어가고,
#: 정책은 "집어 넣고 또 집어 넣는" 것을 한 동작으로 배운다.
CYCLE_PLAN_S = round(sum(p[4] for p in PHASES) + 1.5, 1)


class DemoError(RuntimeError):
    """사람이 고칠 수 있는 실패 — 라우터가 400 으로 돌려준다."""


def _model():
    """⚠ 한계를 **캘리브레이션 표로 좁힌다.**

    `ArmModel` 은 URDF 한계를 쓰는데(joint5 ±75°) 실기 캘리브레이션은 ±65° 다 — sim-env
    1단계의 메모 그대로다. 그 차이 안에서 IK 가 해를 고르면 정규화 값이 ±100 을 넘고,
    `denormalize_all` 이 그걸 잘라서 팔이 **명령한 자세에 영영 못 간다**. 실측: 그렇게 만든
    궤적은 큐브를 집는 대신 밀어냈다(z 가 0.02 에서 안 올라갔다).

    시뮬 데이터가 실기에서 재생돼야 한다는 것이 이 기획의 전제이므로, 시뮬 쪽을 좁히는
    것이 맞다 — 반대가 아니다.
    """
    from piper_robot.armmodel import ArmModel
    from piper_robot.joints import JOINT_CALIBRATION

    m = ArmModel.load("piper")
    cal = np.array([[JOINT_CALIBRATION[j][0], JOINT_CALIBRATION[j][1]] for j in JOINTS]) \
        * math.pi / 180000.0
    m.limits = np.stack([np.maximum(m.limits[:, 0], cal[:, 0]),
                         np.minimum(m.limits[:, 1], cal[:, 1])], axis=1)
    return m


def grasp_rotation(model=None) -> np.ndarray:
    """위에서 잡는 회전 — 찾은 해의 것을 그대로 쓴다(위 주석)."""
    return model.fk(np.radians(GRASP_SEED_DEG))[:3, :3].copy()


def _norm_from_rad(q_rad) -> dict:
    from app.services.relay import _norm_from_rad as conv

    return conv(q_rad)


def plan(cube_xy, bin_xy) -> list[tuple[str, list[float], float, float]]:
    """구간을 **절대 위치**로 편다. 큐브·통 위치는 지금 세계에서 온다."""
    where = {"cube": list(cube_xy), "bin": list(bin_xy)}
    return [(name, [*where[what], z], grip, secs) for name, what, z, grip, secs in PHASES]


def check_reach(cube_xy, bin_xy, model=None) -> list[str]:
    """못 닿는 구간을 미리 찾는다 — **돌려 보고 알면 에피소드를 버린다.**

    통이 y=−0.30 이면 안 닿는다(실측). 가상환경마다 다르므로 시작 전에 전부 풀어 본다.
    """
    m = model or _model()
    R = grasp_rotation(m)
    seed = np.radians(GRASP_SEED_DEG)
    bad = []
    for name, p, _grip, _s in plan(cube_xy, bin_xy):
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = R, p
        sol = m.ik(T, seed)
        if sol.ok:
            seed = sol.q
        else:
            bad.append(f"{name} ({p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f}) — {sol.pos_mm:.0f}mm 모자람")
    return bad


class SimDemo:
    """시연 하나. 리더 세그먼트를 발행하고, 원하면 릴레이도 켠다."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "idle"
        self.phase = ""
        self.episode = 0
        self.episodes = 0
        self.cycle_s = 0.0
        self.error = ""

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {"running": self.is_running, "state": self.state, "phase": self.phase,
                "episode": self.episode, "episodes": self.episodes,
                "cycle_s": round(self.cycle_s, 2), "cycle_plan_s": CYCLE_PLAN_S,
                "leader": LEADER_NAME, "error": self.error}

    def start(self, episodes: int = 1, relay: bool = True, randomize: bool = True) -> dict:
        """⚠ 웹 리더와 **동시에 못 돈다** — 둘이 같은 팔을 밀면 누가 민 자세인지 알 수 없다."""
        from app.services import sim_robot_client as sim
        from app.services.web_leader import web_leader

        with self._lock:
            if self.is_running:
                raise DemoError("시연이 이미 돌고 있습니다")
            if web_leader.is_running:
                raise DemoError("조종 창이 팔을 잡고 있습니다 — 먼저 끝내세요")
            if not sim.sim_available():
                raise DemoError("simd 가 응답하지 않습니다 — 시뮬 데몬이 떠 있나요?")
            objs = sim.call("objects", default=None)
            if not objs:
                raise DemoError("시뮬 팔이 연결돼 있지 않습니다 — [로봇] 에서 연결하세요")
            cube, target = self._pick_objects(objs)
            bad = check_reach(cube["pos"][:2], target["pos"][:2])
            if bad:
                raise DemoError("팔이 못 닿는 자리가 있습니다: " + " · ".join(bad))
            self.state, self.error, self.episode = "running", "", 0
            self.episodes = max(1, int(episodes))
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(relay, randomize),
                                            daemon=True, name="sim-demo")
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)
        self.state = "idle" if not self.error else "error"
        return self.status()

    @staticmethod
    def _pick_objects(objs: list[dict]) -> tuple[dict, dict]:
        """집을 것과 넣을 곳 — **움직이는 것을 집고 고정물에 넣는다.**

        이름을 박지 않는다(`cube`·`bin`). 가상환경은 사람이 만들고 id 도 사람이 짓는다 —
        이름을 박으면 자기 가상환경에서는 시연이 안 돈다.
        """
        movable = [o for o in objs if o.get("movable")]
        fixed = [o for o in objs if not o.get("movable")]
        if not movable:
            raise DemoError("집을 물체가 없습니다 — 가상환경에 움직이는 물체를 하나 두세요")
        if not fixed:
            raise DemoError("넣을 곳이 없습니다 — 가상환경에 통(고정물)을 하나 두세요")
        return movable[0], fixed[0]

    def _run(self, relay: bool, randomize: bool) -> None:
        from piper_shm import arm as shm_arm

        from app.services import sim_robot_client as sim
        from app.services.relay import RelayError, relay_session

        writer = started_relay = None
        try:
            model = _model()
            R = grasp_rotation(model)
            writer = shm_arm.StateWriter(LEADER_NAME)
            writer.publish(dict(PARKING))         # 릴레이가 시작 전에 한 번 읽는다
            if relay:
                try:
                    relay_session.start(LEADER_NAME, FOLLOWER, "joint", "piper", "piper")
                    started_relay = True
                except RelayError as exc:
                    raise DemoError(str(exc))
            for ep in range(self.episodes):
                if self._stop.is_set():
                    break
                self.episode = ep + 1
                t0 = time.monotonic()
                self._one(writer, model, R, sim, randomize)
                self.cycle_s = time.monotonic() - t0
            self.state = "idle"
            self.phase = ""
        except Exception as exc:
            self.error, self.state = str(exc), "error"
            logger.warning("시연 실패: %s", exc)
        finally:
            if started_relay:
                try:
                    relay_session.stop()
                except Exception as exc:
                    logger.warning("릴레이 정지 실패: %s", exc)
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass

    def _one(self, writer, model, R, sim, randomize: bool) -> None:
        """한 바퀴 — 무작위로 놓고, 집고, 넣고, 파킹으로."""
        objs = sim.call("objects", default=[]) or []
        cube, target = self._pick_objects(objs)
        if randomize:
            # 에피소드마다 다른 자리 — 같은 자리만 모으면 정책이 그 한 점만 배운다
            x, y = float(np.random.uniform(0.28, 0.42)), float(np.random.uniform(-0.12, 0.12))
            try:
                sim.call_strict("place_object", cube["id"], x, y)
                cube = {**cube, "pos": [x, y, cube["pos"][2]]}
            except Exception as exc:
                logger.warning("큐브 재배치 실패 (그 자리에서 진행): %s", exc)
        seed = np.radians(GRASP_SEED_DEG)
        grip = GRIPPER_OPEN
        for name, p, want_grip, secs in plan(cube["pos"][:2], target["pos"][:2]):
            if self._stop.is_set():
                return
            self.phase = name
            T = np.eye(4)
            T[:3, :3], T[:3, 3] = R, p
            sol = model.ik(T, seed)
            if not sol.ok:
                raise DemoError(f"'{name}' 구간을 못 풉니다 ({sol.pos_mm:.0f}mm 모자람)")
            seed = self._ramp(writer, seed, sol.q, grip, want_grip, secs)
            grip = want_grip
        self.phase = "파킹"
        self._park(writer, seed)

    def _ramp(self, writer, q_from, q_to, grip_from, grip_to, secs: float):
        """두 자세 사이를 **걸어간다**. 순간이동하면 팔로워가 못 따라오고 학습 데이터가 튄다."""
        steps = max(2, int(secs * PUBLISH_HZ))
        dt = 1.0 / PUBLISH_HZ
        for i in range(1, steps + 1):
            if self._stop.is_set():
                return q_to
            a = i / steps
            q = q_from + (q_to - q_from) * a
            pose = _norm_from_rad(q)
            pose["gripper"] = grip_from + (grip_to - grip_from) * a
            writer.publish(pose)
            time.sleep(dt)
        return q_to

    def _park(self, writer, q_from) -> None:
        from piper_robot import kinematics as K

        q_park = K.norm_to_rad(np.array([[PARKING[j] for j in JOINTS]], float))[0]
        self._ramp(writer, q_from, q_park, GRIPPER_OPEN, PARKING["gripper"], 1.5)


sim_demo = SimDemo()
