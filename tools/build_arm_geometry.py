#!/usr/bin/env python3
"""URDF + STL → `piper_robot/data/arm_geometry.npz` (refactor/robotd-safety.md).

## 왜 미리 굽나

robotd 는 **호스트에 가볍게** 배포된다. 런타임에 XML 을 파싱하고 STL 을 읽게 하면
`vendor/agx_arm_urdf` 서브모듈이 배포 대상에도 있어야 하는데, 배포 절차에 그런 단계가
없다. 구운 파일은 30KB 남짓이라 패키지에 넣는 편이 싸다.

**드리프트는 테스트가 잡는다** — `test_arm_geometry.py` 가 서브모듈이 있을 때
이 스크립트를 다시 돌려 결과를 대조한다.

## 링크를 무엇으로 근사하나

메시 단위 충돌은 안 한다(문서: "바닥면 방지에는 과하다"). 대신 정점을 **복셀로 뭉쳐
구 덮개**로 만든다: 격자 한 칸의 중심 + 반지름 `cell·√3/2`.

캡슐이나 바운딩 박스가 아니라 이걸 고른 이유는 **오차 상한이 자세와 무관**하기
때문이다. 실측(300개 무작위 자세):

    바운딩 박스   평균 0.4~1.7cm, **최악 4.1cm**   ← 자세에 따라 출렁인다
    1cm 복셀      평균 0.9~1.2cm, 상한 1.7cm       ← 방향과 무관하게 갇혀 있다

안전 필터의 여유값(clearance)은 이 오차를 포함해서 정해야 하는데, 상한이 있어야
그 계산이 가능하다. 그리고 **덮개는 항상 실제보다 아래를 본다** — 틀리는 방향이
안전한 쪽으로 고정돼 있다.

사용:  python3 tools/build_arm_geometry.py [--cell 0.01]
"""

from __future__ import annotations

import argparse
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
URDF_DIR = REPO / "vendor" / "agx_arm_urdf" / "piper"
ARM_URDF = URDF_DIR / "urdf" / "piper_description.urdf"
MESHES = URDF_DIR / "meshes"
OUT = REPO / "robot" / "piper_robot" / "data" / "arm_geometry.npz"

# 팔 사슬. `observation.state` 의 앞 6축과 같은 순서다.
ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")

# 그리퍼는 별도 xacro 라 여기 손으로 적는다 — xacro 를 풀려면 ROS 가 필요하고,
# 값이 넷뿐이며 바뀌지 않는다. (원본: piper_with_gripper_description.xacro)
# ⚠ **바닥에 먼저 닿는 것이 그리퍼다.** link6 플랜지만 검사하면 13cm 를 놓친다.
GRIPPER = [
    # (링크명, 부모, xyz, rpy, 메시)
    ("flange_link",   "link6",        (0.0, 0.0, 0.0),    (0, 0, 0), "flange.stl"),
    ("gripper_base",  "flange_link",  (0.0, 0.0, 0.0045), (0, 0, 0), "gripper_base.stl"),
    ("gripper_link1", "gripper_base", (0.0, 0.0, 0.138),  (1.5707963, 0, 0), "gripper_link1.stl"),
    ("gripper_link2", "gripper_base", (0.0, 0.0, 0.138),  (1.5707963, 0, -3.1415926), "gripper_link2.stl"),
]
# 손가락 행정. 열고 닫는 방향이라 공구축과 직교하지만, 덮개는 **열린 채로도
# 닫힌 채로도** 맞아야 하므로 행정 전체를 훑어 합집합을 만든다.
FINGER_TRAVEL = 0.05
FINGER_SAMPLES = 3


def load_stl(path: Path) -> np.ndarray:
    """STL 정점. 바이너리·ASCII 둘 다 읽는다."""
    b = path.read_bytes()
    if b[:5].lower() == b"solid" and b"facet" in b[:2000]:
        pts = [tuple(map(float, ln.split()[1:4]))
               for ln in b.decode(errors="ignore").splitlines()
               if ln.strip().startswith("vertex")]
        return np.array(pts, dtype=float)
    n = struct.unpack("<I", b[80:84])[0]
    raw = np.frombuffer(b, dtype=np.uint8, count=n * 50, offset=84).reshape(n, 50)
    return raw[:, 12:48].copy().view(np.float32).reshape(-1, 3).astype(float)


def cover(v: np.ndarray, cell: float) -> np.ndarray:
    """정점을 복셀 중심으로 뭉친다. 결과의 각 점은 반지름 `cell·√3/2` 를 갖는다."""
    key = np.floor(v / cell).astype(np.int64)
    uniq = np.unique(key, axis=0)
    return ((uniq + 0.5) * cell).astype(np.float32)


def _triple(s: str | None, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if not s:
        return default
    x, y, z = (float(t) for t in s.split())
    return (x, y, z)


def build(cell: float) -> dict:
    root = ET.parse(ARM_URDF).getroot()
    joints = {j.get("name"): j for j in root.findall("joint")}

    names: list[str] = ["base_link"]
    parent: list[int] = [-1]
    xyz: list[tuple] = [(0.0, 0.0, 0.0)]
    rpy: list[tuple] = [(0.0, 0.0, 0.0)]
    axis: list[tuple] = [(0.0, 0.0, 0.0)]
    qidx: list[int] = [-1]
    meshes: list[str | None] = ["base_link.stl"]

    for i, jn in enumerate(ARM_JOINTS):
        j = joints[jn]
        o = j.find("origin")
        names.append(j.find("child").get("link"))
        parent.append(len(names) - 2)
        xyz.append(_triple(o.get("xyz") if o is not None else None))
        rpy.append(_triple(o.get("rpy") if o is not None else None))
        a = j.find("axis")
        axis.append(_triple(a.get("xyz") if a is not None else None, (0.0, 0.0, 1.0)))
        qidx.append(i)
        meshes.append(f"{names[-1]}.stl")

    for name, par, t, r, mesh in GRIPPER:
        names.append(name)
        parent.append(names.index(par))
        xyz.append(t)
        rpy.append(r)
        axis.append((0.0, 0.0, 0.0))
        qidx.append(-1)
        meshes.append(mesh)

    pts: list[np.ndarray] = []
    pt_link: list[np.ndarray] = []
    for k, mesh in enumerate(meshes):
        if mesh is None:
            continue
        p = MESHES / mesh
        if not p.is_file():
            raise SystemExit(f"메시가 없습니다: {p}")
        v = load_stl(p)
        if names[k] in ("gripper_link1", "gripper_link2"):
            # 손가락은 행정 전체를 훑는다 — 그 축은 링크 로컬 z 다
            v = np.concatenate([
                v + np.array([0.0, 0.0, s])
                for s in np.linspace(0.0, FINGER_TRAVEL, FINGER_SAMPLES)
            ])
        c = cover(v, cell)
        pts.append(c)
        pt_link.append(np.full(len(c), k, dtype=np.int32))

    return {
        "names": np.array(names),
        "parent": np.array(parent, dtype=np.int32),
        "xyz": np.array(xyz, dtype=np.float64),
        "rpy": np.array(rpy, dtype=np.float64),
        "axis": np.array(axis, dtype=np.float64),
        "qidx": np.array(qidx, dtype=np.int32),
        "pts": np.concatenate(pts),
        "pt_link": np.concatenate(pt_link),
        "radius": np.array(cell * np.sqrt(3) / 2),
        "cell": np.array(cell),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", type=float, default=0.01, help="복셀 한 변 (m)")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    if not ARM_URDF.is_file():
        raise SystemExit(f"URDF 가 없습니다: {ARM_URDF}\n"
                         "  git submodule update --init vendor/agx_arm_urdf")
    data = build(args.cell)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **data)

    print(f"{args.out.relative_to(REPO)}  ({args.out.stat().st_size / 1024:.0f}KB)")
    print(f"  링크 {len(data['names'])}개, 점 {len(data['pts'])}개, "
          f"반지름 {float(data['radius']) * 100:.2f}cm")
    for i, n in enumerate(data["names"]):
        k = int((data["pt_link"] == i).sum())
        print(f"    {n:14s} {k:5d}점")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
