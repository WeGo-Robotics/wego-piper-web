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
import math
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


def _chain_from(root, base: str, tip: str | None) -> tuple[list, str]:
    """`base` 에서 말단까지 관절을 **위상 순서로** 잇는다.

    ⚠ **문서 순서를 믿으면 안 된다.** SO-101 URDF 는 관절을 말단부터 적어 두었다 —
      그대로 읽으면 사슬이 거꾸로 서고 FK 가 그럴듯하게 틀린다.
    """
    joints = list(root.findall("joint"))
    by_parent: dict[str, list] = {}
    for j in joints:
        by_parent.setdefault(j.find("parent").get("link"), []).append(j)

    def walk(link: str, acc: list) -> list | None:
        if tip is not None and link == tip:
            return acc
        kids = by_parent.get(link, [])
        if not kids:
            return acc if tip is None else None
        for j in kids:
            got = walk(j.find("child").get("link"), acc + [j])
            if got is not None:
                return got
        return None

    path = walk(base, [])
    if path is None:
        raise SystemExit(f"{base} 에서 {tip} 로 가는 사슬을 못 찾았습니다")
    end = path[-1].find("child").get("link") if path else base
    return path, (tip or end)


def _mesh_path(link, urdf_dir: Path, meshes_dir: Path) -> tuple[Path | None, dict]:
    """링크의 충돌 메시 파일과 그 **origin**.

    ⚠ 충돌 origin 을 무시하면 안 된다. Piper 는 전부 0 이라 그동안 문제가 없었는데,
      SO-101 은 링크마다 다르다 — 무시하면 메시가 엉뚱한 자리에 놓여 바닥 판정이
      통째로 틀린다.
    """
    coll = link.find("collision")
    if coll is None:
        return None, {}
    mesh = coll.find("geometry/mesh")
    if mesh is None:
        return None, {}
    name = (mesh.get("filename") or "").replace("package://", "")
    cand = [urdf_dir / name, urdf_dir.parent / name, meshes_dir / Path(name).name]
    o = coll.find("origin")
    pose = {"xyz": _triple(o.get("xyz") if o is not None else None),
            "rpy": _triple(o.get("rpy") if o is not None else None)}
    for c in cand:
        if c.is_file():
            return c, pose
    return None, pose


def _rot(rpy) -> np.ndarray:
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ])


def build(cell: float, urdf: Path | None = None, chain: tuple[str, ...] | None = None,
          base: str = "base_link", tip: str | None = None) -> dict:
    """URDF + 메시 → 지오메트리 dict.

    ⚠ **관절 한계와 말단 링크 이름을 같이 굽는다.** IK 가 둘 다 쓴다
      (`armmodel.ArmModel`). 말단을 `link6` 으로 하드코딩하면 Piper 밖에 못 쓴다.
    """
    urdf_path = urdf or ARM_URDF
    urdf_dir = urdf_path.parent
    meshes_dir = urdf_dir.parent / "meshes" if urdf is None else urdf_dir / "assets"
    root = ET.parse(urdf_path).getroot()
    is_piper = urdf is None or urdf_path == ARM_URDF

    if chain is not None:
        by_name = {j.get("name"): j for j in root.findall("joint")}
        path = [by_name[n] for n in chain]
        end_link = path[-1].find("child").get("link")
    elif is_piper:
        by_name = {j.get("name"): j for j in root.findall("joint")}
        path = [by_name[n] for n in ARM_JOINTS]
        end_link = "link6"
    else:
        path, end_link = _chain_from(root, base, tip)
        path = [j for j in path if j.get("type") in ("revolute", "continuous", "fixed")]

    links = {L.get("name"): L for L in root.findall("link")}

    names: list[str] = [base]
    parent: list[int] = [-1]
    xyz: list[tuple] = [(0.0, 0.0, 0.0)]
    rpy: list[tuple] = [(0.0, 0.0, 0.0)]
    axis: list[tuple] = [(0.0, 0.0, 0.0)]
    qidx: list[int] = [-1]
    limits: list[tuple[float, float]] = []

    n_moving = 0
    for j in path:
        o = j.find("origin")
        names.append(j.find("child").get("link"))
        parent.append(len(names) - 2)
        xyz.append(_triple(o.get("xyz") if o is not None else None))
        rpy.append(_triple(o.get("rpy") if o is not None else None))
        moving = j.get("type") in ("revolute", "continuous")
        a = j.find("axis")
        axis.append(_triple(a.get("xyz") if a is not None else None, (0.0, 0.0, 1.0))
                    if moving else (0.0, 0.0, 0.0))
        qidx.append(n_moving if moving else -1)
        if moving:
            n_moving += 1
            lm = j.find("limit")
            limits.append((float(lm.get("lower")), float(lm.get("upper")))
                          if lm is not None and lm.get("lower") is not None
                          else (-math.pi, math.pi))

    if is_piper:
        for name, par, t, r, mesh in GRIPPER:
            names.append(name); parent.append(names.index(par))
            xyz.append(t); rpy.append(r); axis.append((0.0, 0.0, 0.0)); qidx.append(-1)

    pts: list[np.ndarray] = []
    pt_link: list[np.ndarray] = []
    missing: list[str] = []
    for k, name in enumerate(names):
        if is_piper:
            stem = {"flange_link": "flange", "gripper_base": "gripper_base",
                    "gripper_link1": "gripper_link1", "gripper_link2": "gripper_link2"
                    }.get(name, name)
            path_m, pose = MESHES / f"{stem}.stl", {}
            if not path_m.is_file():
                path_m = None
        else:
            path_m, pose = _mesh_path(links.get(name), urdf_dir, meshes_dir) \
                if links.get(name) is not None else (None, {})
        if path_m is None:
            missing.append(name)
            continue
        v = load_stl(path_m)
        if pose:
            v = v @ _rot(pose["rpy"]).T + np.array(pose["xyz"])
        if name in ("gripper_link1", "gripper_link2"):
            v = np.concatenate([v + np.array([0.0, 0.0, s])
                                for s in np.linspace(0.0, FINGER_TRAVEL, FINGER_SAMPLES)])
        c = cover(v, cell)
        pts.append(c)
        pt_link.append(np.full(len(c), k, dtype=np.int32))

    if missing:
        print(f"  ⚠ 메시 없는 링크(바닥 검사에서 빠짐): {', '.join(missing)}")

    return {
        "names": np.array(names),
        "parent": np.array(parent, dtype=np.int32),
        "xyz": np.array(xyz, dtype=np.float64),
        "rpy": np.array(rpy, dtype=np.float64),
        "axis": np.array(axis, dtype=np.float64),
        "qidx": np.array(qidx, dtype=np.int32),
        "pts": np.concatenate(pts) if pts else np.zeros((0, 3), np.float32),
        "pt_link": np.concatenate(pt_link) if pt_link else np.zeros(0, np.int32),
        "radius": np.array(cell * np.sqrt(3) / 2),
        "cell": np.array(cell),
        "limits": np.array(limits, dtype=np.float64),
        "tip": np.array(end_link),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", type=float, default=0.01, help="복셀 한 변 (m)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--urdf", type=Path, default=None,
                    help="다른 팔의 URDF (SO-101 등). 생략하면 Piper")
    ap.add_argument("--joints", default=None,
                    help="사슬 관절 이름을 쉼표로. 생략하면 base→tip 위상 순서")
    ap.add_argument("--base", default="base_link", help="뿌리 링크")
    ap.add_argument("--tip", default=None, help="말단 링크. 생략하면 사슬 끝")
    args = ap.parse_args()

    if not ARM_URDF.is_file():
        raise SystemExit(f"URDF 가 없습니다: {ARM_URDF}\n"
                         "  git submodule update --init vendor/agx_arm_urdf")
    data = build(args.cell, args.urdf,
                 tuple(args.joints.split(",")) if args.joints else None,
                 args.base, args.tip)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **data)

    out = args.out.resolve()
    label = out.relative_to(REPO) if out.is_relative_to(REPO) else out
    print(f"{label}  ({out.stat().st_size / 1024:.0f}KB)")
    print(f"  링크 {len(data['names'])}개, 자유도 {int((data['qidx'] >= 0).sum())}, "
          f"점 {len(data['pts'])}개, 반지름 {float(data['radius']) * 100:.2f}cm")
    print(f"  말단 링크: {data['tip']}")
    for i, n in enumerate(data["names"]):
        k = int((data["pt_link"] == i).sum())
        print(f"    {n:14s} {k:5d}점")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
