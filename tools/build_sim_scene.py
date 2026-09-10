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
3. 쓰인 메시를 **산출물 옆(`assets/meshes/`)으로 복사**하고 meshdir 을 `meshes`
   로 둔다.

⚠ **예전엔 서브모듈을 상대경로로 가리켰다** (`../../../vendor/agx_arm_urdf/...`).
   그 경로는 저장소를 통째로 체크아웃해 `sim/` 이 루트 바로 아래 있을 때만 맞는다 —
   wheel 로 설치하면 `site-packages/piper_sim/assets/` 에서 세 단계 위라 아무 데도
   안 닿는다. 실기(.120, v0.4.6)에서 simd 가 `link6.stl` 을 못 찾아 월드가 안 뜨고,
   팔도 카메라도 등록되지 않았다 — 같은 원인, 두 증상.

   절대경로로 굽는 것도 답이 아니다(다른 기계에서 안 열린다). 패키지 안에 넣으면
   개발 체크아웃과 설치본이 **같은 상대경로**를 쓴다.
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
  <!-- ⚠ impratio: 기본 1 이면 잡은 물체가 슬슬 미끄러진다(실측 3.7mm/s, 6초에 20mm →
       빠짐, 사용자 보고 2026). MuJoCo 는 잡기에서 impratio 를 올리라고 권한다 — 마찰
       제약을 법선력 대비 단단히 푼다. 50 에서 0.1mm/s(사실상 정지), 흔들어도 견딘다. -->
  <option timestep="0.002" gravity="0 0 -9.81" impratio="50"/>
  <visual>
    <global offwidth="1280" offheight="960"/>
    <headlight ambient="0.4 0.4 0.4" diffuse="0.6 0.6 0.6"/>
  </visual>
  <asset>
    <texture type="skybox" builtin="gradient" rgb1="0.6 0.7 0.85" rgb2="0.2 0.25 0.35" width="256" height="256"/>
    <!-- 바닥 격자 — 대비를 키우고(밝은 회색↔진한 청회색) 반사를 없앤다. 전엔 두 색이
         거의 같고(0.82 vs 0.70) specular 기본 0.5 라 조명 반사에 격자가 씻겨 희미했다
         (사용자 보고 2026). width 큰 텍스처로 경계도 또렷하게. -->
    <texture name="tabletex" type="2d" builtin="checker" rgb1="0.9 0.9 0.93" rgb2="0.32 0.36 0.44" width="512" height="512"/>
    <material name="tablemat" texture="tabletex" texrepeat="12 10" texuniform="true" reflectance="0" specular="0.05" shininess="0.1"/>
    <material name="armmat" rgba="0.85 0.85 0.87 1"/>
    <material name="fingermat" rgba="0.25 0.25 0.28 1"/>
    <material name="cubemat" rgba="0.85 0.2 0.15 1"/>
    <material name="binmat" rgba="0.2 0.35 0.7 1"/>
  </asset>
  <worldbody>
    <light name="sun" pos="0.5 -0.5 1.5" dir="-0.3 0.3 -1" directional="true" diffuse="0.8 0.8 0.8"/>
    <light name="fill" pos="-0.5 0.5 1.2" dir="0.3 -0.3 -1" diffuse="0.4 0.4 0.4"/>
    <geom name="table" type="box" size="0.6 0.5 0.02" pos="0.35 0 -0.02" material="tablemat"/>
    <!-- 탑뷰 — 광학축(−z) 기준 반시계 90°: 이미지 위 = +x(앞, 팔이 뻗는 쪽),
         오른쪽 = −y. EE 마우스 면(위=앞, 오른쪽=−y)과 정확히 맞아 조종이 직관적이다
         (사용자 보고 2026: 마우스 면과 탑뷰가 90° 어긋나 조종이 힘들다). -->
    <camera name="top" pos="0.35 0 0.9" xyaxes="0 -1 0 1 0 0" fovy="55"/>
    <camera name="front" pos="1.1 0 0.45" xyaxes="0 1 0 -0.4 0 1" fovy="50"/>
    <body name="piper_base" pos="0 0 0">
    </body>
    <body name="cube" pos="0.35 0.0 0.02">
      <freejoint name="cube_free"/>
      <!-- 잡히는 물체 — condim6 은 비틀림·구름 마찰(핀치에서 돌아 빠지는 것 방지),
           solref 단단하게(접촉 크리프 감소). 손가락과 같은 마찰. -->
      <geom name="cube_geom" type="box" size="0.02 0.02 0.02" mass="0.05" material="cubemat"
            condim="6" friction="1.5 0.05 0.001" solref="0.005 1"/>
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
  <!-- 손목 카메라 롤 — 시선은 그대로 손가락 축(25° 기울임). 광학축 기준 시계 90°
         (사용자 보고 2026): 이미지 위 = 그리퍼 x-z 평면(위·뒤), 오른쪽 = 그리퍼 −y.
         탑뷰는 마우스 면에 맞춰 두고 손목만 이 방향이 손 느낌과 맞았다. -->
  <camera name="wrist" pos="-0.08 0 -0.02" xyaxes="0 -1 0 -0.906 0 0.423" fovy="70"/>
  <body name="finger_l" pos="0 0.008 0.03">
    <joint name="gripper_l" type="slide" axis="0 1 0" range="0 {s}" damping="2"/>
    <geom type="box" size="0.008 0.004 0.025" pos="0 0.004 0" material="fingermat" mass="0.02" friction="1.5 0.05 0.001" condim="6" solref="0.005 1"/>
  </body>
  <body name="finger_r" pos="0 -0.008 0.03">
    <joint name="gripper_r" type="slide" axis="0 -1 0" range="0 {s}" damping="2"/>
    <geom type="box" size="0.008 0.004 0.025" pos="0 -0.004 0" material="fingermat" mass="0.02" friction="1.5 0.05 0.001" condim="6" solref="0.005 1"/>
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


def copy_meshes(arm: ET.Element, src: Path, dst: Path) -> list[Path]:
    """씬이 참조하는 메시만 산출물 옆으로 복사한다. 복사한 파일 목록.

    ⚠ **쓰지 않는 것은 안 담는다.** 서브모듈 전체가 203MB 인데 wheel 에 다 넣으면
    시뮬을 안 쓰는 호스트도 그걸 받는다.
    """
    import shutil

    names = sorted({e.get("file") for e in arm.iter("mesh") if e.get("file")})
    dst.mkdir(parents=True, exist_ok=True)
    # 안 쓰게 된 것은 지운다 — 남겨 두면 다음 사람이 왜 있는지 모른다
    for stale in dst.glob("*.stl"):
        if stale.name not in names:
            stale.unlink()
    out = []
    for n in names:
        s = src / n
        if not s.is_file():
            raise SystemExit(f"메시가 없습니다: {s}")
        shutil.copy2(s, dst / n)
        out.append(dst / n)
    return out


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
    # ⚠ 쓰이는 메시만 옆에 복사한다. 서브모듈 전체는 203MB, `piper/meshes` 만도
    #   28MB 인데 씬이 참조하는 것은 STL 일곱 개(7.9MB)뿐이다.
    rel = "meshes"
    used = copy_meshes(arm, a.meshes, a.out.parent / rel)
    scene = compose(arm, rel)
    ET.indent(scene, space="  ")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(ET.tostring(scene, encoding="unicode") + "\n")
    # 산출물이 실제로 열리는지 여기서 본다 — 안 열리는 씬을 커밋하지 않는다
    import mujoco
    m = mujoco.MjModel.from_xml_path(str(a.out))
    print(f"wrote {a.out} — nq={m.nq} nu={m.nu} nbody={m.nbody} ncam={m.ncam} "
          f"meshdir={rel} ({len(used)} 개, {sum(f.stat().st_size for f in used)/1e6:.1f}MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
