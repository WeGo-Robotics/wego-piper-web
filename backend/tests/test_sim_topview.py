"""탑뷰 카메라 높이 — **z 만** 사람이 조절한다 (feature/sim-topview-height.md).

여기서 지키는 것: 높이가 실제로 먹는가, 그게 **가상환경 교체가 아닌가**(재컴파일·
렌더러 재생성 없음), 기본값이 바탕 XML 과 갈리지 않는가, 범위는 거절이 아니라 클램프인가.
"""

import inspect
import json
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


# ── 명세에 실린다 (2단계) ──


def test_a_scene_without_cameras_keeps_todays_world():
    """옛 가상환경 파일에는 이 키가 없다. **기본값이 채워져 오늘과 같은 세계**가 되어야
    한다 — 여기가 어긋나면 업데이트하는 순간 모든 기존 가상환경의 화각이 바뀐다."""
    from piper_sim import scene_spec

    v = scene_spec.validate({"version": 1, "id": "old", "name": "옛것", "objects": []})
    assert v["cameras"]["top"]["z"] == pytest.approx(scene_spec.TOP_Z_DEFAULT)
    # 기본 가상환경(파일)도 같은 값이어야 한다
    assert scene_spec.default()["cameras"]["top"]["z"] == pytest.approx(
        scene_spec.TOP_Z_DEFAULT)


def test_the_spec_height_is_clamped_but_a_typo_is_refused():
    """범위 밖은 클램프 — 남의 파일 하나 때문에 불러오기가 통째로 실패하면 안 된다.
    숫자가 아닌 것은 다르다: 사람이 고쳐야 할 오타라서 말해 준다."""
    from piper_sim import scene_spec

    lo, hi = scene_spec.CAMERA_Z_RANGE
    over = scene_spec.validate({"cameras": {"top": {"z": 3.0}}, "objects": []})
    assert over["cameras"]["top"]["z"] == pytest.approx(hi)
    under = scene_spec.validate({"cameras": {"top": {"z": 0.01}}, "objects": []})
    assert under["cameras"]["top"]["z"] == pytest.approx(lo)
    for bad in ("높이", None, float("nan")):
        with pytest.raises(scene_spec.SceneError, match="숫자가 아닙니다"):
            scene_spec.validate({"cameras": {"top": {"z": bad}}, "objects": []})


def test_the_world_is_built_at_the_height_the_spec_asks_for():
    """살아 있는 세계(`set_camera_z`)와 **굽는 세계**가 같은 높이로 서야 한다.
    안 그러면 가상환경을 다시 올리는 순간 카메라가 조용히 원래 자리로 돌아간다."""
    from piper_sim import scene_spec

    m = scene_spec.build({"version": 1, "id": "high", "name": "높게",
                          "cameras": {"top": {"z": 0.75}}, "objects": []})
    cid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, "top")
    assert float(m.cam_pos[cid][2]) == pytest.approx(0.75)
    # x·y 는 안 건드린다 — 높이만 여는 것이 이 기능의 전부다
    assert [float(v) for v in m.cam_pos[cid][:2]] == pytest.approx([0.25, 0.0])


def test_the_dataset_records_the_height_it_actually_saw(monkeypatch):
    """⚠ **사이드카는 저장된 파일이 아니라 돌고 있는 값을 적는다.** 슬라이더를 끌고
    저장을 안 한 채 수집을 시작할 수 있고, 그때 파일의 옛 높이를 적으면 거짓말이 된다.
    """
    from app.services import sim_scenes

    stored = {"version": 1, "id": "s", "name": "n",
              "cameras": {"top": {"z": 0.60}}, "objects": []}
    monkeypatch.setattr(sim_scenes, "current_id", lambda: "s")
    monkeypatch.setattr(sim_scenes, "read", lambda sid: stored)
    from app.services import sim_robot_client as sim
    monkeypatch.setattr(sim, "call",
                        lambda verb, *a, **kw: 0.82 if verb == "camera_z" else kw.get("default"))

    spec = sim_scenes.applied_spec()
    assert spec["cameras"]["top"]["z"] == pytest.approx(0.82), "파일의 옛 높이를 적었다"
    assert stored["cameras"]["top"]["z"] == pytest.approx(0.60), "저장된 명세를 건드렸다"
    assert sim_scenes.sidecar(spec)["cameras"]["top"]["z"] == pytest.approx(0.82)


# ── 게이트웨이 (3단계) ──


def test_moving_the_camera_is_refused_while_the_observation_is_being_consumed():
    """⚠ 에피소드 한가운데 화각이 바뀌면 그 에피소드는 앞뒤가 다른 세계다. 추론은 한 겹
    더 나쁘다 — 정책이 **학습한 적 없는 화각**을 받고 그대로 팔을 움직인다."""
    from app.services import exclusivity as X

    blockers = set(X.BLOCKED_BY[X.Activity.CAMERA_MOVE])
    assert {X.Activity.RECORDING, X.Activity.INFERENCE,
            X.Activity.ORCHESTRATOR} <= blockers, "관측을 먹는 활동을 안 막는다"
    assert X.Activity.CAMERA_MOVE in X.LABELS, "409 문구를 만들 라벨이 없다"
    assert X.Activity.CAMERA_MOVE not in X.STATE_PROVIDERS, \
        "카메라 높이 변경은 순간 동작이다 — 남을 막는 활동이 아니다"


def test_moving_the_camera_is_allowed_while_someone_drives_the_arm():
    """⚠ **가상환경 교체와 여기서 갈린다.** 물체가 사라지고 생기는 것은 팔을 모는 중에
    위험하지만, 카메라가 올라가는 것은 팔에 아무 일도 안 한다. 막을 이유 없는 것을 같이
    막으면 사람이 납득을 못 하고, 납득 못 하는 규칙은 우회된다.

    같은 이유로 조종 창(웹 리더)도 안 본다 — 그건 `busy_reason` 쪽 이야기다.
    """
    from app.services import exclusivity as X
    from app.services import sim_scenes

    assert X.Activity.TELEOP not in X.BLOCKED_BY[X.Activity.CAMERA_MOVE]
    assert X.Activity.TELEOP in X.BLOCKED_BY[X.Activity.SCENE_SWAP], \
        "교체 쪽까지 풀어 버렸다"
    src = inspect.getsource(sim_scenes.camera_busy_reason)
    assert "web_leader" not in src, "조종 창을 보면 조종 중 높이 조절이 막힌다"


def test_the_endpoint_refuses_first_and_asks_the_daemon_second(monkeypatch):
    """거절이 **먼저**다 — 데몬을 부른 뒤에 막으면 세계는 이미 바뀌어 있다."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import sim_robot_client as sim
    from app.services import sim_scenes

    called: list = []
    monkeypatch.setattr(sim, "call_strict",
                        lambda verb, *a: called.append((verb, a)) or float(a[0]))

    with TestClient(app) as c:
        monkeypatch.setattr(sim_scenes, "camera_busy_reason", lambda: "녹화")
        r = c.put("/api/sim/scenes/live/camera", json={"z": 0.9})
        assert r.status_code == 409 and "녹화" in r.json()["detail"]
        assert not called, "거절했는데 세계를 건드렸다"

        monkeypatch.setattr(sim_scenes, "camera_busy_reason", lambda: None)
        r = c.put("/api/sim/scenes/live/camera", json={"z": 0.9})
        assert r.status_code == 200 and r.json()["z"] == pytest.approx(0.9)
        assert called == [("set_camera_z", (0.9,))]


def test_the_readout_says_what_the_height_means():
    """높이 숫자만으로는 0.72m 가 무슨 뜻인지 아무도 모른다. fovy 를 아는 쪽이
    "얼마나 담기나 · 물체가 몇 px 인가 · 테이블이 다 보이나"로 옮겨 준다.

    ⚠ 화면이 fovy 를 베껴 적으면 바탕 XML 을 고치는 날 조용히 어긋난다 — 그래서 서버가 준다.
    """
    from piper_sim.world import World

    w = World()
    w.set_camera_z(0.60)
    low = w.camera_view(aspect=640 / 480, height_px=480)
    w.set_camera_z(1.20)
    high = w.camera_view(aspect=640 / 480, height_px=480)

    # 실측 대조: v0.5.1 이 0.6m 에서 큐브 33px 을 쟀다
    assert low["probe_px"] == pytest.approx(33, abs=1), low
    assert low["covers_table"] is False, "0.6m 에서 테이블이 다 보인다면 표가 틀렸다"
    assert high["covers_table"] is True, "최대 높이에서도 테이블이 안 들어온다"
    assert high["probe_px"] < low["probe_px"], "올라갔는데 물체가 커졌다"
    assert high["half_y"] > low["half_y"]
    # 화면이 클램프 끝을 알아야 슬라이더를 그린다
    view_src = inspect.getsource(World.camera_view)
    assert "TABLE_HALF" in view_src, "테이블 크기를 여기에 또 손으로 적었다"


def test_the_ui_and_the_click_math_share_one_table_size():
    """테이블 범위가 두 곳에 손으로 적히면 "다 보인다"는 말과 실제 배치 한계가 갈린다."""
    src = (REPO / "sim" / "piper_sim" / "world.py").read_text()
    assert src.count("TABLE_HALF = (") == 1
    assert "0.35 - 0.55" not in src and "-0.45, 0.45" not in src, \
        "클램프가 아직 숫자를 손으로 들고 있다"


# ── 조명 기준선 (5단계) ──


def test_moving_the_camera_drops_the_light_baseline(monkeypatch):
    """⚠ 카메라가 움직이면 밝기·색이 튄다 — **맞는 관측이지만 틀린 해석**이다(조명이
    아니라 화각이 바뀐 것이다). 쓸모없는 경보는 옆의 진짜 경보를 묻는다: v0.5.6 에서
    손목 카메라 때문에 카메라별 스위치를 단 것과 같은 병이다.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import sim_robot_client as sim
    from app.services import sim_scenes
    from app.services.light_watch import light_watch

    monkeypatch.setattr(sim_scenes, "camera_busy_reason", lambda: None)
    monkeypatch.setattr(sim, "call_strict", lambda verb, *a: float(a[0]))
    light_watch._judges["sim:top"] = object()

    with TestClient(app) as c:
        assert c.put("/api/sim/scenes/live/camera", json={"z": 0.9}).status_code == 200
    assert "sim:top" not in light_watch._judges, "옛 기준선으로 새 화각을 판정한다"


def test_the_slider_reads_its_numbers_from_the_server():
    """화면이 fovy 를 베껴 적으면 바탕 XML 을 고치는 날 조용히 어긋난다.
    그리고 카메라를 올렸다고 **배치 캔버스가 잠기면 안 된다** — 세계는 이미 그 높이다.
    """
    from conftest import code_only

    # ⚠ 코드만 본다 — "화면이 fovy 를 베껴 적으면 안 된다"고 적어 둔 주석이 바로 이
    #   검사에 걸린다(이 저장소에서 세 번째다).
    page = code_only((REPO / "frontend" / "src" / "pages" / "ScenePage.tsx").read_text())
    assert "/sim/scenes/live/camera" in page
    assert "fovy" not in page and "Math.tan" not in page, "화면이 화각을 직접 계산한다"
    assert "cam.probe_px" in page and "cam.covers_table" in page, "판독값을 안 보여 준다"
    # 저장은 `dirty` 와 따로 센다 — 캔버스를 잠그는 값은 그대로 둔다
    assert "camDirty" in page and "(!dirty && !camDirty)" in page
    assert "const applied = !!spec && current === sid && !stale" in page, \
        "카메라 변경이 배치 캔버스를 잠그게 됐다"
    # ⚠ 슬라이더는 **탑뷰와 같은 카드**에 있다 (사용자 요청 2026-09-28). 떼어 놓으면
    #   무엇의 높이인지가 화면에서 안 보이고, 페이지 바탕에 덩그러니 떠 있게 된다.
    head = page.split('id="cam-z"', 1)[0]
    opened = head.rfind("bg-neutral-800 p-4")        # 슬라이더 위로 가장 가까운 카드
    assert opened != -1 and "sim%3Atop/preview" in head[opened:], \
        "높이 슬라이더가 탑뷰 카드 밖에 있다 — 둘 사이에 탑뷰가 없다"


# ── 출처 기록 (feature/sim-provenance.md) ──


def test_the_sidecar_says_which_schema_and_which_world():
    """⚠ **바탕 세계는 명세가 아니라 wheel 안 XML 이 정한다** — 팔·테이블·조명·카메라 fovy.
    그게 바뀌면 같은 명세라도 다른 세계다. v0.5.1 이 탑뷰를 0.9 → 0.6 으로 옮긴 것이 실제
    사례이고, 그 전후 데이터는 파일상으로 구분되지 않았다.

    버전 문자열이 아니라 **내용 해시**를 남긴다: 릴리스 번호가 그대로여도 파일이 바뀌면
    다른 세계이고, 해시는 그걸 그대로 말한다.
    """
    import hashlib

    from app.services import sim_scenes
    from piper_sim import scene_spec

    side = sim_scenes.sidecar({"version": 1, "id": "s", "name": "n", "objects": []})
    assert side["spec_version"] == scene_spec.SPEC_VERSION
    want = hashlib.sha256(scene_spec.SCENE_XML.read_bytes()).hexdigest()[:16]
    assert side["sim"]["base_sha"] == want, "바탕 XML 의 해시가 아니다"
    assert side["sim"]["package"], "시뮬 패키지 판을 모른다"


def test_a_training_run_inherits_the_world_it_learned_in(tmp_path, monkeypatch):
    """체크포인트가 아는 것은 `dataset.repo_id` 문자열 하나다(.120 실측). 데이터셋이
    지워지거나 이름이 바뀌면 "이 정책이 무엇을 보고 배웠나"에 영영 답을 못 한다.

    ⚠ **시작할 때** 복사한다 — 끝날 때 하면 그 사이에 사라진 데이터셋을 못 따라간다.
    """
    from app.services import sim_scenes

    ds, run = tmp_path / "ds", tmp_path / "run"
    spec = {"version": 1, "id": "s", "name": "n",
            "cameras": {"top": {"z": 0.82}}, "objects": []}
    sim_scenes.write_sidecar(ds, spec)

    assert sim_scenes.inherit_to_run(ds, run) is True
    got = json.loads((run / "piper_scene.json").read_text())
    assert got == json.loads((ds / "meta" / "piper_scene.json").read_text()), \
        "물려준 사본이 데이터셋의 것과 다르다"
    assert got["cameras"]["top"]["z"] == pytest.approx(0.82)
    assert got["sim"]["base_sha"], "버전 없는 사본이 체크포인트에 박제됐다"


def test_a_real_robot_run_inherits_nothing_at_all(tmp_path):
    """실기 데이터셋에는 가상환경 기록이 없는 것이 정상이다. 빈 파일을 남기면
    "시뮬인데 기록이 없다" 와 구분이 안 된다."""
    from app.services import sim_scenes

    ds, run = tmp_path / "ds", tmp_path / "run"
    ds.mkdir()
    assert sim_scenes.inherit_to_run(ds, run) is False
    assert not (run / "piper_scene.json").exists(), "빈 사이드카를 남겼다"


def test_training_start_hands_the_scene_over_where_the_notes_already_go():
    """붙는 자리가 이미 있다 — 제목·설명 사이드카를 쓰는 그 자리다."""
    src = (REPO / "backend" / "app" / "routers" / "training.py").read_text()
    assert "sim_scenes.inherit_to_run" in src
    start = src.split("notes_written = False", 1)[1].split("return {", 1)[0]
    assert "inherit_to_run" in start, "학습 시작 경로가 아니다"
    assert '"scene_inherited"' in src, "물려줬는지 화면이 알 길이 없다"
