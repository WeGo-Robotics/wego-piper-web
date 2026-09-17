"""SimHub — simd 의 RPC 표면. 로봇 데몬 계약 동사 + 시뮬 전용 진단.

한 세계에 팔 하나(`sim_follower1`). 스크립트 시연은 게이트웨이가 조종 창과 **같은 리더
세그먼트**로 발행한다 (4단계, backend/app/services/sim_demo.py) — 여기 리더는 없다.
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
            # ⚠ 가상환경이 갈리면 카메라 렌더러가 옛 모델을 쥔 채 남는다 — 그러면 팔은 새
            #   세계에서 도는데 화면엔 옛 세계가 나온다. 세계가 직접 알린다.
            self.world.on_model_change.append(self.cameras.invalidate_model)
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

    # ── 가상환경 (feature/sim-scene-editor.md) ─────────────────────────────────

    def scene(self) -> dict:
        """지금 올라간 가상환경 — 명세 + 물체의 지금 위치.

        게이트웨이가 이걸 보고 "내가 적용한 가상환경이 맞나"를 판단한다. simd 가 재시작하면
        기본 가상환경으로 돌아오므로, 그 불일치를 **게이트웨이가 보고 다시 올린다** — 데몬이
        파일을 읽게 하지 않는 이유는 §6 에 적었다(컨테이너는 `/data`, 호스트는
        `/srv/piper-data` 라 같은 id 가 서로 다른 경로가 된다).
        """
        w = self._world()
        return {"spec": w.spec, "objects": w.objects()}

    def load_scene(self, spec: dict) -> dict:
        """가상환경을 갈아끼운다. 명세는 **여기서도 검증한다** — 데몬은 자기 입력을 안 믿는다."""
        return self._world().load_scene(spec)

    def objects(self) -> list[dict]:
        return self._world().objects()

    def default_object(self) -> str:
        """이름을 안 주면 **첫 번째 움직이는 물체**. 시연이 쓰는 규칙과 같다.

        ⚠ 조종 창의 R·B 는 `cube` 를 박아 두고 있었다. 기본 가상환경에서는 맞지만 사람이
        만든 환경은 물체 이름이 `box1` 이라 **둘 다 죽었다**(실기 보고 2026-09-17).
        이름을 박으면 자기 환경에서 안 되는 것은 이 저장소가 여러 번 겪은 실수다.
        """
        for o in self._world().objects():
            if o.get("movable"):
                return o["id"]
        raise SimError("움직이는 물체가 없습니다 — 가상환경에 하나 두세요")

    def reset_objects(self, **_) -> list[dict]:
        """움직이는 물체 전부를 제자리로 (팔은 그대로). 조종 창의 [환경 리셋]."""
        return self._world().reset_objects()

    def place_object(self, oid: str, x: float, y: float) -> list[float]:
        return self._world().place(str(oid), float(x), float(y))

    def point_from_view(self, cam: str = "top", u: float = 0.5, v: float = 0.5,
                        aspect: float = 4.0 / 3.0, z: float = 0.0) -> dict:
        """클릭한 픽셀 → **테이블 위의 한 점**. 물체는 안 건드린다.

        고정물(통)은 자유관절이 없어 qpos 로 못 옮긴다 — 자리를 바꾸려면 가상환경을 고쳐
        다시 올려야 한다. 편집기가 그 좌표를 여기서 받아 명세에 적는다.
        """
        name = str(cam).split(":", 1)[-1]
        hit = self._world().ray_to_table(name, float(u), float(v), float(aspect), float(z))
        return {"point": hit, "ok": hit is not None}

    def pick_from_view(self, cam: str = "top", u: float = 0.5, v: float = 0.5,
                       aspect: float = 4.0 / 3.0, radius: float = 0.07) -> dict:
        """클릭한 픽셀 **위에 있는** 움직이는 물체를 찾는다 (옮기기 전의 "집기").

        물체가 여럿이면 어느 것을 옮길지 사람이 찍어야 한다 — 첫 번째를 고르던 규칙은
        물체가 하나일 때만 맞았다.
        """
        name = str(cam).split(":", 1)[-1]
        hit = self._world().ray_to_table(name, float(u), float(v), float(aspect), 0.0)
        if hit is None:
            return {"ok": False, "object": None, "reason": "클릭한 자리가 테이블이 아닙니다"}
        found = self._world().object_at(hit[0], hit[1], float(radius))
        return ({"ok": True, "object": found} if found else
                {"ok": False, "object": None, "reason": "그 자리에 옮길 수 있는 물체가 없습니다"})

    def object_from_view(self, cam: str = "top", u: float = 0.5, v: float = 0.5,
                         aspect: float = 4.0 / 3.0, oid: str = "") -> dict:
        """클릭한 카메라 픽셀로 물체를 옮긴다. cam 은 `sim:top`/`top` 둘 다 받는다.

        `oid` 를 안 주면 첫 번째 움직이는 물체다(`default_object`) — 사람이 만든 환경에서도 돈다.
        """
        name = str(cam).split(":", 1)[-1]
        target = str(oid) or self.default_object()
        hit = self._world().object_from_ray(name, float(u), float(v), float(aspect), target)
        return {"object": target, "cube": hit, "ok": hit is not None}

    # ── 옛 이름 (게이트웨이가 아직 이 어휘를 쓴다) ──────────────────────────

    def cube_pos(self) -> list[float] | None:
        return self._world().object_pos(self.default_object())

    def reset_cube(self, x: float = 0.35, y: float = 0.0) -> list[float]:
        return self._world().place(self.default_object(), float(x), float(y))

    #: 씬의 큐브 시작 위치·파킹 자세 (build_sim_scene.py 와 같은 값)
    CUBE_START = (0.35, 0.0)
    PARKING = {"joint1": 0.0, "joint2": -100.0, "joint3": 100.0, "joint4": 0.0,
               "joint5": 0.0, "joint6": 0.0, "gripper": 0.0}

    def cube_from_view(self, cam: str = "top", u: float = 0.5, v: float = 0.5,
                       aspect: float = 4.0 / 3.0) -> dict:
        # ⚠ 이름을 안 넘긴다 — 넘기면 사람이 만든 환경(물체가 `box1`)에서 죽는다.
        return self.object_from_view(cam, u, v, aspect, "")

    def reset(self, arm_only: bool = False, cube_x: float | None = None, cube_y: float | None = None) -> dict:
        """환경 리셋 — 팔 파킹·속도 0. arm_only 면 큐브·조명은 그대로(T: 로봇 위치만
        초기화), 아니면 큐브도 시작 위치로 (feature/web-leader.md §5)."""
        w = self._world()
        # 시작 자리는 **가상환경 JSON** 이 쥔다 — 움직이는 물체 전부가 제자리로 간다.
        # `cube_x/y` 는 그 중 큐브만 다른 자리에 놓는 옛 인자다(시연 무작위화).
        # 덮어쓰기 키도 **이름이 아니라** 첫 움직이는 물체다 — 사람이 만든 환경에서도 먹게
        over = ({self.default_object(): (float(cube_x), float(cube_y))}
                if not arm_only and cube_x is not None and cube_y is not None else None)
        w.reset(self.PARKING, objects=not arm_only, overrides=over)
        # `cube` 키는 옛 부르는 쪽을 위한 자리다 — 값은 **첫 움직이는 물체**다(이름을 안 박는다)
        return {"cube": self.cube_pos(), "objects": w.objects(),
                "parking": self.PARKING, "arm_only": arm_only}
