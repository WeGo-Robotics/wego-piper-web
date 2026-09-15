"""시뮬 파지 — 비스듬히 잡아도 나르는 동안 놓치지 않는다 (사용자 보고 2026-09-14).

"약간만 비스듬하게 잡아도 떨어진다." 큐브가 돌아간 건 문제가 아니었다(40° 도 잡힌다) —
**손이 기울어진 채**(피치 10°) 잡고 나르면 놓쳤다. 실험(떠 있는 그리퍼, 아래 `_build`)으로
설정을 하나씩 바꿔 본 결과:

| 손잡이 | 효과 |
|---|---|
| `cone="elliptic"` | impratio 가 비로소 뜻을 가진다(pyramidal 에선 무시) → 손 10° 를 잡는다 |
| 그리퍼 kp 200→600 | 20°·얕은 파지·기울기+회전까지 나르며 잡는다. 800 은 튕겨 나간다 |
| 손가락 댐핑 2→5 | kp 에 맞춘 감쇠비(0.3→0.7) — 빈손 닫기에 튀지 않게 |
| 마찰 1.5→2.0 | **차이 없음** (사용자 요청으로 올려 둠) |
| 접촉 부드럽게(solref 0.01) / 더 단단히(solimp) | 둘 다 **나빠짐** |

여기서는 실제 장면 XML 의 <option>·손가락·큐브 문자열을 그대로 떼어 떠 있는 그리퍼에
붙이고, 기울어진 손으로 내려가 닫고 들어 흔든다. 접촉 물리만 본다(팔 IK 는 무관).
"""

import math
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENE = REPO / "sim" / "piper_sim" / "assets" / "piper_scene.xml"
BUILDER = REPO / "tools" / "build_sim_scene.py"


def test_the_friction_cone_is_elliptic_so_impratio_means_something():
    """⚠ impratio=50 을 올려 둔 뒤에도 pyramidal 콘이라 무시되고 있었다. 두 파일이 같아야 한다."""
    for path in (SCENE, BUILDER):
        opt = re.search(r"<option[^>]*/>", path.read_text()).group(0)
        assert 'cone="elliptic"' in opt, f"{path.name}: pyramidal 콘 — impratio 가 무시된다"
        assert 'impratio="50"' in opt and 'noslip_iterations="10"' in opt
    xml = SCENE.read_text()
    assert xml.count('kp="600"') == 2, "장면: 그리퍼 kp 가 600 이 아니다 — 10° 기울면 놓친다"
    assert 'kp="600"' in BUILDER.read_text(), "빌더: 그리퍼 kp 가 600 이 아니다 (장면과 갈린다)"
    for path in (SCENE, BUILDER):
        src = path.read_text()
        assert src.count('friction="2.0 0.1 0.001"') == 3 and 'friction="1.5 0.05 0.001"' not in src, \
            f"{path.name}: 손가락 둘·큐브의 마찰이 같지 않다"
        assert src.count('damping="5"') == 2, f"{path.name}: 손가락 댐핑이 kp 에 안 맞는다"


def _build(pitch_deg: float):
    """떠 있는 그리퍼 — 캐리어(세계 슬라이드 xyz + yaw) 아래에 피치만큼 기울어진 손.
    손가락·큐브 geom 과 <option> 은 장면 XML 문자열 그대로."""
    import numpy as np
    import mujoco

    scene = SCENE.read_text()
    opt = re.search(r"<option[^>]*/>", scene).group(0)
    fl = re.search(r'<geom type="box" size="0.008 0.004 0.025" pos="0 0.004 0"[^>]*/>', scene).group(0)
    fr = re.search(r'<geom type="box" size="0.008 0.004 0.025" pos="0 -0.004 0"[^>]*/>', scene).group(0)
    cube = re.search(r'<geom name="cube_geom"[^>]*/>', scene).group(0)
    kp = re.search(r'<position name="gripper_l"[^>]*kp="(\d+)"', scene).group(1)
    damp = re.search(r'<joint name="gripper_l"[^>]*damping="([\d.]+)"', scene).group(1)

    def q_axis(axis, deg):
        q = np.zeros(4); mujoco.mju_axisAngle2Quat(q, np.array(axis, float), math.radians(deg)); return q
    def q_mul(a, b):
        r = np.zeros(4); mujoco.mju_mulQuat(r, a, b); return r
    hq = q_mul(q_axis([1, 0, 0], 180), q_axis([0, 1, 0], pitch_deg))   # 손가락이 아래, 피치만큼 기울임
    hq_s = " ".join(f"{v:.6f}" for v in hq)
    xml = f"""
<mujoco model="floating_gripper">
  {opt}
  <asset><material name="fingermat" rgba="0.25 0.25 0.28 1"/><material name="cubemat" rgba="0.85 0.2 0.15 1"/></asset>
  <worldbody>
    <geom name="table" type="box" size="0.6 0.5 0.02" pos="0 0 -0.02"/>
    <body name="carrier" pos="0 0 0.15">
      <joint name="hx" type="slide" axis="1 0 0" damping="20"/>
      <joint name="hz" type="slide" axis="0 0 1" damping="20"/>
      <joint name="hyaw" type="hinge" axis="0 0 1" damping="2"/>
      <geom type="sphere" size="0.005" mass="0.3"/>
      <body name="hand" quat="{hq_s}">
        <geom type="box" size="0.03 0.015 0.012" material="fingermat" mass="0.05"/>
        <body name="finger_l" pos="0 0.008 0.03">
          <joint name="gripper_l" type="slide" axis="0 1 0" range="0 0.034" damping="{damp}"/>
          {fl}
        </body>
        <body name="finger_r" pos="0 -0.008 0.03">
          <joint name="gripper_r" type="slide" axis="0 -1 0" range="0 0.034" damping="{damp}"/>
          {fr}
        </body>
      </body>
    </body>
    <body name="cube" pos="0 0 0.02"><freejoint name="cube_free"/>{cube}</body>
  </worldbody>
  <actuator>
    <position name="hx" joint="hx" kp="1500"/><position name="hz" joint="hz" kp="1500"/><position name="hyaw" joint="hyaw" kp="50"/>
    <position name="gripper_l" joint="gripper_l" kp="{kp}" ctrlrange="0 0.034"/>
    <position name="gripper_r" joint="gripper_r" kp="{kp}" ctrlrange="0 0.034"/>
  </actuator>
</mujoco>"""
    return mujoco.MjModel.from_xml_string(xml)


def _grasp_and_carry(model, yaw_deg: float, dz: float) -> bool:
    """내려가(0.5초) → 닫고(0.8초) → 1초에 들어 올려 → 2.5초 나르며 흔든다(yaw ±0.5rad 1Hz, x ±2cm 2Hz).
    끝에 큐브가 손과 같이 떠 있고 손가락 사이에 있으면 잡은 것."""
    import numpy as np
    import mujoco

    data = mujoco.MjData(model)
    a = {n: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, n) for n in ("hx", "hz", "hyaw", "gripper_l", "gripper_r")}
    adr = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "cube_free")]
    data.qpos[adr:adr + 3] = [0, 0, 0.02]
    data.qpos[adr + 3:adr + 7] = [math.cos(math.radians(yaw_deg / 2)), 0, 0, math.sin(math.radians(yaw_deg / 2))]
    for f in ("gripper_l", "gripper_r"):
        data.qpos[model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, f)]] = 0.034
        data.ctrl[a[f]] = 0.034
    cube_b = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "cube")
    hand = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "hand")
    dt = model.opt.timestep
    z_down, z_up = -0.09 + dz, 0.05      # 손 원점 세계 z = 0.06 + dz (손가락 끝이 테이블 5mm 위 + dz)
    for i in range(int(4.9 / dt)):
        t = i * dt
        if t < 0.6:
            hz, g, yw, hx = z_down * min(t / 0.5, 1.0), 0.034, 0.0, 0.0
        elif t < 1.4:
            hz, g, yw, hx = z_down, 0.0, 0.0, 0.0
        elif t < 2.4:
            hz, g, yw, hx = z_down + (z_up - z_down) * (t - 1.4), 0.0, 0.0, 0.0
        else:
            s = t - 2.4
            hz, g, yw, hx = z_up, 0.0, 0.5 * math.sin(2 * math.pi * s), 0.02 * math.sin(4 * math.pi * s)
        data.ctrl[a["hz"]] = hz; data.ctrl[a["hyaw"]] = yw; data.ctrl[a["hx"]] = hx
        data.ctrl[a["gripper_l"]] = data.ctrl[a["gripper_r"]] = g
        mujoco.mj_step(model, data)
    R = data.xmat[hand].reshape(3, 3)
    rel = R.T @ (data.xpos[cube_b] - data.xpos[hand])
    return bool(data.xpos[cube_b][2] > 0.08 and abs(rel[0]) < 0.03 and abs(rel[1]) < 0.03)


@pytest.mark.parametrize("label, pitch, yaw, dz", [
    ("손 10° 기울임", 10, 0, 0.010),          # 옛 설정이 놓치던 것 — 사용자 증상
    ("손 20° 기울임", 20, 0, 0.015),
    ("얕게 + 15°", 15, 0, 0.025),
    ("15° + 큐브 20° 회전", 15, 20, 0.015),
])
def test_a_tilted_grasp_survives_carrying(label, pitch, yaw, dz):
    pytest.importorskip("mujoco")
    assert _grasp_and_carry(_build(pitch), yaw, dz), f"{label}: 나르는 동안 큐브를 놓쳤다"
