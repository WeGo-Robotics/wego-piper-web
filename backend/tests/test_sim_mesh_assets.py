"""메시 자산 — 사람이 스캔하거나 만든 물건을 테이블에 올린다
(feature/sim-scene-editor.md §2·§4 · 3단계).

이 파일이 지키는 것 넷:

① **받는 포맷은 셋뿐이다** (OBJ · 바이너리 STL · PNG). ASCII STL 과 GLB 는 흔한 내보내기
   기본값이라 반드시 오는데, 거절만 하면 "왜 안 되는지 모르겠다"가 된다 — 무엇으로 바꿔
   오면 되는지까지 말한다.
② **단위는 사람이 안다.** OBJ·STL 에 단위가 없어 mm 모델은 1000배로 선다. 추측은 하되
   잰 크기를 보여 주고 확인받는다.
③ **오목함을 잰다.** ⚠ MuJoCo 는 메시를 볼록껍질로 충돌시킨다 — 그릇·컵은 겉만 오목하고
   물리는 덩어리다. 얼마나 파였는지 말해 준다.
④ **게이트웨이와 데몬이 같은 디렉토리를 본다.** 가상환경은 dict 로 넘기지만 메시는 파일이라
   양쪽이 각자 자기 루트에서 같은 id 를 푼다 — 그 둘이 갈리면 "올렸는데 시뮬에 없다"가 된다.
"""

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("piper_sim")

REPO = Path(__file__).resolve().parents[2]


# ── 시험용 메시 ────────────────────────────────────────────────────────────

def box_obj(sx=20.0, sy=30.0, sz=40.0) -> bytes:
    """볼록한 닫힌 상자. 기본값은 CAD 가 mm 로 뱉는 4cm 짜리 블럭이다."""
    vs = [(x * sx, y * sy, z * sz) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    fs = [(1, 2, 4), (1, 4, 3), (5, 8, 6), (5, 7, 8), (1, 3, 7), (1, 7, 5),
          (2, 6, 8), (2, 8, 4), (1, 5, 6), (1, 6, 2), (3, 4, 8), (3, 8, 7)]
    return ("".join(f"v {x} {y} {z}\n" for x, y, z in vs)
            + "".join(f"f {a} {b} {c}\n" for a, b, c in fs)).encode()


def l_prism_obj() -> bytes:
    """닫힌 **오목** 솔리드 — L 자 단면. 안쪽 모서리가 정확히 1.0 만큼 파여 있다."""
    pts = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    n = len(pts)
    vs = [(x, y, 0) for x, y in pts] + [(x, y, 1) for x, y in pts]
    fs = []
    for i in range(1, n - 1):
        fs += [(0, i + 1, i), (n, n + i, n + i + 1)]
    for i in range(n):
        j = (i + 1) % n
        fs += [(i, j, n + j), (i, n + j, n + i)]
    return ("".join(f"v {x} {y} {z}\n" for x, y, z in vs)
            + "".join(f"f {a + 1} {b + 1} {c + 1}\n" for a, b, c in fs)).encode()


def tetra_stl() -> bytes:
    """닫힌 정사면체, **바이너리** STL. 꼭짓점이 삼각형마다 복제돼 있다(STL 의 성질)."""
    t = [((0, 0, 0), (0, 1, 0), (1, 0, 0)), ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
         ((0, 0, 0), (0, 0, 1), (0, 1, 0)), ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
    buf = b"\0" * 80 + struct.pack("<I", len(t))
    for tri in t:
        buf += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v) for v in tri) \
            + struct.pack("<H", 0)
    return buf


ASCII_STL = (b"solid t\nfacet normal 0 0 0\nouter loop\n"
             b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid t\n")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """⚠ 끝나면 못 박은 루트를 **푼다.** 안 그러면 이 파일의 다른 테스트가 지워진
    tmp_path 를 가리킨 채 돈다 — 순서에 따라 붙었다 떨어졌다 하는 종류의 실패다."""
    from piper_sim import assets as A

    from app.core.config import settings
    from app.services import sim_assets

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    try:
        yield sim_assets
    finally:
        A.use_config_dir(None)


# ── 읽기·재기 ──────────────────────────────────────────────────────────────

def test_the_three_formats_we_take_and_what_to_do_about_the_rest(tmp_path):
    """⚠ ASCII STL 과 GLB 는 **흔한 내보내기 기본값**이다. MuJoCo 는 둘 다 못 읽는데,
    그쪽 오류는 "number of faces should be between 1 and 200000" 이라 아무도 못 고친다."""
    from piper_sim import mesh as M

    (tmp_path / "a.obj").write_bytes(box_obj())
    (tmp_path / "a.stl").write_bytes(tetra_stl())
    assert M.inspect(tmp_path / "a.obj")["faces"] == 12
    assert M.inspect(tmp_path / "a.stl")["faces"] == 4

    (tmp_path / "bad.stl").write_bytes(ASCII_STL)
    with pytest.raises(M.MeshError, match="바이너리"):
        M.inspect(tmp_path / "bad.stl")
    for name, word in (("x.glb", "Blender"), ("x.dae", "Blender"), ("x.step", "FreeCAD"),
                       ("x.jpg", "PNG")):
        (tmp_path / name).write_bytes(b"whatever")
        with pytest.raises(M.MeshError, match=word):
            M.inspect(tmp_path / name)
    (tmp_path / "x.zip").write_bytes(b"PK")
    with pytest.raises(M.MeshError, match="OBJ 또는 바이너리 STL"):
        M.inspect(tmp_path / "x.zip")


def test_vertices_are_welded_before_measuring_or_every_stl_looks_open(tmp_path):
    """⚠ STL 에는 꼭짓점 공유가 없다 — 삼각형마다 좌표를 새로 적는다. 용접 전에 재면
    모서리가 한 면에만 붙은 것처럼 보여 **닫힌 솔리드가 전부 "열림"으로** 나온다."""
    from piper_sim import mesh as M

    (tmp_path / "t.stl").write_bytes(tetra_stl())
    v, f = M.read(tmp_path / "t.stl")
    assert len(v) == 4, "용접을 안 했다 — STL 은 12 개 정점으로 온다"
    assert M.is_closed(f), "닫힌 정사면체를 열림으로 봤다"


def test_concavity_is_measured_without_trusting_which_way_the_faces_point(tmp_path):
    """⚠ 처음엔 "평면 **바깥**으로 나간 최대 거리"로 쟀다. 그건 법선이 바깥을 향한다는
    가정이라 뒤집힌 면 하나에 무너진다 — 실제로 V 자 골짜기의 깊이 0.1m 를 **0 으로**
    답했다. 면마다 꼭짓점이 한쪽에만 있는지 양쪽에 걸치는지만 보면 그 가정이 필요 없다."""
    from piper_sim import mesh as M

    (tmp_path / "box.obj").write_bytes(box_obj(1, 1, 1))
    (tmp_path / "l.obj").write_bytes(l_prism_obj())
    assert M.inspect(tmp_path / "box.obj")["concavity"] == pytest.approx(0, abs=1e-9)
    l = M.inspect(tmp_path / "l.obj")
    assert l["closed"] and l["concavity"] == pytest.approx(1.0, abs=1e-6), \
        "L 자 안쪽 모서리(깊이 1.0)를 못 잡았다"

    # 면 방향을 전부 뒤집어도 같은 답이어야 한다
    flipped = (tmp_path / "l.obj").read_text().replace("f ", "f ")
    lines = [ln.split() for ln in flipped.splitlines() if ln.startswith("f ")]
    head = "".join(ln + "\n" for ln in flipped.splitlines() if ln.startswith("v "))
    (tmp_path / "flip.obj").write_text(head + "".join(f"f {c} {b} {a}\n" for _, a, b, c in lines))
    assert M.inspect(tmp_path / "flip.obj")["concavity"] == pytest.approx(1.0, abs=1e-6)


def test_the_unit_guess_lands_on_the_size_a_tabletop_object_actually_is():
    """⚠ "범위에 들어오는 첫 배율"은 틀린다 — 20×30×40 은 cm(40cm)도 범위에 들지만 CAD 의
    mm(4cm)일 때가 훨씬 흔하다. 추측은 어디까지나 기본값이고 화면이 확인을 받는다."""
    from piper_sim.assets import guess_unit_scale as g

    assert g([20, 30, 40]) == 0.001          # CAD mm → 4cm
    assert g([400, 200, 150]) == 0.001       # mm → 40cm
    assert g([0.02, 0.03, 0.04]) == 1.0      # 이미 m
    assert g([8.2, 8.1, 9.5]) == 0.01        # cm 스캔 → 9.5cm
    assert g([0, 0, 0]) == 1.0               # 잴 게 없으면 건드리지 않는다


# ── 저장소 ────────────────────────────────────────────────────────────────

def test_the_same_file_twice_is_one_asset_and_a_bad_file_leaves_nothing(store):
    """id 는 내용 해시다 — 같은 파일을 또 올려도 중복이 안 쌓인다. 그리고 못 쓰는 파일은
    디스크에 안 남는다: 목록에만 있고 못 쓰는 자산이 쌓이면 사람이 그걸 못 치운다."""
    a = store.add(box_obj(), "block.obj", name="블럭")
    b = store.add(box_obj(), "다른이름.obj", name="같은 것")
    assert a["id"] == b["id"] and len(store.listing()) == 1
    assert a["unit_scale"] == 0.001 and a["bbox_m"] == pytest.approx([0.02, 0.03, 0.04])

    with pytest.raises(ValueError, match="바이너리"):
        store.add(ASCII_STL, "bad.stl")
    assert len(store.listing()) == 1, "거절한 파일이 자산으로 남았다"
    assert not any(d.name != a["id"] for d in store.store().root().iterdir())


def test_a_person_can_correct_the_units_and_everything_follows(store):
    a = store.add(box_obj(), "block.obj")
    fixed = store.set_unit_scale(a["id"], 0.01)
    assert fixed["bbox_m"] == pytest.approx([0.2, 0.3, 0.4]), "크기가 안 따라왔다"
    assert store.meta(a["id"])["unit_scale"] == 0.01, "디스크에 안 남았다"
    with pytest.raises(ValueError, match="이상합니다"):
        store.set_unit_scale(a["id"], 0)
    assert store.delete(a["id"]) and store.listing() == []


def test_an_asset_id_cannot_walk_out_of_the_store(store):
    for bad in ("../escape", "", "ZZZZ", "0" * 8):
        with pytest.raises(ValueError, match="이상합니다"):
            store.meta(bad)


def test_the_gateway_says_where_the_store_is_instead_of_guessing(store, tmp_path, monkeypatch):
    """⚠ 한 기계에 배포본과 개발 체크아웃이 같이 있으면 추정이 갈린다 — `/srv/piper-data` 가
    있으니 데몬 쪽 규칙은 그걸 고르는데 개발 게이트웨이는 `~/.config/piper-web` 을 쓴다.
    그러면 "올렸는데 시뮬에 없다"가 된다. 게이트웨이는 자기 설정을 **못 박는다.**"""
    from piper_sim import assets as A

    monkeypatch.setenv("PIPER_CONFIG_DIR", "/nowhere/else")
    store.add(box_obj(), "block.obj")
    assert A.root() == tmp_path / "sim_assets", "환경변수 추정이 게이트웨이 설정을 이겼다"
    # 데몬에는 못 박을 것이 없다 — 환경변수 → 데이터 루트 → 기본 순서를 그대로 쓴다
    A.use_config_dir(None)
    assert A.config_root() == Path("/nowhere/else")


def test_the_daemon_unit_is_told_where_the_store_is():
    """데몬은 호스트에서 돈다 — 게이트웨이가 `/data/config` 로 보는 그 디렉토리를 호스트
    경로로 알아야 메시를 연다. 유닛이 박고, 설치 스크립트가 게이트웨이와 같은 규칙으로 고른다."""
    unit = (REPO / "deploy" / "systemd" / "piper-simd.service").read_text()
    assert "Environment=PIPER_CONFIG_DIR=@CONF@" in unit
    inst = (REPO / "deploy" / "install-daemons.sh").read_text()
    assert "s|@CONF@|$CONF|g" in inst, "치환이 없으면 유닛에 @CONF@ 가 그대로 남는다"
    for rule in ('PIPER_CONFIG_DIR:-', 'PIPER_DATA_ROOT/config', '/srv/piper-data',
                 '$HOME/.config/piper-web'):
        assert rule in inst, f"게이트웨이와 같은 순서로 안 고른다 ({rule})"


# ── 가상환경에 얹기 ────────────────────────────────────────────────────────────

def test_a_mesh_object_lands_on_the_table_and_collides_as_its_hull(store, monkeypatch):
    """⚠ MuJoCo 는 메시를 **자기 무게중심으로** 옮겨 놓고 geom 위치로 그걸 되돌린다. 우리는
    그 위에 `origin_offset`(AABB 아래면 가운데 → 원점)을 얹어 놓으면 앉게 한다.
    실측: body z 가 -0.0002 로 안착하고 geom 중심이 (0, 0, 반높이)에 온다."""
    mujoco = pytest.importorskip("mujoco")
    from piper_sim.scene import load_model

    a = store.add(box_obj(), "block.obj", name="블럭")        # 20×30×40 mm → 0.02×0.03×0.04 m
    spec = {"objects": [{"id": "block", "shape": "mesh", "asset": a["id"],
                         "pos": [0.33, 0.05, 0.0], "mass": 0.06}]}
    m = load_model(spec)
    gid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "block_geom")
    assert m.geom_type[gid] == mujoco.mjtGeom.mjGEOM_MESH
    assert list(m.geom_pos[gid]) == pytest.approx([0, 0, 0.02], abs=1e-6), \
        "메시가 body 원점 위에 안 앉았다 — 클릭 배치가 어긋난다"
    d = mujoco.MjData(m)
    for _ in range(1500):
        mujoco.mj_step(m, d)
    bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "block")
    assert float(d.xpos[bid][2]) == pytest.approx(0.0, abs=0.002), "테이블에 안 앉았다"


def test_a_scene_that_wants_a_mesh_this_machine_lacks_says_so_plainly(store):
    """가상환경 파일은 기계 사이를 오가는데 **메시는 따라오지 않는다.** 적용할 때 터지는 오류가
    사람 말이어야 하고, 목록에서는 누르기 **전에** 말해 줘야 한다."""
    from piper_sim import scene_spec as S

    spec = {"objects": [{"id": "x", "shape": "mesh", "asset": "0" * 16, "pos": [0.3, 0, 0]}]}
    assert S.validate(spec)["objects"][0]["asset"] == "0" * 16, "저장은 된다(다른 기계에서 올 수 있다)"
    assert store.missing(spec) == ["0" * 16]
    pytest.importorskip("mujoco")
    with pytest.raises(S.SceneError, match="이 기계에 없습니다"):
        S.build(spec)


def test_a_mesh_object_is_rejected_without_a_real_asset_id():
    from piper_sim import scene_spec as S

    for bad in (None, "", "nope", "0" * 8, "ZZZZZZZZZZZZZZZZ"):
        with pytest.raises(S.SceneError, match="asset"):
            S.validate({"objects": [{"id": "x", "shape": "mesh", "asset": bad}]})
    with pytest.raises(S.SceneError, match="scale"):
        S.validate({"objects": [{"id": "x", "shape": "mesh", "asset": "a" * 16, "scale": 0}]})


# ── API ───────────────────────────────────────────────────────────────────

def test_the_upload_takes_a_raw_body_and_hands_back_the_real_reason(store):
    """⚠ multipart 는 `python-multipart` 의존성을 끌고 온다 — 이 저장소는 같은 이유로 이미
    raw 바디를 쓴다(YOLO 이미지·가중치). 창구를 둘로 만들 이유가 없다."""
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/sim/assets?filename=block.obj&name=블럭", content=box_obj())
        assert r.status_code == 200 and r.json()["name"] == "블럭"
        aid = r.json()["id"]
        assert c.get("/api/sim/assets").json()["assets"][0]["id"] == aid

        bad = c.post("/api/sim/assets?filename=x.stl", content=ASCII_STL)
        assert bad.status_code == 400 and "바이너리" in bad.json()["detail"]
        assert c.post("/api/sim/assets?filename=x.glb", content=b"glTF").json()["detail"].count("Blender")
        assert c.post("/api/sim/assets?filename=e.obj", content=b"").status_code == 400

        assert c.put(f"/api/sim/assets/{aid}/scale", json={"unit_scale": 0.01}).json()["bbox_m"][2] \
            == pytest.approx(0.4)
        assert c.put(f"/api/sim/assets/{aid}/name", json={"name": "새 이름"}).json()["name"] == "새 이름"
        assert c.delete(f"/api/sim/assets/{aid}").json()["deleted"] is True


def test_the_scene_list_warns_about_missing_meshes_before_you_press_apply(store):
    from app.main import app

    with TestClient(app) as c:
        c.put("/api/sim/scenes/guest", json={"spec": {"objects": [
            {"id": "x", "shape": "mesh", "asset": "0" * 16, "pos": [0.3, 0, 0]}]}})
        row = c.get("/api/sim/scenes").json()["scenes"][0]
    assert row["missing_assets"] == ["0" * 16], "적용을 눌러 보고서야 알게 된다"


def test_the_editor_tells_people_which_formats_and_why(store):
    """화면이 포맷을 말해 주지 않으면 GLB 를 올려 보고 거절당한 뒤에야 안다."""
    page = (REPO / "frontend" / "src" / "pages" / "ScenePage.tsx").read_text()
    assert 'accept=".obj,.stl"' in page
    assert "바이너리" in page and "GLB" in page and "ASCII STL" in page
    assert "Polycam" in page or "스캔" in page, "어디서 만들어 오는지 말 안 한다"
    # 오목·열린 메시 경고 — 볼록껍질 충돌은 올리고 나서 알면 늦다
    assert "볼록껍질" in page and "열린 메시" in page
    assert "'/sim/assets'" in page and "unit_scale" in page
    # 업로드는 raw 바디 (multipart 의존성 없음)
    up = page.split("const uploadMesh", 1)[1].split("}), [", 1)[0]
    assert "method: 'POST', body: f" in up and "FormData" not in up


def test_the_json_a_person_writes_can_name_a_mesh(store, tmp_path):
    """가상환경 JSON 은 사람이 손으로도 쓴다 — 메시 물체의 모양을 여기 한 번 적어 둔다."""
    from piper_sim import scene_spec as S

    a = store.add(box_obj(), "mug.obj", name="머그")
    text = json.dumps({"name": "머그 하나", "objects": [
        {"id": "mug", "label": "머그컵", "shape": "mesh", "asset": a["id"],
         "scale": 1.0, "pos": [0.3, 0.15, 0.0], "mass": 0.12, "movable": True}]})
    v = S.validate(json.loads(text))
    o = v["objects"][0]
    assert o["asset"] == a["id"] and o["scale"] == 1.0 and o["condim"] == 6
    assert S.rest_z(o) == 0.0, "메시는 원점이 이미 바닥이다"


def test_the_screen_says_where_to_get_objects(store):
    """"어디서 구하지?" 가 나오는 자리는 **올리려다 막힌 자리**다 — 도움말을 거기 둔다
    (사용자 요청 2026-09-17). 링크는 2026-09-17 에 전부 응답을 확인했다.
    ⚠ 공식 YCB 사이트(ycbbenchmarks.com)는 그때 500 이라 내려받기 도구를 대신 건다."""
    page = (REPO / "frontend" / "src" / "pages" / "ScenePage.tsx").read_text()
    assert "도움말 — 물체는 어디서 구하나" in page
    for url in ("https://github.com/kevinzakka/mujoco_scanned_objects",
                "https://github.com/google-deepmind/mujoco_menagerie",
                "https://github.com/sea-bass/ycb-tools",
                "https://objaverse.allenai.org/",
                "https://poly.cam/", "https://scaniverse.com/",
                "https://www.blender.org/", "https://github.com/kevinzakka/obj2mjcf"):
        assert url in page, f"{url} 가 도움말에 없다"
    assert "ycbbenchmarks.com" not in page.split("SOURCES")[1].split("const hex")[0], \
        "죽은 사이트를 링크한다"
    assert 'rel="noreferrer"' in page, "바깥 링크에 rel 이 없다"
    # 오목한 것은 메시로 받지 말라는 안내 — 이걸 모르면 그릇을 받아 놓고 왜 안 담기는지 모른다
    assert "볼록껍질" in page and "프리셋으로 지으세요" in page
