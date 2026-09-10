"""SimHub — simd 의 RPC 표면. 로봇 데몬 계약 동사 + 시뮬 전용 진단.

한 세계에 팔 하나(`sim_follower1`). 리더(`sim_leader1`, 스크립트 시연)는 4단계.
"""

import logging
import threading
import time
from typing import Callable

from piper_sim.bridge import SimArmBridge
from piper_sim.world import World

logger = logging.getLogger(__name__)

ARM_NAME = "sim_follower1"

#: 램프 걸음 간격·예산·도달 허용치(정규화 단위). 50Hz 는 조그가 목표를 보내는
#: 빈도와 같은 급 — 필터의 step_limit 이 그 빈도를 전제로 잡혀 있다.
RAMP_DT_S = 0.02
RAMP_BUDGET_S = 10.0
RAMP_TOL = 0.5


class SimError(RuntimeError):
    pass


def ramp_goal(snapshot: Callable[[], dict], set_goal: Callable[[dict], None],
              target: dict, safety, running: Callable[[], bool] = lambda: True,
              stop: threading.Event | None = None,
              dt: float = RAMP_DT_S, budget_s: float = RAMP_BUDGET_S) -> bool:
    """목표까지 **걸어간다** — 안전 필터(`filter_goal`)는 호출 한 번에 관절당
    step_limit 만큼만 허용하므로 한 번 세팅하면 한 걸음만 가고 멈춘다(실측: 파킹
    −100 → −80 정지). 매 걸음 지금 자세에서 필터를 다시 지나 다음 목표를 세팅한다.
    브리지가 죽거나(`running`) 새 램프가 오면(`stop`) 그 자리에서 멈춘다.
    반환: 예산 안에 모든 관절이 허용치 안에 들어왔는가. 부작용은 set_goal 뿐."""
    from piper_robot.safety import filter_goal

    deadline = time.monotonic() + budget_s
    while running() and not (stop and stop.is_set()):
        now = snapshot()
        if all(abs(now.get(j, 0.0) - v) <= RAMP_TOL for j, v in target.items()):
            return True
        goal, _reason = filter_goal(now, dict(target), safety, deadman_tripped=False)
        set_goal(goal)
        if time.monotonic() >= deadline:
            return False
        time.sleep(dt)
    return False


class SimHub:
    def __init__(self) -> None:
        self.world: World | None = None
        self.bridges: dict[str, SimArmBridge] = {}
        # go_to 램프 — RPC 루프가 단일 스레드라 여기서 블로킹하면 램프 내내
        # scan/info/카메라가 멈춘다. 스레드로 돌리고 info 의 moving/reached 로 알린다.
        self._ramps: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._ramp_result: dict[str, bool | None] = {}
        # 카메라 — 팔과 같은 세계를 그린다. RPC 이름이 팔 동사(scan/info/lost)와
        # 겹쳐 `cam_` 접두사로 나른다; 게이트웨이 클라이언트가 어휘를 되돌린다.
        from piper_sim.cameras import SimCameraHub
        self.cameras = SimCameraHub(self._world)

    def _world(self) -> World:
        if self.world is None:
            self.world = World()
            self.world.start()
            logger.info("MuJoCo 세계 시작 (nq=%d)", self.world.model.nq)
        return self.world

    # ── 계약 동사 ──

    def scan(self) -> list[dict]:
        b = self.bridges.get(ARM_NAME)
        return [{"id": ARM_NAME, "model": "piper", "transport": "sim",
                 "arm": ARM_NAME if (b and b.running) else None, "present": True,
                 "scene": "piper_scene"}]

    def attach(self, arm_name: str = "", **_) -> dict:
        name = arm_name or ARM_NAME
        if name != ARM_NAME:
            raise SimError(f"이 세계에는 {ARM_NAME} 하나뿐입니다: {name}")
        b = self.bridges.get(name)
        if b and b.running:
            raise SimError(f"{name} 은 이미 연결돼 있습니다")
        b = SimArmBridge(name, self._world())
        b.start()
        self.bridges[name] = b
        return self.info(name)

    def release(self, arm_name: str) -> bool:
        b = self.bridges.pop(arm_name, None)
        if b is not None:
            b.stop()
        return b is not None

    def release_all(self) -> bool:
        for a in list(self.bridges):
            self.release(a)
        return True

    def estop(self, arm_name: str = "") -> list[str]:
        hit = [a for a in self.bridges if not arm_name or a == arm_name]
        for a in hit:
            self.bridges[a].estop()
        return hit

    def info(self, arm_name: str = "") -> dict:
        def one(a: str, b: SimArmBridge) -> dict:
            ramp = self._ramps.get(a)
            return {
                "arm": a, "running": b.running, "published": b.published,
                "sent": b.sent, "filtered": b.filtered,
                "last_reason": b.last_reason.value, "torque_on": b.torque_on,
                # go_to 램프 — moving 이 False 가 된 뒤의 reached 가 완주 여부
                "moving": bool(ramp and ramp[0].is_alive()),
                "reached": self._ramp_result.get(a),
                "capabilities": {
                    "model": "piper", "transport": "sim", "dof": 6, "gripper": True,
                    "kinematics": "piper",
                    "joint_names": ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "gripper"],
                    # 시뮬에 없는 것은 없다고 **선언**한다 — UI 가 안 그린다
                    "features": {"master_slave": False, "hw_zero": False, "slip_reset": False,
                                 "parking": True, "rate_limit": True},
                },
            }
        if arm_name:
            b = self.bridges.get(arm_name)
            if b is None:
                raise SimError(f"모르는 팔: {arm_name}")
            return one(arm_name, b)
        return {"arms": [one(a, b) for a, b in self.bridges.items()],
                "physics_steps": self.world.steps if self.world else 0,
                "lag_s": round(self.world.lag_s, 4) if self.world else 0.0}

    def lost(self) -> list[dict]:
        return []       # 시뮬 팔은 사라지지 않는다

    # ── 카메라 (camerad 어휘 → cam_ 접두사) ──

    def cam_scan(self): return self.cameras.scan()
    def cam_connect(self, cam_id, width=0, height=0, fps=0, controls=None):
        return self.cameras.connect(cam_id, width, height, fps, controls)
    def cam_disconnect(self, cam_id): return self.cameras.disconnect(cam_id)
    def cam_release_all(self): return self.cameras.release_all()
    def cam_probe(self, cam_id): return self.cameras.probe(cam_id)
    def cam_list_controls(self, cam_id): return self.cameras.list_controls(cam_id)
    def cam_set_control(self, cam_id, name, value): return self.cameras.set_control(cam_id, name, value)
    def cam_apply_controls(self, cam_id, wanted): return self.cameras.apply_controls(cam_id, wanted)
    def cam_last_apply_report(self, cam_id): return self.cameras.last_apply_report(cam_id)
    def cam_info(self, cam_id): return self.cameras.info(cam_id)
    def cam_lost(self): return self.cameras.lost()
    def set_light(self, scale): return self.cameras.set_light(scale)

    # ── 시뮬 전용 ──

    def go_to(self, arm_name: str, norm_goal: dict) -> bool:
        """파킹 등 게이트웨이가 시키는 이동 — 안전 필터를 지나 세계 목표로.
        action 세그먼트를 거치지 않는 이유: 데드맨(300ms)이 한 번 쓴 목표를
        곧 되감아, 파킹처럼 몇 초 걸리는 이동은 세그먼트로는 완주하지 못한다."""
        from piper_robot.safety import filter_goal

        b = self.bridges.get(arm_name)
        if b is None or not b.running:
            raise SimError(f"{arm_name} 이 연결돼 있지 않습니다")
        # ⚠ 필터는 호출 한 번에 관절당 step_limit 만큼만 허용한다 — 한 번 세팅하고
        #   끝내면 **한 걸음(20)만 가고 멈춘다**(실측: 파킹 −100 → −80 에서 정지).
        #   robotd 의 go_parking 처럼 완주까지 걸어간다 — 단 RPC 루프가 단일
        #   스레드라 **여기서 기다리지 않는다**: 스레드가 걷고, 완주 여부는
        #   info(arm)['moving'/'reached'] 로 게이트웨이가 폴링한다.
        old = self._ramps.pop(arm_name, None)
        if old is not None:
            old[1].set()
            old[0].join(timeout=1.0)
        w = self._world()
        stop = threading.Event()
        self._ramp_result[arm_name] = None

        def run() -> None:
            self._ramp_result[arm_name] = ramp_goal(
                w.snapshot, w.set_goal, dict(norm_goal), b.safety,
                running=lambda: b.running, stop=stop)

        t = threading.Thread(target=run, name=f"sim-ramp-{arm_name}", daemon=True)
        self._ramps[arm_name] = (t, stop)
        t.start()
        return True

    def cube_pos(self) -> list[float]:
        return self._world().cube_pos()

    def reset_cube(self, x: float = 0.35, y: float = 0.0) -> list[float]:
        w = self._world()
        w.reset_cube(float(x), float(y))
        return w.cube_pos()

    #: 씬의 큐브 시작 위치·파킹 자세 (build_sim_scene.py 와 같은 값)
    CUBE_START = (0.35, 0.0)
    PARKING = {"joint1": 0.0, "joint2": -100.0, "joint3": 100.0, "joint4": 0.0,
               "joint5": 0.0, "joint6": 0.0, "gripper": 0.0}

    def reset(self, arm_only: bool = False, cube_x: float | None = None, cube_y: float | None = None) -> dict:
        """환경 리셋 — 팔 파킹·속도 0. arm_only 면 큐브·조명은 그대로(T: 로봇 위치만
        초기화), 아니면 큐브도 시작 위치로 (feature/web-leader.md §5)."""
        w = self._world()
        if arm_only:
            w.reset(self.PARKING, None, None)
        else:
            cx = self.CUBE_START[0] if cube_x is None else float(cube_x)
            cy = self.CUBE_START[1] if cube_y is None else float(cube_y)
            w.reset(self.PARKING, cx, cy)
        return {"cube": w.cube_pos(), "parking": self.PARKING, "arm_only": arm_only}
