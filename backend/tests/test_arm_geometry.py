"""구운 지오메트리가 URDF·메시와 여전히 같은가.

`robot/piper_robot/data/arm_geometry.npz` 는 `tools/build_arm_geometry.py` 가
URDF 와 STL 에서 만든다. 런타임에 서브모듈을 안 읽는 대신(robotd 는 호스트에
가볍게 배포된다) **드리프트를 여기서 잡는다.**

서브모듈이 없는 체크아웃에서는 건너뛴다 — 그 경우 구운 파일이 유일한 진실이다.
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("piper_robot")
from piper_robot import kinematics as K  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BUILDER = REPO / "tools" / "build_arm_geometry.py"
URDF = REPO / "vendor" / "agx_arm_urdf" / "piper" / "urdf" / "piper_description.urdf"


def _builder():
    if not URDF.is_file():
        pytest.skip("URDF 서브모듈이 없다 (git submodule update --init)")
    spec = importlib.util.spec_from_file_location("build_arm_geometry", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_arm_geometry"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_baked_chain_matches_the_urdf():
    """관절 원점·축이 URDF 그대로인가. 여기가 어긋나면 FK 가 조용히 틀린다."""
    g = K.geometry()
    fresh = _builder().build(float(np.load(K.DATA)["cell"]))
    assert tuple(str(n) for n in fresh["names"]) == g.names
    for key in ("xyz", "rpy", "axis"):
        assert np.allclose(fresh[key], getattr(g, key)), key
    assert np.array_equal(fresh["parent"], g.parent)
    assert np.array_equal(fresh["qidx"], g.qidx)


def test_the_baked_points_match_the_meshes():
    """메시가 바뀌었는데 다시 안 구우면 여기서 걸린다."""
    g = K.geometry()
    fresh = _builder().build(float(np.load(K.DATA)["cell"]))
    assert np.array_equal(fresh["pt_link"], g.pt_link)
    assert np.allclose(fresh["pts"], g.pts)


def test_the_radius_covers_the_cell():
    """각 점은 복셀 중심이다 — 반지름이 대각선 절반보다 작으면 **덮개가 아니다**
    (실제 표면이 구 밖으로 나오고, 그러면 실제보다 위를 보게 된다)."""
    with np.load(K.DATA) as z:
        assert float(z["radius"]) == pytest.approx(float(z["cell"]) * np.sqrt(3) / 2)


def test_every_moving_link_has_points():
    """점이 없는 링크는 **검사에서 통째로 빠진다** — 아무 에러 없이."""
    g = K.geometry()
    root = int(np.flatnonzero(g.parent < 0)[0])
    for i, name in enumerate(g.names):
        if i == root:
            continue
        assert (g.pt_link == i).any(), f"{name} 에 점이 없다"


def test_the_data_file_is_declared_as_package_data():
    """⚠ `.npz` 는 코드가 아니라 데이터다. 선언 안 하면 **비편집 설치에서 빠지고**,
    빠지면 바닥 필터가 조용히 꺼진다. robotd 호스트 배포가 그 경로를 탄다."""
    toml = (REPO / "robot" / "pyproject.toml").read_text()
    assert "[tool.setuptools.package-data]" in toml
    assert "data/*.npz" in toml
    assert K.DATA.parent.name == "data"
