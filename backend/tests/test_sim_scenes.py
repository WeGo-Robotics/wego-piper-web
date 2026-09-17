"""시뮬 장면 스토어와 API — 사람이 만든 세계를 파일로 (feature/sim-scene-editor.md §6·§8).

장면 하나가 **파일 하나**다. 그래서 "환경 불러오기/내보내기"가 파일을 주고받는 일이 되고,
기계 사이 이사·공유·백업이 같은 한 가지 동작이 된다 (사용자 요청 2026-09-17:
"그 파일을 환경 불러오기로 불러오는거지").

이 파일이 지키는 것 셋:

① **깨진 장면은 디스크에 안 남고**, 왜 깨졌는지가 화면까지 간다. "저장 실패" 로 바꿔
   버리면 사람은 무엇을 고쳐야 할지 모른 채 같은 걸 다시 누른다.
② **데몬에는 id 가 아니라 명세를 보낸다.** 컨테이너는 `/data/config/sim_scenes`, 호스트는
   `/srv/piper-data/config/sim_scenes` — 같은 id 가 서로 다른 경로다.
③ **simd 가 재시작하면 기본 장면으로 돌아온다.** 그 불일치를 게이트웨이가 보고 다시 올린다.
   안 그러면 사람은 자기 세계가 올라가 있다고 믿은 채 엉뚱한 장면에서 수집한다.
"""

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("piper_sim")

CUBE = {"id": "cube", "shape": "box", "size": [0.02, 0.02, 0.02], "pos": [0.35, 0, 0.02]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services import sim_scenes

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    return sim_scenes


def test_a_scene_is_one_file_and_survives_the_round_trip(store):
    store.save("blocks", {"name": "블럭 두 개", "objects": [CUBE]})
    got = store.read("blocks")
    assert got["name"] == "블럭 두 개" and got["id"] == "blocks"
    assert got["objects"][0]["mass"] == 0.05, "저장이 기본값을 안 채웠다"
    rows = store.listing()
    assert [r["id"] for r in rows] == ["blocks"] and rows[0]["count"] == 1
    assert rows[0]["applied"] is False and rows[0]["error"] is None
    # 내보내기는 사람이 열어 고칠 수 있는 모양이어야 한다
    text = store.export_text("blocks")
    assert json.loads(text)["objects"][0]["id"] == "cube" and "\n  " in text
    assert store.delete("blocks") and store.listing() == []


def test_a_broken_scene_never_reaches_the_disk_and_says_why(store):
    """id 가 예약어면 모델이 깨진다. 저장해 두고 적용할 때 터지면, 목록에는 있는데 못 올리는
    장면이 쌓이고 사람은 그게 왜 안 되는지 모른다 — **저장 자체를 막는다.**"""
    with pytest.raises(store.SceneStoreError, match="팔·테이블·카메라"):
        store.save("bad", {"objects": [{"id": "link3", "shape": "box", "size": [.02, .02, .02]}]})
    assert store.listing() == [], "거절한 장면이 디스크에 남았다"


def test_importing_a_file_checks_it_first_and_never_overwrites(store):
    """환경 불러오기 — 다른 기계에서 온 파일이라 깨져 있을 수 있다. 그리고 가져오기는
    **추가**다: 이름이 겹친다고 남의 장면을 덮으면 그건 복구 못 한다."""
    first = store.import_text(json.dumps({"name": "손님 장면", "objects": [CUBE]}))
    again = store.import_text(json.dumps({"name": "손님 장면", "objects": [CUBE]}))
    assert first["id"] != again["id"], "같은 이름이 앞의 장면을 덮었다"
    assert len(store.listing()) == 2

    with pytest.raises(store.SceneStoreError, match="3번째 줄"):
        store.import_text('{\n  "objects": [\n    {"id": "x",}\n  ]\n}')
    with pytest.raises(store.SceneStoreError, match="너무 큽니다"):
        store.import_text(" " * (store.MAX_BYTES + 1))
    with pytest.raises(store.SceneStoreError, match="객체가 아닙니다"):
        store.import_text("[]")
    assert len(store.listing()) == 2, "거절한 가져오기가 장면을 남겼다"


def test_a_scene_id_cannot_walk_out_of_the_data_root(store):
    """id 는 파일명 한 조각이다. `../` 이 들어오면 데이터 루트 밖을 쓰고 지운다."""
    for bad in ("../escape", "a/b", "", ".current", "A-Upper"):
        with pytest.raises(store.SceneStoreError, match="이상합니다"):
            store.read(bad)


def test_a_scene_that_will_not_parse_is_listed_with_its_error_not_hidden(store):
    """⚠ 조용히 건너뛰면 사람이 만든 장면이 **사라진 것처럼** 보인다. 목록에서 사라진
    파일은 아무도 못 고친다."""
    store.save("good", {"objects": [CUBE]})
    (store._dir() / "wrecked.json").write_text("{ nope", encoding="utf-8")
    rows = {r["id"]: r for r in store.listing()}
    assert rows["wrecked"]["error"] and rows["good"]["error"] is None
    assert rows["wrecked"]["count"] == 0


def test_applying_sends_the_spec_itself_because_the_two_sides_see_different_paths(store, monkeypatch):
    """⚠ 컨테이너와 호스트가 같은 디렉토리를 **다른 경로**로 본다 — id 를 보내면 그 차이가
    언젠가 조용히 문다. 게이트웨이가 파일을 읽어 명세 dict 를 통째로 넘긴다."""
    from app.services import sim_robot_client as sim

    sent = []
    monkeypatch.setattr(sim, "call_strict",
                        lambda m, *a, **k: sent.append((m, a)) or {"scene": "x", "objects": []})
    store.save("mine", {"name": "내 장면", "objects": [CUBE]})
    store.apply("mine")
    (method, args), = sent
    assert method == "load_scene"
    assert isinstance(args[0], dict) and args[0]["objects"][0]["id"] == "cube", \
        "데몬에 id 를 보냈다 — 데몬은 그 경로를 못 찾는다"
    assert store.current_id() == "mine"


def test_a_failed_apply_leaves_the_applied_marker_alone(store, monkeypatch):
    """먼저 올리고 나중에 기록한다. 반대면 못 올린 장면이 "적용됨"으로 남아
    `ensure_applied()` 가 매번 같은 실패를 되풀이한다."""
    from app.services import sim_robot_client as sim

    def boom(*a, **k):
        raise RuntimeError("simd 가 응답하지 않습니다 — 시뮬 데몬이 떠 있나요?")

    monkeypatch.setattr(sim, "call_strict", boom)
    store.save("mine", {"objects": [CUBE]})
    with pytest.raises(RuntimeError, match="simd"):
        store.apply("mine")
    assert store.current_id() is None


def test_the_applied_scene_goes_back_up_after_the_daemon_restarts(store, monkeypatch):
    """⚠ simd 는 명세를 **메모리에만** 들고 있다(§6 의 경로 문제를 피한 대가다). 재시작하면
    기본 장면으로 돌아오는데, 그걸 아무도 안 보면 사람은 자기 세계가 올라가 있다고 믿은 채
    엉뚱한 장면에서 수집한다. 게이트웨이 기동이 그걸 본다."""
    from app.services import sim_robot_client as sim

    pushed = []
    monkeypatch.setattr(sim, "sim_available", lambda: True)
    monkeypatch.setattr(sim, "call_strict", lambda m, *a, **k: pushed.append(a[0]) or {})
    store.save("mine", {"objects": [CUBE]})
    monkeypatch.setattr(sim, "call", lambda m, *a, **k: {"spec": {"objects": []}})  # 기본 장면
    store.apply("mine")
    pushed.clear()

    assert store.ensure_applied(), "데몬이 다른 세계인데 다시 안 올렸다"
    assert pushed and pushed[0]["objects"][0]["id"] == "cube"

    # 이미 그 세계면 다시 안 올린다 — 기동마다 물리를 흔들 이유가 없다
    live = {"spec": {"objects": store.read("mine")["objects"]}}
    monkeypatch.setattr(sim, "call", lambda m, *a, **k: live)
    pushed.clear()
    assert store.ensure_applied() is None and not pushed


def test_the_applied_scene_cannot_be_deleted_out_from_under_the_simulator(store, monkeypatch):
    from app.services import sim_robot_client as sim

    monkeypatch.setattr(sim, "call_strict", lambda *a, **k: {})
    store.save("mine", {"objects": [CUBE]})
    store.apply("mine")
    with pytest.raises(store.SceneStoreError, match="지금 올라간 장면"):
        store.delete("mine")


def test_the_api_hands_the_real_reason_to_the_screen(store):
    """400 과 **그 문장**. "저장 실패" 로 바꾸면 사람은 무엇을 고쳐야 할지 모른다."""
    from app.main import app

    with TestClient(app) as c:
        r = c.put("/api/sim/scenes/bad",
                  json={"spec": {"objects": [{"id": "top", "shape": "box", "size": [.02, .02, .02]}]}})
        assert r.status_code == 400 and "팔·테이블·카메라" in r.json()["detail"]

        assert c.put("/api/sim/scenes/ok", json={"spec": {"name": "좋아", "objects": [CUBE]}}).status_code == 200
        body = c.get("/api/sim/scenes").json()
        assert [s["id"] for s in body["scenes"]] == ["ok"] and body["current"] is None
        assert c.get("/api/sim/scenes/ok/export").text.startswith("{")
        assert c.get("/api/sim/scenes/nope").status_code == 400


def test_the_screen_gets_the_shape_list_from_the_backend_not_its_own_copy(store):
    """화면이 모양·프리셋 목록을 따로 적으면, 늘릴 때마다 두 곳을 고쳐야 하고 한 곳을
    잊으면 화면에만 없는(또는 화면에만 있는) 모양이 생긴다."""
    from app.main import app
    from piper_sim import scene_spec as S

    with TestClient(app) as c:
        d = c.get("/api/sim/scenes/defaults").json()
    assert set(d["primitives"]) == set(S.PRIMITIVES) and set(d["presets"]) == set(S.PRESETS)
    assert d["primitives"]["cylinder"]["size_len"] == 2
    assert d["presets"]["bin"]["params"]["wall_h"] == 0.03
    assert "link3" in d["reserved"] and d["max_objects"] == S.MAX_OBJECTS


def test_the_gateway_image_carries_the_scene_spec_it_validates_with():
    """⚠ 게이트웨이가 `piper_sim.scene_spec` 을 import 한다 — 이미지에 없으면 배포판에서만
    장면 저장이 500 이 된다. `piper_cam` 이 빠져 카메라 프로파일 저장이 죽은 것과 같은
    사고다(NUC, 2026-09-15). `--no-deps` 라 mujoco 는 안 딸려 온다 — 굽는 것은 simd 의 일이다."""
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[2] / "backend" / "Dockerfile").read_text()
    assert "COPY sim/ /tmp/pkg/sim/" in dockerfile and "/tmp/pkg/sim" in dockerfile
    install = next(ln for ln in dockerfile.splitlines() if "pip install --no-deps /tmp/pkg/bus" in ln)
    assert "--no-deps" in install, "mujoco 까지 딸려 와 이미지가 부푼다"
