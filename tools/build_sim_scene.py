#!/usr/bin/env python3
"""Piper 시뮬 씬(MJCF)을 굽는다 — feature/sim-env.md 1단계.

`tools/build_arm_geometry.py` 와 같은 발상: URDF 는 빌드 때만 읽고, 런타임은
구운 산출물(`sim/piper_sim/assets/piper_scene.xml`)만 연다.

    python3 tools/build_sim_scene.py            # vendor/agx_arm_urdf 서브모듈에서

하는 일:
1. 공식 URDF 를 MuJoCo 로 열어 MJCF 로 덤프한다 (`discardvisual` — DAE 시각
   메시는 MuJoCo 가 못 읽으므로 STL 충돌 메시를 시각으로 쓴다).
2. 덤프에서 메시 자산과 팔 본체 트리(link1..6)를 떼어 씬 템플릿에 심는다:
   테이블·큐브·통·조명·카메라(top/wrist)·**그리퍼**(공식 URDF 엔 없다 —
   평행 슬라이드 손가락 둘, 스트로크 68mm = 실기 gripper 캘리브레이션
   0..68000µm 과 같은 뜻)·위치 액추에이터.
3. meshdir 은 **상대 경로**로 바꾼다 — 절대 경로가 박히면 다른 기계에서 안 열린다.
"""

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
URDF = REPO / "vendor" / "agx_arm_urdf" / "piper" / "urdf" / "piper_description.urdf"
MESHES = REPO / "vendor" / "agx_arm_urdf" / "piper" / "meshes"
OUT = REPO / "sim" / "piper_sim" / "assets" / "piper_scene.xml"

#: 그리퍼 한쪽 손가락 스트로크 (m). 둘이 합쳐 68mm — piper 캘리브레이션 0..68000.
FINGER_STROKE_M = 0.034

SCENE_TEMPLATE = """<mujoco model="piper_scene">
  <compiler angle="radian" meshdir="{meshdir}" balanceinertia="true"/>
  <option timestep="0.002" gravity="0 0 -9.81"/>
  <visual>
    <global offwidth="1280" offheight="960"/>
    <headlight ambient="0.4 0.4 0.4" diffuse="0.6 0.6 0.6"/>
  </visual>
  <asset>
    <texture type="skybox" builtin="gradient" rgb1="0.6 0.7 0.85" rgb2="0.2 0.25 0.35" width="256" height="256"/>
    <texture name="tabletex" type="2d" builtin="checker" rgb1="0.82 0.8 0.76" rgb2="0.7 0.68 0.64" width="64" height="64"/>
    <material name="tablemat" texture="tabletex" texrepeat="6 6" reflectance="0.05"/>
    <material name="armmat" rgba="0.85 0.85 0.87 1"/>
    <material name="fingermat" rgba="0.25 0.25 0.28 1"/>
    <material name="cubemat" rgba="0.85 0.2 0.15 1"/>
    <material name="binmat" rgba="0.2 0.35 0.7 1"/>
  </asset>
  <worldbody>
    <light name="sun" pos="0.5 -0.5 1.5" dir="-0.3 0.3 -1" directional="true" diffuse="0.8 0.8 0.8"/>
    <light name="fill" pos="-0.5 0.5 1.2" dir="0.3 -0.3 -1" diffuse="0.4 0.4 0.4"/>
    <geom name="table" type="box" size="0.6 0.5 0.02" pos="0.35 0 -0.02" material="tablemat"/>
    <camera name="top" pos="0.35 0 0.9" quat="1 0 0 0" fovy="55"/>
    <camera name="front" pos="1.1 0 0.45" xyaxes="0 1 0 -0.4 0 1" fovy="50"/>
    <body name="piper_base" pos="0 0 0">
    </body>
    <body name="cube" pos="0.35 0.0 0.02">
      <freejoint name="cube_free"/>
      <geom name="cube_geom" type="box" size="0.02 0.02 0.02" mass="0.05" material="cubemat"/>
    </body>
    <body name="bin" pos="0.35 -0.25 0">
      <geom type="box" size="0.08 0.08 0.003" pos="0 0 0.003" material="binmat"/>
      <geom type="box" size="0.003 0.08 0.03" pos="0.08 0 0.03" material="binmat"/>
      <geom type="box" size="0.003 0.08 0.03" pos="-0.08 0 0.03" material="binmat"/>
      <geom type="box" size="0.08 0.003 0.03" pos="0 0.08 0.03" material="binmat"/>
      <geom type="box" size="0.08 0.003 0.03" pos="0 -0.08 0.03" material="binmat"/>
    </body>
  </worldbody>
  <contact>
    <!-- ⚠ base_link(정적 → MuJoCo 가 world 로 합친다)와 link1 메시가 6mm 겹친다
         (실측). world↔link1 접촉은 부모-자식 제외가 안 걸려 마찰로 joint1 이
         잠겼다 — 모델 아티팩트라 이 쌍만 뺀다. 팔은 여전히 테이블·물체와 부딪친다. -->
    <exclude body1="piper_base" body2="link1"/>
  </contact>
  <actuator>
  </actuator>
</mujoco>
"""

GRIPPER_XML = """<body name="gripper_base" pos="0 0 0.035">
  <geom type="box" size="0.03 0.015 0.012" material="fingermat" mass="0.05"/>
  <!-- 손목 카메라 — MuJoCo 카메라는 -z 를 본다. 손가락 축(+z)을 보되 25° 손가락
       쪽으로 기울이고, **이미지 위 = -x(툴)**. 근거는 툴 프레임의 세계 방향(실측,
       q=0): 툴 z = 세계 +x(앞), 툴 y = 세계 y = **관절 2·3·5 의 공통 피치 축**,
       툴 x = 세계 -z(아래). 이미지 위를 y 로 잡으면(첫 시도) 세계 아래가 이미지
       오른쪽(내적 -1.00)이라 테이블이 오른쪽 벽처럼 선다 — 사용자 스크린샷 그대로.
       -x 는 q=0 에서 이미지위·세계위 = +1.00 이고, 피치 축이 y 라 어떤 피치 자세
       (파킹·앞·아래)에서도 수직면에 남는다 → 하늘 위·바닥 아래. 카메라는 위쪽
       (-x) 으로 **8cm**·뒤로 2cm. ⚠ 4.5cm 로 뒀더니(첫 시도의 y 축 값을 옮김) 베이스
       상자의 x 반폭이 3cm 라 상자 면이 렌즈 1.5cm 앞에 와 시야의 60% 를 막았다
       (사용자 스크린샷). 오프셋×기울기 스윕 실측: 8cm/25° 는 파킹에서 상자 4%,
       앞·아래에서 테이블 86%·손가락 끝 4% 가 아래 가장자리(행 0.90) 띠로만 남는다.
       ⚠ 첫 시도의 "바닥 65%·손가락 아래" 실측은 기울기·비율만 본 것이라 롤 오류를
       못 잡았다 — 롤은 이미지위·세계위 내적으로 잰다(테스트). -->
  <camera name="wrist" pos="-0.08 0 -0.02" xyaxes="0 -1 0 -0.906 0 0.423" fovy="70"/>
  <body name="finger_l" pos="0 0.008 0.03">
    <joint name="gripper_l" type="slide" axis="0 1 0" range="0 {s}" damping="2"/>
    <geom type="box" size="0.008 0.004 0.025" pos="0 0.004 0" material="fingermat" mass="0.02" friction="1.5 0.02 0.001"/>
  </body>
  <body name="finger_r" pos="0 -0.008 0.03">
    <joint name="gripper_r" type="slide" axis="0 -1 0" range="0 {s}" damping="2"/>
    <geom type="box" size="0.008 0.004 0.025" pos="0 -0.004 0" material="fingermat" mass="0.02" friction="1.5 0.02 0.001"/>
  </body>
</body>
"""

# 위치 서보 강성. 링크에 gravcomp=1 을 주므로 kp 는 중력을 이기는 값이 아니라
# **추종 강성**이다 — 실기 Piper 는 서보가 자세를 딱 잡는다. 감쇠는 진동 방지.
ACTUATORS = [("joint1", 400), ("joint2", 400), ("joint3", 300), ("joint4", 150),
             ("joint5", 150), ("joint6", 80)]
JOINT_DAMPING = {"joint1": 8, "joint2": 8, "joint3": 6, "joint4": 3, "joint5": 3, "joint6": 2}


def dump_arm(urdf: Path, meshdir: Path) -> ET.Element:
    """URDF → MuJoCo 덤프 MJCF (ElementTree 루트)."""
    import mujoco

    txt = urdf.read_text()
    txt = re.sub(r"package://[^/]+/agx_arm_urdf/piper/meshes/", "", txt)
    txt = re.sub(r"(<robot[^>]*>)",
                 rf'\1\n  <mujoco><compiler meshdir="{meshdir}" discardvisual="true" '
                 r'balanceinertia="true"/></mujoco>', txt, count=1)
    tmp = urdf.parent / ".piper_mujoco.urdf"
    tmp.write_text(txt)
    try:
        m = mujoco.MjModel.from_xml_path(str(tmp))
        out = tmp.with_suffix(".xml")
        mujoco.mj_saveLastXML(str(out), m)
        root = ET.parse(out).getroot()
        out.unlink()
    finally:
        tmp.unlink(missing_ok=True)
    return root


def compose(arm: ET.Element, meshdir_rel: str) -> ET.Element:
    scene = ET.fromstring(SCENE_TEMPLATE.format(meshdir=meshdir_rel))
    # 메시 자산
    asset = scene.find("asset")
    for mesh in arm.find("asset").findall("mesh"):
        asset.append(mesh)
    # 팔 본체: 덤프의 worldbody 직속 geom(base_link) + body(link1 트리)
    base = scene.find(".//body[@name='piper_base']")
    wb = arm.find("worldbody")
    for child in list(wb):
        if child.tag in ("geom", "body"):
            base.append(child)
    for g in base.iter("geom"):
        if g.get("type") == "mesh":
            g.set("material", "armmat")
    # 중력보상 — 위치 서보가 중력에 처지지 않게 (실기 서보의 자세 유지에 해당).
    # 감쇠는 관절마다 명시 (덤프된 joint 요소에 속성이 없다).
    for body in base.iter("body"):
        body.set("gravcomp", "1")
    for j in base.iter("joint"):
        if j.get("name") in JOINT_DAMPING:
            j.set("damping", str(JOINT_DAMPING[j.get("name")]))
    # 그리퍼를 link6 아래에
    link6 = scene.find(".//body[@name='link6']")
    assert link6 is not None, "link6 를 못 찾았다 — URDF 가 바뀌었나"
    link6.append(ET.fromstring(GRIPPER_XML.format(s=FINGER_STROKE_M)))
    # 액추에이터 — 위치 서보. kp 는 팔 크기에 맞춘 출발값 (실측 튜닝 대상)
    act = scene.find("actuator")
    for name, kp in ACTUATORS:
        ET.SubElement(act, "position", name=name, joint=name, kp=str(kp),
                      ctrlrange=" ".join(scene.find(f".//joint[@name='{name}']").get("range").split()))
    for f in ("gripper_l", "gripper_r"):
        ET.SubElement(act, "position", name=f, joint=f, kp="200",
                      ctrlrange=f"0 {FINGER_STROKE_M}")
    return scene


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urdf", type=Path, default=URDF)
    ap.add_argument("--meshes", type=Path, default=MESHES)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    if not a.urdf.exists():
        raise SystemExit(f"URDF 가 없습니다: {a.urdf} — `git submodule update --init vendor/agx_arm_urdf`")
    arm = dump_arm(a.urdf, a.meshes)
    import os
    rel = os.path.relpath(a.meshes, a.out.parent)
    scene = compose(arm, rel)
    ET.indent(scene, space="  ")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(ET.tostring(scene, encoding="unicode") + "\n")
    # 산출물이 실제로 열리는지 여기서 본다 — 안 열리는 씬을 커밋하지 않는다
    import mujoco
    m = mujoco.MjModel.from_xml_path(str(a.out))
    print(f"wrote {a.out} — nq={m.nq} nu={m.nu} nbody={m.nbody} ncam={m.ncam} meshdir={rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
