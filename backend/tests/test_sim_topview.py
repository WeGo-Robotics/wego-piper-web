"""탑뷰 카메라 높이 — **z 만** 사람이 조절한다 (feature/sim-topview-height.md).

여기서 지키는 것: 높이가 실제로 먹는가, 그게 **가상환경 교체가 아닌가**(재컴파일·
렌더러 재생성 없음), 기본값이 바탕 XML 과 갈리지 않는가, 범위는 거절이 아니라 클램프인가.
"""

import re
from pathlib import Path

import pytest

mujoco = pytest.importorskip("mujoco")
piper_sim = pytest.importorskip("piper_sim")

REPO = Path(__file__).resolve().parents[2]


def test_the_default_height_matches_the_base_scene_xml():
    """⚠ 손으로 적은 값이 **둘**이다 — 상수와 XML. 갈리면 "화면엔 0.60 인데 세계는
    0.75" 가 되고, 아무 에러도 안 난다. 여기서 XML 을 파싱해 대조한다."""
    from piper_sim import scene_spec

    xml = (REPO / "sim" / "piper_sim" / "assets" / "piper_scene.xml").read_text()
    m = re.search(r'<camera\s+name="top"\s+pos="([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"', xml)
    assert m, "바탕 XML 에서 top 카메라를 못 찾았다 — 이름이나 속성 순서가 바뀌었나"
    assert float(m.group(3)) == pytest.approx(scene_spec.TOP_Z_DEFAULT), \
        "상수와 XML 의 기본 높이가 갈렸다"
    lo, hi = scene_spec.CAMERA_Z_RANGE
    assert lo < scene_spec.TOP_Z_DEFAULT < hi, "기본값이 조절 범위 밖이다"


def test_the_range_clamps_instead_of_refusing():
    """사람이 슬라이더를 끝까지 끄는 것은 잘못이 아니다 — 끝이 어디인지는
    **돌려주는 값**이 말한다. 거절로 만들면 화면이 그 말을 옮겨야 한다."""
    from piper_sim import scene_spec

    lo, hi = scene_spec.CAMERA_Z_RANGE
    assert scene_spec.clamp_camera_z(0.0) == lo
    assert scene_spec.clamp_camera_z(99.0) == hi
    assert scene_spec.clamp_camera_z(0.72) == pytest.approx(0.72)


def test_setting_the_height_moves_the_camera_without_rebuilding_the_world():
    """⚠ **이것이 이 기능이 싼 이유다.** 모델 필드 하나라 재컴파일도 `MjData`
    재생성도 렌더러 재생성도 없다 — 그래서 EGL 컨텍스트 문제와 무관하고, 슬라이더를
    끄는 동안 세계가 안 끊긴다. 누가 이걸 `load_scene` 위에 다시 얹으면 여기서 실패한다.
    """
    from piper_sim.world import World

    w = World()
    model, data = w.model, w.data
    fired: list[str] = []
    w.on_model_change.append(lambda: fired.append("rebuilt"))

    assert w.camera_z() == pytest.approx(0.60), "기본 세계의 탑뷰가 0.60 이 아니다"
    assert w.set_camera_z(0.86) == pytest.approx(0.86)

    cid = mujoco.mj_name2id(w.model, mujoco.mjtObj.mjOBJ_CAMERA, "top")
    assert float(w.model.cam_pos[cid][2]) == pytest.approx(0.86)
    # 클릭→배치가 읽는 것은 data 쪽이다 — 스텝을 안 기다리고 바로 맞아야 한다
    assert float(w.data.cam_xpos[cid][2]) == pytest.approx(0.86)
    assert w.model is model and w.data is data, "모델·데이터를 다시 만들었다"
    assert not fired, "렌더러를 버리게 만들었다 — 가상환경 교체가 아니다"


def test_the_height_is_the_zoom_and_the_click_math_follows_it():
    """fovy 가 고정이라 z 가 곧 줌이다. 그리고 **클릭 배치가 따라온다** — 화면에서
    같은 픽셀이 높이에 따라 다른 자리를 가리켜야 맞다(카메라 자세를 그때그때 읽는다).

    실측 근거: 0.6m 에서 보이는 y 반폭이 0.312m (테이블 ±0.45 의 69%), 0.86m 에서
    0.448m 로 테이블이 다 들어온다 — 그래서 지금 높이에서는 **클릭으로 못 놓는 자리**가 있다.
    """
    from piper_sim.world import World

    w = World()
    edge = 0.02            # 화면 위쪽 가장자리 근처

    w.set_camera_z(0.60)
    near = w.ray_to_table("top", 0.5, edge, 640 / 480)
    w.set_camera_z(0.86)
    far = w.ray_to_table("top", 0.5, edge, 640 / 480)

    assert near is not None and far is not None
    # 같은 픽셀인데 높이가 올라가면 **더 먼 자리**를 가리킨다 = 더 넓게 담긴다
    assert far[0] > near[0] + 0.05, (near, far)
    # 낮을 때는 테이블 끝(x 0.9)에 클릭으로 닿지 못한다 — 이 기능이 푸는 문제다
    assert near[0] < 0.9, "0.6m 에서 이미 테이블 끝까지 보인다면 표가 틀린 것이다"


def test_the_daemon_exposes_the_height_verbs():
    """데몬이 안 노출하면 게이트웨이가 못 부른다 — so101 `set_flipped` 과 같은 자리."""
    src = (REPO / "daemons" / "simd.py").read_text()
    methods = src.split("_METHODS = {", 1)[1].split("}", 1)[0]
    for verb in ("camera_z", "set_camera_z"):
        assert f'"{verb}"' in methods, f"simd 가 {verb} 를 안 노출한다"
    hub = (REPO / "sim" / "piper_sim" / "hub.py").read_text()
    assert "def set_camera_z" in hub and "_world().set_camera_z" in hub, \
        "허브가 `self.world` 를 직접 만지면 세계가 없을 때 죽는다"
