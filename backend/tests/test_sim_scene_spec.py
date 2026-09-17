"""장면 명세 — 테이블 위 사물을 JSON 으로 정의한다 (feature/sim-scene-editor.md).

큐브와 통은 `piper_scene.xml` 안에 **박혀** 있었다. 그래서 사람이 사물을 바꾸려면
릴리스를 내야 했고, 릴리스를 건너뛴 호스트는 영영 옛 세계를 봤다 — .120 이 게이트웨이
v0.5.4 에 wheel 0.4.7 로 돌며 옛 바닥·옛 탑뷰를 렌더한 그 사건(2026-09-16)이 같은 뿌리다.

이제 바탕 XML 은 **팔·테이블·카메라·조명**만 담고, 사물은 `assets/default_scene.json` →
`scene_spec.compose` → `MjSpec` 으로 얹힌다.

**이 파일의 첫째 임무는 "이사해도 세계가 그대로다"를 수치로 잡아 두는 것이다.** 파지는
2026-09-14 에 실험으로 맞춘 접촉값(elliptic 콘·condim 6·friction 2.0·solref 0.005)에
걸려 있어서, 옮기다 한 자리만 틀려도 "비스듬하면 떨어진다"가 되돌아온다.
"""

import json
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENE_XML = REPO / "sim" / "piper_sim" / "assets" / "piper_scene.xml"
DEFAULT_JSON = REPO / "sim" / "piper_sim" / "assets" / "default_scene.json"
BUILDER = REPO / "tools" / "build_sim_scene.py"

mujoco = pytest.importorskip("mujoco")


def _model(spec=None):
    from piper_sim.scene import load_model

    return load_model(spec)


def _body(m, name):
    return mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, name)


def _geoms(m, bid):
    return list(range(m.body_geomadr[bid], m.body_geomadr[bid] + m.body_geomnum[bid]))


def test_the_default_scene_reproduces_the_world_the_xml_used_to_carry():
    """이사 전 모델에서 **직접 읽어 적은 수치**다. 하나라도 어긋나면 이사가 세계를 바꾼 것이다.

    큐브: body (0.35, 0, 0.02)·질량 0.05·자유관절 하나, geom 은 반변 2cm 상자에 condim 6,
    friction (2.0, 0.1, 0.001), solref (0.005, 1) — 전부 비스듬한 파지 실험의 결론이다.
    통: body (0.35, −0.25, 0)·관절 없음(고정), geom 다섯(바닥 + 벽 넷)에 MuJoCo 기본 접촉.
    """
    m = _model()
    assert (m.nq, m.nbody, m.ncam) == (15, 13, 3), "관절·바디·카메라 수가 옛 씬과 다르다"

    cube = _body(m, "cube")
    assert cube > 0, "기본 장면에 큐브가 없다"
    assert list(m.body_pos[cube]) == [0.35, 0.0, 0.02]
    assert m.body_mass[cube] == pytest.approx(0.05)
    assert m.body_jntnum[cube] == 1, "큐브가 자유관절을 잃었다 — 못 움직인다"
    assert mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "cube_free") >= 0, \
        "자유관절 이름이 `cube_free` 가 아니다 — world.reset 이 qpos 를 못 찾는다"
    (g,) = _geoms(m, cube)
    assert mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g) == "cube_geom"
    assert m.geom_type[g] == mujoco.mjtGeom.mjGEOM_BOX
    assert list(m.geom_size[g]) == [0.02, 0.02, 0.02]
    assert m.geom_condim[g] == 6, "비틀림·구름 마찰이 빠졌다 — 핀치에서 돌아 빠진다"
    assert list(m.geom_friction[g]) == pytest.approx([2.0, 0.1, 0.001])
    assert list(m.geom_solref[g]) == pytest.approx([0.005, 1.0])
    assert list(m.geom_rgba[g])[:3] == pytest.approx([0.85, 0.2, 0.15]), "큐브 색이 바뀌었다"

    bin_ = _body(m, "bin")
    assert list(m.body_pos[bin_]) == [0.35, -0.25, 0.0]
    assert m.body_jntnum[bin_] == 0, "통이 떠다닌다 — 고정물이어야 한다"
    sizes = sorted(tuple(round(float(x), 4) for x in m.geom_size[g]) for g in _geoms(m, bin_))
    assert sizes == sorted([(0.08, 0.08, 0.003),          # 바닥
                            (0.003, 0.08, 0.03), (0.003, 0.08, 0.03),   # ±x 벽
                            (0.08, 0.003, 0.03), (0.08, 0.003, 0.03)]), "통의 조각이 달라졌다"
    for g in _geoms(m, bin_):
        assert m.geom_condim[g] == 3 and list(m.geom_friction[g]) == pytest.approx([1.0, 0.005, 1e-4]), \
            "고정물이 MuJoCo 기본 접촉을 안 쓴다"


def test_the_base_scene_no_longer_carries_the_objects_people_edit():
    """바탕 XML 에 사물을 도로 적으면 **사람이 그걸 못 지운다** — wheel 안이라 릴리스가 필요하고,
    릴리스를 건너뛴 호스트는 옛 세계에 갇힌다. 빌더와 산출물 양쪽을 본다(둘이 갈리면 다음
    `build_sim_scene.py` 실행이 조용히 되돌린다)."""
    for path in (SCENE_XML, BUILDER):
        src = path.read_text()
        assert '<body name="cube"' not in src and '<body name="bin"' not in src, \
            f"{path.name}: 편집 대상 물체가 바탕에 도로 들어왔다"
        assert "cubemat" not in src and "binmat" not in src, \
            f"{path.name}: 물체 재질이 바탕에 남았다 — 색은 장면 JSON 이 쥔다"
    # 바탕이 여전히 들고 있어야 하는 것 — 이게 빠지면 팔도 카메라도 없다
    src = SCENE_XML.read_text()
    for keep in ('name="table"', 'name="top"', 'name="front"', 'name="wrist"', 'name="piper_base"'):
        assert keep in src, f"바탕에서 {keep} 가 사라졌다"


def test_the_wheel_ships_the_default_scene_or_the_table_comes_up_empty():
    """⚠ 기본 장면은 **패키지 데이터**다. `assets/*.json` 이 빠지면 설치본에서 파일이 없어
    시뮬이 빈 테이블로 뜬다 — 메시를 안 담아 월드가 통째로 안 뜬 v0.4.6 사건과 같은 종류다."""
    import tomllib

    data = tomllib.loads((REPO / "sim" / "pyproject.toml").read_text())
    globs = data["tool"]["setuptools"]["package-data"]["piper_sim"]
    assert "assets/*.json" in globs, "기본 장면 JSON 이 wheel 에 안 실린다"
    assert json.loads(DEFAULT_JSON.read_text())["objects"], "기본 장면이 비어 있다"


def test_a_scene_cannot_take_a_name_the_arm_or_the_cameras_already_use():
    """물체 id 가 `link3` 나 `top` 이면 모델이 깨진다. MuJoCo 도 거절하지만 그 오류는
    사람이 못 읽는다("Error: repeated name") — 무엇이 문제인지 우리가 말한다."""
    from piper_sim import scene_spec as S

    for bad in ("link3", "top", "table", "joint1", "gripper_l", "piper_base"):
        with pytest.raises(S.SceneError, match="팔·테이블·카메라"):
            S.validate({"objects": [{"id": bad, "shape": "box", "size": [.02, .02, .02]}]})
    # 이름 계약은 굽고 나서도 살아 있어야 한다
    m = _model({"objects": [{"id": "thing", "shape": "sphere", "size": [0.02], "pos": [0.3, 0.2, 0.05]}]})
    for j in ("joint1", "joint6", "gripper_l", "gripper_r"):
        assert mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, j) >= 0, f"{j} 가 사라졌다"
    assert [mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_CAMERA, i) for i in range(m.ncam)] == \
        ["top", "front", "wrist"], "카메라 이름·순서가 바뀌면 등록된 시뮬 카메라가 어긋난다"


@pytest.mark.parametrize("bad, msg", [
    ({"objects": [{"id": "A", "shape": "box", "size": [.02, .02, .02]}]}, "소문자"),
    ({"objects": [{"id": "x", "shape": "blob", "size": [.02]}]}, "모르는 shape"),
    ({"objects": [{"id": "x", "shape": "box", "size": [.02, .02]}]}, "숫자 3 개"),
    ({"objects": [{"id": "x", "shape": "box", "size": [.02, .02, 99]}]}, "범위"),
    ({"objects": [{"id": "x", "shape": "sphere", "size": [.02]},
                  {"id": "x", "shape": "sphere", "size": [.02]}]}, "둘입니다"),
    ({"objects": [{"id": "x", "shape": "preset:bowl"}]}, "모르는 프리셋"),
    ({"objects": [{"id": "x", "shape": "preset:bin", "params": {"nope": 1}}]}, "없는 항목"),
    ({"version": 99, "objects": []}, "모르는 장면 버전"),
    ({"objects": [{"id": "x", "shape": "box", "size": [.02, .02, .02], "condim": 5}]}, "condim"),
    ({"objects": [{"id": "x", "shape": "box", "size": [.02, .02, .02], "mass": 999}]}, "mass"),
])
def test_a_broken_scene_says_what_to_fix_instead_of_letting_mujoco_crash(bad, msg):
    """사람이 쓰는 파일이다 — 틀렸을 때 **어디가 왜** 틀렸는지 말해야 고친다.
    검사를 안 하면 오류가 MuJoCo 컴파일러에서 나고, 그건 물체 id 를 말해 주지 않는다."""
    from piper_sim import scene_spec as S

    with pytest.raises(S.SceneError, match=msg):
        S.validate(bad)


def test_a_concave_preset_is_built_from_convex_parts_because_meshes_collide_as_hulls():
    """⚠ **MuJoCo 는 메시를 볼록껍질로 충돌시킨다.** V 자 골짜기 메시에 공을 떨어뜨리면
    골짜기 바닥(0.010)이 아니라 껍질 위(실측 0.1096)에 선다 — 그릇·통을 메시 하나로 올리면
    겉만 오목하고 물리는 덩어리다. 그래서 오목한 것은 **볼록 조각**으로 짓는다.

    사람이 툴 없이 오목한 것을 얻는 유일한 길이므로 프리셋이 실제로 조각을 내는지 잡아 둔다.
    """
    from piper_sim import scene_spec as S

    m = _model({"objects": [{"id": "tray", "shape": "preset:bin", "movable": False,
                             "pos": [0.35, 0.2, 0], "params": {"wall_h": 0.02}}]})
    tray = _body(m, "tray")
    gs = _geoms(m, tray)
    assert len(gs) == 5, "프리셋이 조각을 안 냈다 — 오목함이 물리에 안 산다"
    assert all(m.geom_type[g] == mujoco.mjtGeom.mjGEOM_BOX for g in gs), "조각은 전부 볼록해야 한다"
    assert max(float(m.geom_pos[g][2]) for g in gs) == pytest.approx(0.02), "wall_h 가 안 먹었다"

    # 그리고 실제로 담긴다 — 공을 벽 안에 떨어뜨리면 바닥에 앉는다(껍질 위가 아니라)
    m2 = _model({"objects": [
        {"id": "tray", "shape": "preset:bin", "movable": False, "pos": [0.35, 0.2, 0]},
        {"id": "ball", "shape": "sphere", "size": [0.012], "pos": [0.35, 0.2, 0.12], "mass": 0.02}]})
    d = mujoco.MjData(m2)
    for _ in range(1500):
        mujoco.mj_step(m2, d)
    z = float(d.xpos[_body(m2, "ball")][2])
    assert z < 0.03, f"공이 통 안으로 안 들어갔다 (z={z:.4f}) — 오목함이 물리에 없다"


def test_objects_people_add_land_on_the_table_and_leave_room_in_the_realtime_budget():
    """물체는 얹으면 **테이블에 앉아야** 한다(허공·바닥밑이면 배치 UI 가 거짓말한다).
    그리고 실시간 500Hz 를 지켜야 하므로 예산도 같이 잰다 — 물체 수 상한의 근거다."""
    from piper_sim import scene_spec as S

    spec = {"objects": [
        {"id": "green", "shape": "box", "size": [0.03, 0.02, 0.02], "pos": [0.30, 0.12, 0.02],
         "rgba": [0.1, 0.6, 0.2, 1], "mass": 0.05},
        {"id": "can", "shape": "cylinder", "size": [0.025, 0.05], "pos": [0.40, 0.10, 0.05],
         "rgba": [0.9, 0.8, 0.1, 1], "mass": 0.08},
        {"id": "ball", "shape": "sphere", "size": [0.025], "pos": [0.30, -0.10, 0.025], "mass": 0.03},
    ]}
    m = _model(spec)
    d = mujoco.MjData(m)
    t0 = time.perf_counter()
    for _ in range(500):             # 물리 1초 (timestep 2ms)
        mujoco.mj_step(m, d)
    budget = time.perf_counter() - t0
    for oid, want in (("green", 0.02), ("can", 0.05), ("ball", 0.025)):
        z = float(d.xpos[_body(m, oid)][2])
        assert z == pytest.approx(want, abs=0.004), f"{oid} 가 테이블에 안 앉았다 (z={z:.4f})"
    assert budget < 0.3, f"물체 셋에 물리 1초가 {budget:.3f}s — 실시간 예산 1초를 위협한다"
    assert S.MAX_OBJECTS >= len(spec["objects"])


def test_a_scene_file_loads_from_disk_and_bad_json_names_the_line():
    """"환경 불러오기" 가 타는 길 — 파일에서 읽는다. JSON 이 깨졌으면 **몇 번째 줄**인지
    말해야 고친다(편집기에서 쉼표 하나 빠뜨리는 것이 제일 흔하다)."""
    from piper_sim import scene_spec as S

    loaded = S.load(DEFAULT_JSON)
    assert [o["id"] for o in loaded["objects"]] == ["cube", "bin"]
    assert loaded["objects"][0]["label"] == "빨간 블럭", "라벨이 사라졌다 — 화면이 id 를 보여 준다"

    bad = Path(__import__("tempfile").mkdtemp()) / "broken.json"
    bad.write_text('{\n  "objects": [\n    {"id": "x",}\n  ]\n}')
    with pytest.raises(S.SceneError, match="3번째 줄"):
        S.load(bad)
    with pytest.raises(S.SceneError, match="없습니다"):
        S.load(bad.parent / "nope.json")


def test_the_validated_scene_is_what_both_the_screen_and_the_daemon_read():
    """기본값을 채운 **한 사본**을 돌려준다. 화면과 데몬이 각자 기본값을 정하면 "화면엔
    이렇게 보이는데 시뮬은 저렇게 돈다"가 된다 — 고치기 제일 어려운 종류의 버그다."""
    from piper_sim import scene_spec as S

    v = S.validate({"objects": [{"id": "x", "shape": "box", "size": [.02, .02, .02]}]})
    o = v["objects"][0]
    assert o["movable"] is True and o["mass"] == 0.05, "기본이 움직이는 물체가 아니다"
    assert o["friction"] == S.MOVABLE_PHYSICS["friction"] and o["condim"] == 6
    assert o["label"] == "x" and o["euler_deg"] == [0, 0, 0] and o["pos"] == [0, 0, 0]
    static = S.validate({"objects": [{"id": "w", "shape": "box", "size": [.1, .01, .05],
                                      "movable": False}]})["objects"][0]
    assert "mass" not in static, "고정물에 질량을 매겼다 — 뜻이 없다"
    assert static["condim"] == 3, "고정물이 파지용 접촉을 쓴다"

    summary = S.describe({"objects": [{"id": "x", "shape": "box", "size": [.02, .02, .02]}]})
    assert summary["count"] == 1 and summary["objects"][0]["shape"] == "box", \
        "화면이 굽지 않고도 목록을 못 읽는다"
