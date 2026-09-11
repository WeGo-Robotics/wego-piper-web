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
    def __init__(self) -> None:
        import mujoco

        self.model = load_model()
        self.data = mujoco.MjData(self.model)
        self.jm = JointMap(self.model)
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

    def cube_pos(self) -> list[float]:
        with self._lock:
            return [float(v) for v in self.data.xpos[self.jm.cube]]

    def reset_cube(self, x: float, y: float) -> None:
        """큐브를 테이블 위 (x, y) 에 다시 놓는다 — 시연 무작위화·에피소드 리셋."""
        import mujoco

        with self._lock:
            adr = self.model.jnt_qposadr[mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_JOINT, "cube_free")]
            self.data.qpos[adr:adr + 7] = [x, y, 0.02, 1, 0, 0, 0]
            self.data.qvel[:] = 0
            mujoco.mj_forward(self.model, self.data)

    def cube_from_ray(self, cam_name: str, u: float, v: float, aspect: float) -> list[float] | None:
        """탑뷰 등에서 클릭한 픽셀(정규화 u,v: 0..1, u=오른쪽 v=아래)을 카메라 광선으로
        쏴 **테이블 평면과 만나는 지점**으로 큐브를 옮긴다 (사용자 요청 2026: 블럭을 손으로
        옮기기 어렵다 → 클릭으로 순간이동). 카메라 자세·fovy 를 아는 여기서 계산해야
        정확하다. 테이블 밖은 가장자리로 클램프. 못 맞히면(뒤·평행) None."""
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
            z_plane = 0.02                     # 큐브 중심 안착 높이(테이블 위)
            if abs(d[2]) < 1e-6 or (z_plane - pos[2]) / d[2] <= 0:
                return None                    # 광선이 평면과 평행하거나 뒤로 간다
            hit = pos + (z_plane - pos[2]) / d[2] * d
            # 테이블(중심 0.35,0 · 반폭 0.6×0.5) 안으로, 큐브 반폭 여유
            x = float(np.clip(hit[0], 0.35 - 0.55, 0.35 + 0.55))
            y = float(np.clip(hit[1], -0.45, 0.45))
            adr = self.model.jnt_qposadr[mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_JOINT, "cube_free")]
            self.data.qpos[adr:adr + 7] = [x, y, z_plane, 1, 0, 0, 0]
            self.data.qvel[:] = 0
            mujoco.mj_forward(self.model, self.data)
            return [x, y, z_plane]

    def reset(self, arm_norm: dict[str, float], cube_x: float | None = None, cube_y: float | None = None) -> None:
        """환경 리셋 — 큐브를 시작 위치로, 팔을 파킹으로, 속도 0 (feature/web-leader.md §5).

        ⚠ 팔 qpos 를 파킹으로 **스냅**하고 목표·ctrl 도 파킹으로 래치한다. qpos 만 옮기고
        목표를 안 바꾸면 서보가 다음 스텝에 옛 목표로 도로 끌어당긴다. 리더(릴레이/웹)가
        계속 옛 자세를 명령하면 팔은 다시 끌려간다 — 그건 게이트웨이가 리더도 함께
        리셋해서 막는다(web_leader.reset_to_parking)."""
        import mujoco

        with self._lock:
            for n, v in denormalize_all({k: v for k, v in arm_norm.items() if k in JOINT_CALIBRATION}).items():
                if n in ARM_JOINTS:
                    self.data.qpos[self.jm.qadr[n]] = v / MILLIDEG_PER_RAD
            for f in FINGERS:
                self.data.qpos[self.jm.qadr[f]] = 0.0        # 파킹은 그리퍼 닫힘(0)
            if cube_x is not None and cube_y is not None:      # 팔만 리셋(T)이면 큐브는 그대로
                cube_adr = self.model.jnt_qposadr[mujoco.mj_name2id(
                    self.model, mujoco.mjtObj.mjOBJ_JOINT, "cube_free")]
                self.data.qpos[cube_adr:cube_adr + 7] = [cube_x, cube_y, 0.02, 1, 0, 0, 0]
            self.data.qvel[:] = 0
            self.jm.write_ctrl(self.data, arm_norm)          # 서보 목표도 파킹
            self._goal = dict(arm_norm)
            self._hold = False
            mujoco.mj_forward(self.model, self.data)

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
