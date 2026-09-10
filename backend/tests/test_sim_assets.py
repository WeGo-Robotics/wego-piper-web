"""시뮬 씬 자산 — **설치본에서도 열리는가** (feature/sim-env.md).

⚠ 실기에서 이렇게 났다(.120, v0.4.6). 씬 XML 이 메시를
`meshdir="../../../vendor/agx_arm_urdf/piper/meshes"` 로 가리켰는데, 그 경로는
저장소를 통째로 체크아웃해 `sim/` 이 루트 바로 아래 있을 때만 맞는다. wheel 로
설치하면 `site-packages/piper_sim/assets/` 에서 세 단계 위라 아무 데도 안 닿는다.

`link6.stl` 을 못 찾아 MuJoCo 월드가 안 뜨고 `attach()` 가 매번 예외를 던졌다 —
**팔도 카메라도 등록되지 않았다.** 시뮬 팔과 시뮬 카메라가 한 월드를 공유하므로
원인 하나에 증상이 둘이었다.

절대경로로 굽는 것도 답이 아니다(다른 기계에서 안 열린다). 패키지 안에 넣어
개발 체크아웃과 설치본이 **같은 상대경로**를 쓰게 한다.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "sim" / "piper_sim" / "assets"
SCENE = ASSETS / "piper_scene.xml"


def _scene() -> str:
    if not SCENE.is_file():
        pytest.skip("씬이 아직 안 구워졌다")
    return SCENE.read_text()


def test_the_meshdir_stays_inside_the_package():
    """⚠ 패키지 밖으로 나가는 순간 설치본에서 깨진다 — 저장소 레이아웃을 전제하니까."""
    m = re.search(r'meshdir="([^"]*)"', _scene())
    assert m, "meshdir 이 없다"
    meshdir = m.group(1)
    assert not meshdir.startswith("/"), f"절대경로가 박혔다: {meshdir}"
    assert ".." not in meshdir, (
        f"패키지 밖을 가리킨다: {meshdir} — wheel 로 설치하면 안 닿는다"
    )


def test_every_referenced_mesh_ships():
    """씬이 부르는 파일이 실제로 옆에 있어야 한다. 하나만 없어도 월드가 안 뜬다."""
    scene = _scene()
    meshdir = re.search(r'meshdir="([^"]*)"', scene).group(1)
    names = sorted(set(re.findall(r'file="([^"]+\.(?:stl|STL|obj))"', scene)))
    assert names, "씬이 메시를 하나도 안 부른다 — 정말인가?"
    for n in names:
        assert (ASSETS / meshdir / n).is_file(), f"{n} 이 없다 (meshdir={meshdir})"


def test_the_wheel_is_told_to_carry_them():
    """⚠ 파일이 있어도 `package-data` 에 없으면 wheel 에 안 담긴다 — 저장소에서는
    되고 배포에서만 깨진다. 그게 정확히 .120 에서 난 일이다."""
    cfg = tomllib.loads((ROOT / "sim" / "pyproject.toml").read_text())
    globs = cfg["tool"]["setuptools"]["package-data"]["piper_sim"]
    assert any(g.endswith(".stl") for g in globs), \
        f"메시가 package-data 에 없다: {globs}"


def test_the_redistributed_meshes_are_credited():
    """⚠ MIT 는 재배포를 허락하되 **고지를 유지**하라고 한다. wheel 로 남의 자산을
    실어 보내면서 안 적으면 라이선스 위반이다."""
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text()
    assert "agx_arm_urdf" in notices, "재배포하는 메시가 고지에 없다"


def test_the_scene_actually_loads():
    """구운 씬이 열리는가. 위 셋이 다 맞아도 XML 이 깨졌으면 소용없다."""
    mujoco = pytest.importorskip("mujoco")
    m = mujoco.MjModel.from_xml_path(str(SCENE))
    assert m.nbody > 1 and m.ncam >= 1
