"""씬 로드와 관절 규약 — MuJoCo 관절각 ↔ Piper 정규화 (feature/sim-env.md §2·§6).

정규화는 **`piper_robot.joints` 그대로** 쓴다. 시뮬 관절각(rad) → 밀리도 →
`normalize_all`. 표가 하나여야 실기에서 학습한 정책이 시뮬에서 같은 숫자를 본다.
그리퍼는 손가락 슬라이드 둘(각 0..34mm) = 0..68000µm 캘리브레이션과 같은 뜻.
"""

import math
from pathlib import Path

import numpy as np

from piper_robot.joints import JOINT_CALIBRATION, denormalize_all, normalize_all

ASSETS = Path(__file__).resolve().parent / "assets"
SCENE_XML = ASSETS / "piper_scene.xml"

ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")
FINGERS = ("gripper_l", "gripper_r")
#: 손가락 한쪽 스트로크(m). 둘 합쳐 68mm = gripper 캘리브레이션 상한 68000µm.
FINGER_STROKE_M = 0.034
MILLIDEG_PER_RAD = 180000.0 / math.pi


def load_model():
    import mujoco

    if not SCENE_XML.exists():
        raise FileNotFoundError(
            f"씬이 없습니다: {SCENE_XML} — python3 tools/build_sim_scene.py 로 구우세요")
    return mujoco.MjModel.from_xml_path(str(SCENE_XML))


class JointMap:
    """모델의 관절·액추에이터 id 를 이름으로 잡아 둔다 — 루프에서 이름 조회를 안 한다."""

    def __init__(self, model) -> None:
        import mujoco

        self.qadr = {n: model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n)]
                     for n in ARM_JOINTS + FINGERS}
        self.act = {n: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, n)
                    for n in ARM_JOINTS + FINGERS}
        self.link6 = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "link6")
        self.cube = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "cube")

    def read_norm(self, data) -> dict[str, float]:
        """qpos → 정규화 dict (joint1..6 + gripper). 발행 레코드 그대로."""
        raw = {n: float(data.qpos[self.qadr[n]]) * MILLIDEG_PER_RAD for n in ARM_JOINTS}
        # 그리퍼 개도 = 두 손가락 합 (m) → µm
        opening_m = float(data.qpos[self.qadr["gripper_l"]] + data.qpos[self.qadr["gripper_r"]])
        raw["gripper"] = opening_m * 1e6
        return normalize_all(raw)

    def write_ctrl(self, data, norm_goal: dict[str, float]) -> None:
        """정규화 목표 → 액추에이터 ctrl (rad / m). 빠진 관절은 건드리지 않는다."""
        raw = denormalize_all({k: v for k, v in norm_goal.items() if k in JOINT_CALIBRATION})
        for n in ARM_JOINTS:
            if n in raw:
                data.ctrl[self.act[n]] = raw[n] / MILLIDEG_PER_RAD
        if "gripper" in raw:
            half = max(0.0, min(FINGER_STROKE_M, raw["gripper"] * 1e-6 / 2.0))
            data.ctrl[self.act["gripper_l"]] = half
            data.ctrl[self.act["gripper_r"]] = half

    def hold(self, data) -> None:
        """지금 자세를 목표로 — 데드맨·E-stop 의 "그 자리에 서기"."""
        for n in ARM_JOINTS + FINGERS:
            data.ctrl[self.act[n]] = float(data.qpos[self.qadr[n]])

    def q_rad(self, data) -> np.ndarray:
        return np.array([data.qpos[self.qadr[n]] for n in ARM_JOINTS], dtype=float)
