"""SimHub — simd 의 RPC 표면. 로봇 데몬 계약 동사 + 시뮬 전용 진단.

한 세계에 팔 하나(`sim_follower1`). 리더(`sim_leader1`, 스크립트 시연)는 4단계.
"""

import logging

from piper_sim.bridge import SimArmBridge
from piper_sim.world import World

logger = logging.getLogger(__name__)

ARM_NAME = "sim_follower1"


class SimError(RuntimeError):
    pass


class SimHub:
    def __init__(self) -> None:
        self.world: World | None = None
        self.bridges: dict[str, SimArmBridge] = {}
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
            return {
                "arm": a, "running": b.running, "published": b.published,
                "sent": b.sent, "filtered": b.filtered,
                "last_reason": b.last_reason.value, "torque_on": b.torque_on,
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
        now = self._world().snapshot()
        goal, _reason = filter_goal(now, dict(norm_goal), b.safety, deadman_tripped=False)
        self._world().set_goal(goal)
        return True

    def cube_pos(self) -> list[float]:
        return self._world().cube_pos()

    def reset_cube(self, x: float = 0.35, y: float = 0.0) -> list[float]:
        w = self._world()
        w.reset_cube(float(x), float(y))
        return w.cube_pos()
