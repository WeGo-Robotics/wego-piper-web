"""World — 물리 루프 한 개, 실시간 페이싱 (feature/sim-env.md §6).

물리 500Hz(timestep 2ms), 벽시계에 맞춘다 — LeRobot 수집·추론 루프가 벽시계라
시뮬이 빨리 돌면 fps 가 거짓이 된다. 명령은 `set_goal` 로 들어와 다음 스텝의
액추에이터 목표가 되고, 상태는 `snapshot` 으로 나간다. 스레드 하나가 스텝을
돌리고 락은 goal/snapshot 교환에만 잡는다.
"""

import logging
import threading
import time

from piper_sim.scene import (ARM_JOINTS, FINGERS, JOINT_CALIBRATION,
                              JointMap, MILLIDEG_PER_RAD, denormalize_all, load_model)

logger = logging.getLogger(__name__)

PHYSICS_HZ = 500.0


class World:
    def __init__(self, spec: dict | None = None) -> None:
        import mujoco
        from piper_sim import scene_spec

        #: 지금 올라간 장면(검증된 사본). 테이블 위 사물의 **정본**이다 — 바탕 XML 에는
        #: 팔·테이블·카메라만 있다 (feature/sim-scene-editor.md).
        self.spec = scene_spec.validate(spec) if spec is not None else scene_spec.default()
        self.model = load_model(self.spec)
        self.data = mujoco.MjData(self.model)
        self.jm = JointMap(self.model)
        #: 모델이 바뀌면 부를 것들. ⚠ 렌더러는 **만들 때의 모델**을 쥔다 — 안 버리면
        #: 장면을 갈아끼워도 옛 세계를 계속 그린다. 버리는 일은 렌더 스레드가 한다(EGL).
        self.on_model_change: list = []
        self._lock = threading.Lock()
        self._goal: dict[str, float] | None = None
        self._hold = True
        self._running = False
        self._thread: threading.Thread | None = None
        self.steps = 0
        self.lag_s = 0.0          # 벽시계 대비 밀린 시간 — 진단용
        mujoco.mj_forward(self.model, self.data)
        self.jm.hold(self.data)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sim-physics")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def set_goal(self, norm_goal: dict[str, float]) -> None:
        with self._lock:
            self._goal = dict(norm_goal)
            self._hold = False

    def hold(self) -> None:
        """데드맨·E-stop — 지금 자세를 목표로 **한 번** 래치한다.

        ⚠ 매 스텝 ctrl=qpos 로 다시 잡으면 서보가 처지는 자세를 계속 따라가
        "정지"가 미끄러진다 (실측: 펼친 자세 2초에 7.5, 데드맨 뒤 0.5초에 1.9).
        래치는 루프가 다음 스텝에 딱 한 번 한다 — 그 뒤 ctrl 은 고정이다.
        """
        with self._lock:
            self._goal = None
            self._hold = True

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return self.jm.read_norm(self.data)

    # ── 장면의 물체 (feature/sim-scene-editor.md §1) ─────────────────────────
    # ⚠ 예전엔 이 절 전체가 `cube` 하나로 박혀 있었다 — 이름도, 안착 높이 0.02 도.
    #   물체를 늘리는 순간 전부 다시 써야 하는 형태였다. 지금은 **id 를 받는 쪽이 본체**이고
    #   `cube_*` 는 옛 부르는 쪽(게이트웨이 `/leader/web/cube`)을 위한 껍질이다.

    def objects(self) -> list[dict]:
        """장면의 물체 — 명세 + **지금 위치**. 편집기 목록과 배치 화면이 이걸 읽는다."""
        with self._lock:
            out = []
            for o in self.spec["objects"]:
                bid = self.jm.objects.get(o["id"])
                if bid is not None:
                    out.append({"id": o["id"], "label": o["label"], "shape": o["shape"],
                                "movable": o["movable"],
                                "pos": [float(v) for v in self.data.xpos[bid]]})
            return out

    def _spec_of(self, oid: str) -> dict | None:
        return next((o for o in self.spec["objects"] if o["id"] == oid), None)

    def object_pos(self, oid: str = "cube") -> list[float] | None:
        with self._lock:
            bid = self.jm.objects.get(oid)
            return None if bid is None else [float(v) for v in self.data.xpos[bid]]

    def place(self, oid: str, x: float, y: float, z: float | None = None) -> list[float]:
        """물체를 테이블 위 (x, y) 에 다시 놓는다 — 시연 무작위화·에피소드 리셋·클릭 배치.

        높이는 모양에서 온다(`scene_spec.rest_z`) — 상수 0.02 는 큐브에만 맞았다. 자세는
        장면이 적은 각도로 되돌린다(굴러간 물체가 반듯하게 선다).

        ⚠ **고정물은 못 옮긴다.** 자유관절이 없어 qpos 에 자리가 없고 body pos 는 컴파일에
        박혀 있다 — 옮기려면 장면을 고쳐 다시 올려야 한다. 조용히 무시하면 "눌렀는데 안
        움직인다"가 되므로 말해 준다.
        """
        import mujoco
        from piper_sim import scene_spec

        obj = self._spec_of(oid)
        if obj is None:
            raise ValueError(f"'{oid}' 라는 물체가 장면에 없습니다")
        if not obj["movable"]:
            raise ValueError(f"'{oid}' 는 고정물이라 못 옮깁니다 — 장면을 고쳐 다시 올리세요")
        zz = scene_spec.rest_z(obj) if z is None else float(z)
        q = scene_spec.euler_quat(obj["euler_deg"])
        with self._lock:
            adr = self.model.jnt_qposadr[mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_JOINT, f"{oid}_free")]
            self.data.qpos[adr:adr + 7] = [float(x), float(y), zz, *q]
            self.data.qvel[:] = 0
            mujoco.mj_forward(self.model, self.data)
        return [float(x), float(y), zz]

    def load_scene(self, spec: dict) -> dict:
        """장면을 갈아끼운다 — **모델 재컴파일 + MjData 신규**. 공짜가 아니다.

        ⚠ **팔 자세를 옮겨 심는다.** 안 그러면 갈아끼울 때마다 팔이 원점으로 튄다 —
        조종 중이거나 수집 직전이면 그게 사고다. ctrl 도 그 자리로 래치한다(`hold`).
        ⚠ **컴파일을 먼저 한다.** 깨진 장면이면 예외가 여기서 나고 **옛 세계는 그대로**다.
        먼저 버리고 나중에 짓는 순서였다면 장면 하나 잘못 올려 시뮬이 통째로 죽는다.
        ⚠ 렌더러는 옛 모델을 쥐고 있다 — `on_model_change` 로 알린다(버리는 것은 렌더
        스레드가 한다, EGL 컨텍스트는 스레드 귀속이라).
        """
        import mujoco
        from piper_sim import scene_spec

        v = scene_spec.validate(spec)
        model = load_model(v)                       # ← 여기서 실패하면 아무것도 안 바뀐다
        data = mujoco.MjData(model)
        jm = JointMap(model)
        with self._lock:
            for n in ARM_JOINTS + FINGERS:
                data.qpos[jm.qadr[n]] = self.data.qpos[self.jm.qadr[n]]
            mujoco.mj_forward(model, data)
            jm.hold(data)                           # 서보 목표 = 지금 자세 (튐 방지)
            self.model, self.data, self.jm, self.spec = model, data, jm, v
            self._goal, self._hold = None, True
        for cb in list(self.on_model_change):
            try:
                cb()
            except Exception as exc:
                logger.warning("모델 교체 알림 실패: %s", exc)
        logger.info("장면 교체: %s (물체 %d)", v["name"], len(v["objects"]))
        return {"scene": v["name"], "objects": self.objects()}

    def ray_to_table(self, cam_name: str, u: float, v: float, aspect: float,
                     z_plane: float = 0.0) -> list[float] | None:
        """클릭한 픽셀(정규화 u,v: 0..1, u=오른쪽 v=아래) → **테이블 평면 위의 한 점**.

        카메라 자세·fovy 를 아는 여기서 계산해야 정확하다. 테이블 밖은 가장자리로 클램프.
        못 맞히면(뒤·평행) None. 편집기의 "끌어 배치"도 이걸 쓴다 — 물체를 안 건드린다.
        """
        import math

        import mujoco
        import numpy as np

        with self._lock:
            cid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, cam_name)
            if cid < 0:
                return None
            pos = np.array(self.data.cam_xpos[cid], float)
            R = np.array(self.data.cam_xmat[cid], float).reshape(3, 3)
            tan_v = math.tan(math.radians(float(self.model.cam_fovy[cid])) / 2.0)
            tan_u = tan_v * max(aspect, 1e-3)
            # MuJoCo 카메라는 -z 를 본다. 이미지 오른쪽=+x_cam, 위=+y_cam.
            d_cam = np.array([(u - 0.5) * 2.0 * tan_u, (0.5 - v) * 2.0 * tan_v, -1.0])
            d = R @ d_cam
            if abs(d[2]) < 1e-6 or (z_plane - pos[2]) / d[2] <= 0:
                return None                    # 광선이 평면과 평행하거나 뒤로 간다
            hit = pos + (z_plane - pos[2]) / d[2] * d
            # 테이블(중심 0.35,0 · 반폭 0.6×0.5) 안으로, 물체 반폭 여유
            x = float(np.clip(hit[0], 0.35 - 0.55, 0.35 + 0.55))
            y = float(np.clip(hit[1], -0.45, 0.45))
            return [x, y, float(z_plane)]

    def object_from_ray(self, cam_name: str, u: float, v: float, aspect: float,
                        oid: str = "cube") -> list[float] | None:
        """클릭한 픽셀로 물체를 옮긴다 (사용자 요청 2026: 블럭을 손으로 옮기기 어렵다)."""
        import math

        obj = self._spec_of(oid)
        if obj is None:
            raise ValueError(f"'{oid}' 라는 물체가 장면에 없습니다")
        from piper_sim import scene_spec

        hit = self.ray_to_table(cam_name, u, v, aspect, scene_spec.rest_z(obj))
        return None if hit is None else self.place(oid, hit[0], hit[1])

    def reset(self, arm_norm: dict[str, float], objects: bool = True,
              overrides: dict[str, tuple[float, float]] | None = None) -> None:
        """환경 리셋 — 팔을 파킹으로, **움직이는 물체 전부**를 장면이 적은 자리로, 속도 0
        (feature/web-leader.md §5).

        ⚠ 팔 qpos 를 파킹으로 **스냅**하고 목표·ctrl 도 파킹으로 래치한다. qpos 만 옮기고
        목표를 안 바꾸면 서보가 다음 스텝에 옛 목표로 도로 끌어당긴다. 리더(릴레이/웹)가
        계속 옛 자세를 명령하면 팔은 다시 끌려간다 — 그건 게이트웨이가 리더도 함께
        리셋해서 막는다(web_leader.reset_to_parking).

        `objects=False` 는 팔만(T 키). `overrides` 는 특정 물체를 다른 자리에 놓는다
        (시연 무작위화). 시작 자리는 **장면 JSON** 이 쥔다 — 예전엔 상수 하나였다.
        """
        import mujoco
        from piper_sim import scene_spec

        overrides = overrides or {}
        with self._lock:
            for n, v in denormalize_all({k: v for k, v in arm_norm.items() if k in JOINT_CALIBRATION}).items():
                if n in ARM_JOINTS:
                    self.data.qpos[self.jm.qadr[n]] = v / MILLIDEG_PER_RAD
            for f in FINGERS:
                self.data.qpos[self.jm.qadr[f]] = 0.0        # 파킹은 그리퍼 닫힘(0)
            if objects:                                       # 팔만 리셋(T)이면 물체는 그대로
                for o in self.spec["objects"]:
                    if not o["movable"] or o["id"] not in self.jm.objects:
                        continue
                    x, y = overrides.get(o["id"], (o["pos"][0], o["pos"][1]))
                    adr = self.model.jnt_qposadr[mujoco.mj_name2id(
                        self.model, mujoco.mjtObj.mjOBJ_JOINT, f"{o['id']}_free")]
                    self.data.qpos[adr:adr + 7] = [float(x), float(y), scene_spec.rest_z(o),
                                                   *scene_spec.euler_quat(o["euler_deg"])]
            self.data.qvel[:] = 0
            self.jm.write_ctrl(self.data, arm_norm)          # 서보 목표도 파킹
            self._goal = dict(arm_norm)
            self._hold = False
            mujoco.mj_forward(self.model, self.data)

    # ── 옛 이름 (게이트웨이 `/leader/web/cube` 가 아직 이 어휘를 쓴다) ───────────
    def cube_pos(self) -> list[float] | None:
        return self.object_pos("cube")

    def reset_cube(self, x: float, y: float) -> None:
        self.place("cube", x, y)

    def cube_from_ray(self, cam_name: str, u: float, v: float, aspect: float) -> list[float] | None:
        return self.object_from_ray(cam_name, u, v, aspect, "cube")

    def _loop(self) -> None:
        import mujoco

        dt = self.model.opt.timestep
        next_t = time.perf_counter()
        while self._running:
            with self._lock:
                if self._hold:
                    self.jm.hold(self.data)   # 한 번만 래치 — 다음 스텝부터 ctrl 고정
                    self._hold = False
                elif self._goal is not None:
                    self.jm.write_ctrl(self.data, self._goal)
                    self._goal = None       # 한 번 쓰면 액추에이터가 들고 있는다
                mujoco.mj_step(self.model, self.data)
            self.steps += 1
            next_t += dt
            now = time.perf_counter()
            if next_t > now:
                time.sleep(next_t - now)
            else:
                self.lag_s = now - next_t     # 밀렸다 — 따라잡되 기록만 한다
                if self.lag_s > 0.5:
                    next_t = now              # 반초 넘게 밀리면 포기하고 재동기
