"""여러 정책을 올려 두고 바꿔 쓰기 (feature/multi-policy.md).

목적은 **물건을 든 채 정책을 바꾸는 것**이다 — 끄고 켜면 정지마다 파킹하고 토크가 꺼진다. 여기 테스트는

1. 요청 상태 — wrapper/policy_slots.py 가 `policy`·`hold` 를 어떻게 받아 드는가
2. wrapper 배선 — 갈아끼우는 순서(관측을 먼저 비우고 추론 중 표시를 본다), 파킹 없음, 유지, 텔레메트리
3. 추론 인자 — 정책이 하나면 명령이 한 글자도 안 바뀐다. 여럿이면 사양이 실리고, 못 돌릴 조합은 장치 전에 400
4. 게이트웨이 상태 — 바꿨다고 적는 것은 wrapper 텔레메트리가 확인한 뒤다. 올리지 않은 이름은 안 보낸다
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
from policy_slots import PolicySwitch, action_dim, parse_extra  # noqa: E402

from app.services import action_space as A  # noqa: E402
from app.services import inference_state  # noqa: E402

WRAPPER = _REPO / "wrapper" / "lerobot_wrapper.py"


# ── 1. 요청 상태 ────────────────────────────────────────────────────────

def _switch(active="pick"):
    s = PolicySwitch()
    s.attach(["pick", "place_a", "place_b"], active)
    return s


def test_a_request_waits_until_the_loop_swaps():
    s = _switch()
    assert s.update_param("policy", "place_a")
    assert s.pending() == "place_a" and s.blocks() and s.active == "pick"
    s.done("place_a")
    assert s.active == "place_a" and s.pending() is None and not s.blocks()


def test_unknown_names_are_dropped_but_not_passed_on():
    """True 를 돌려야 리스너가 다른 처리(PARAM_SETTERS·"Unknown param")로 흘려보내지 않는다."""
    s = _switch()
    assert s.update_param("policy", "nope")
    assert s.pending() is None and s.active == "pick"
    assert not s.update_param("task", "x")


def test_hold_blocks_and_releasing_it_restarts_the_current_policy():
    s = _switch()
    s.update_param("hold", True)
    assert s.holding and s.blocks() and s.pending() is None
    s.update_param("hold", False)
    assert not s.holding and s.pending() == "pick", "유지하는 동안 쌓인 결과는 낡았다 — 처음부터 다시"


def test_releasing_hold_when_not_holding_does_nothing():
    s = _switch()
    s.update_param("hold", False)
    assert s.pending() is None and not s.blocks()


def test_switching_releases_hold():
    s = _switch()
    s.update_param("hold", True)
    s.update_param("policy", "place_a")
    assert not s.holding and s.pending() == "place_a"


def test_a_newer_request_survives_the_older_swap():
    s = _switch()
    s.update_param("policy", "place_a")
    s.update_param("policy", "place_b")
    s.done("place_a")
    assert s.pending() == "place_b", "늦게 온 요청이 앞 것의 완료에 지워진다"


def test_extra_policy_argument():
    assert parse_extra("", "main") == []
    got = parse_extra(json.dumps([{"name": "place_a", "path": "/ck"}]), "pick")
    assert got == [{"name": "place_a", "path": "/ck", "action_space": "absolute", "delta_spec": {},
                    "target_spec": None}]
    for bad in ([{"name": "pick", "path": "/x"}],                         # 첫 정책과 겹친다
                [{"name": "a", "path": "/x"}, {"name": "a", "path": "/y"}],
                [{"name": "a b", "path": "/x"}],
                [{"name": "a", "path": ""}],
                [{"name": "a", "path": "/x", "action_space": "relative"}],
                {"name": "a"}):
        with pytest.raises(ValueError):
            parse_extra(json.dumps(bad), "pick")


def _ckpt(root: Path, name: str, *, type_="act", action=14, state=14) -> Path:
    c = root / name / "pretrained_model"
    c.mkdir(parents=True)
    (c / "config.json").write_text(json.dumps({
        "type": type_,
        "input_features": {"observation.state": {"type": "STATE", "shape": [state]}},
        "output_features": {"action": {"type": "ACTION", "shape": [action]}},
    }))
    return c


def test_action_dim_reads_the_config(tmp_path):
    assert action_dim(_ckpt(tmp_path, "a", action=7)) == 7
    assert action_dim(tmp_path / "missing") == 0, "모르는 것은 막지 않는다 — 허브 repo id 등"


# ── 2. wrapper 배선 ─────────────────────────────────────────────────────

def _src() -> str:
    return code_only(WRAPPER.read_text())


def _switch_block(src: str) -> str:
    start = src.index("_want = _switch.pending()")
    return src[start:src.index("if _reset_requested:", start)]


def test_the_swap_empties_the_observation_before_looking_at_the_inference_flag():
    """스레드는 추론 중 표시를 올린 **뒤에** 관측을 읽는다. 메인 루프는 관측을 **먼저** 비운다 — 그래야 둘 중
    하나는 반드시 상대를 본다. 어느 쪽이든 뒤집히면 A 의 정규화로 편 관측을 B 가 읽을 수 있다."""
    src = _src()
    block = _switch_block(src)
    assert block.index("_obs_for_inference = None") < block.index("if not _inference_in_flight:")
    thread = src[src.index("def _inference_thread_fn"):]
    assert thread.index("_inference_in_flight = True") < thread.index("pending = _obs_for_inference")


def test_the_swap_neither_parks_nor_restarts_the_filter():
    block = _switch_block(_src())
    for banned in ("ParkingController", "step = 0", "_action_filter.reset()", "_latest_action_dict = None"):
        assert banned not in block, f"정책을 바꾸며 {banned} — 물건을 든 채 바꾸는 것이 목적이다"
    for swapped in ("preprocessor", "postprocessor", "_rename_map", "_target_in", "_delta", "_stage_names"):
        assert swapped + "," in block or swapped + " =" in block, f"{swapped} 를 안 갈아끼운다"
    assert "_reset_gen += 1" in block and "_action_queue = []" in block and "policy.reset()" in block


def test_no_new_inference_while_swapping_or_holding():
    src = _src()
    trigger = src[src.index("_refill_lead = "):]
    assert "not _switch.blocks()" in trigger[:trigger.index("_obs_event.set()")]


def test_hold_resends_the_last_action_instead_of_taking_from_the_queue():
    src = _src()
    hold = src.index("if _switch.holding:", src.index("_queue_step = step"))
    assert "action_np, action_dict = _latest_action_np, _latest_action_dict" in src[hold:hold + 200]
    assert hold < src.index("ensembled = _ensembler.pop(step)"), "유지 중에 앙상블을 꺼낸다"


def test_extra_policies_are_checked_before_the_robot_connects():
    src = _src()
    connect = src.index("robot.connect()")
    assert src.index("_check_slot_markers(_x)") < connect
    assert src.index('_target_input_for(_x["target_spec"]') < connect


def test_policy_setters_reach_every_loaded_policy():
    """AMP·RTC 를 지금 정책에만 걸면 바꾼 뒤의 정책이 다른 설정으로 돈다."""
    src = _src()
    listener = src[src.index("def param_listener"):src.index("def _load_policy")]
    assert "for p in [s.policy for s in _slots.values()]" in listener
    assert "_switch.update_param(key, value)" in listener


def test_one_policy_keeps_its_telemetry():
    src = _src()
    i = src.index('telemetry["policy"]')
    assert "if len(_slots) > 1:" in src[i - 60:i]


def test_paused_telemetry_carries_the_policy_too():
    """일시정지 중에도 바꿀 수 있다(갈아끼우기는 일시정지 분기보다 위다) — 화면·게이트웨이가 그걸 봐야 한다."""
    src = _src()
    paused = src[src.index('"paused": True,'):]
    paused = paused[:paused.index("continue")]
    assert 'telemetry["policy"] = _switch.active' in paused and 'telemetry["holding"] = True' in paused
    assert src.index("_want = _switch.pending()") < src.index('"paused": True,')


# ── 3. 추론 인자 ────────────────────────────────────────────────────────

@pytest.fixture()
def lerobot_home(tmp_path, monkeypatch):
    from app.core.config import settings

    home = tmp_path / "lerobot"
    monkeypatch.setattr(settings, "lerobot_dir", home)
    return home


def _args(first: Path, extras=(), name="main", mode="local"):
    from app.routers.models import ExtraPolicy, InferencePreviewRequest, _build_args_for

    req = InferencePreviewRequest(checkpoint_path=str(first), inference_mode=mode, robot_port="can0",
                                  policy_type="act", policy_name=name,
                                  extra_policies=[ExtraPolicy(name=n, checkpoint_path=str(p)) for n, p in extras])
    return _build_args_for(req, "piper_follower", "can0")


def test_one_policy_gets_the_same_command_as_before(tmp_path, lerobot_home):
    args = _args(_ckpt(tmp_path, "pick"))
    assert "--policy-name" not in args and "--extra-policies" not in args


def test_a_named_policy_carries_its_name_even_alone(tmp_path, lerobot_home):
    """게이트웨이는 그 이름으로 바꾸기 요청을 거르는데 wrapper 가 `main` 으로 알면 요청이 버려진다."""
    args = _args(_ckpt(tmp_path, "pick"), name="pick")
    assert args[args.index("--policy-name") + 1] == "pick" and "--extra-policies" not in args


def test_extra_policies_carry_their_specs_in_the_wrappers_shape(tmp_path, lerobot_home):
    pick, place = _ckpt(tmp_path, "pick"), _ckpt(tmp_path, "place")
    args = _args(pick, [("place_a", place)], name="pick")
    raw = args[args.index("--extra-policies") + 1]
    assert parse_extra(raw, "pick") == [{"name": "place_a", "path": str(place), "action_space": "absolute",
                                         "delta_spec": {}, "target_spec": None}]


def test_a_delta_extra_carries_its_delta_spec(tmp_path, lerobot_home):
    spec = {"delta_names": ["joint1.pos"], "clamp": {"joint1.pos": 5.0}}
    ds = lerobot_home / "org/place_delta"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text("{}")
    (ds / "meta" / A.DATASET_META).write_text(json.dumps({"action_space": "delta_v1", **spec}))
    place = _ckpt(tmp_path, "place")
    (place / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": "org/place_delta", "root": None}}))
    raw = _args(_ckpt(tmp_path, "pick"), [("place_a", place)])
    got = parse_extra(raw[raw.index("--extra-policies") + 1], "main")[0]
    assert got["action_space"] == "delta_v1" and got["delta_spec"] == spec


@pytest.mark.parametrize("case, detail", [
    ("server", "로컬"),
    ("type", "종류"),
    ("length", "액션 길이"),
    ("dup", "겹칩니다"),
    ("bad_name", "이름이 틀렸습니다"),
    ("missing", "읽을 수 없습니다"),
])
def test_what_cannot_run_together_is_refused(tmp_path, lerobot_home, case, detail):
    pick, place = _ckpt(tmp_path, "pick"), _ckpt(tmp_path, "place")
    extras = {"server": [("place_a", place)],
              "type": [("place_a", _ckpt(tmp_path, "s", type_="smolvla"))] if case == "type" else [],
              "length": [("place_a", _ckpt(tmp_path, "l", action=7))] if case == "length" else [],
              "dup": [("main", place)],
              "bad_name": [("place a", place)],
              "missing": [("place_a", tmp_path / "gone")]}[case]
    with pytest.raises(HTTPException) as e:
        _args(pick, extras, mode="server" if case == "server" else "local")
    assert e.value.status_code == 400 and detail in e.value.detail


def test_an_extra_policy_needs_its_cameras_mapped(tmp_path, lerobot_home):
    """학습 카메라 대조는 매핑에 없는 카메라를 "모름" 으로 넘긴다 — 빠지면 그 정책으로 바꾸는 순간부터 매 추론이 죽는다."""
    from app.routers.models import ExtraPolicy, InferencePreviewRequest, _extra_policies_for

    pick = _ckpt(tmp_path, "pick")
    place = _ckpt(tmp_path, "place")
    cfg = json.loads((place / "config.json").read_text())
    cfg["input_features"]["observation.images.wrist"] = {"type": "VISUAL", "shape": [3, 480, 640]}
    (place / "config.json").write_text(json.dumps(cfg))

    def body(mapping):
        return InferencePreviewRequest(checkpoint_path=str(pick), robot_port="can0", camera_mapping=mapping,
                                       extra_policies=[ExtraPolicy(name="place_a", checkpoint_path=str(place))])

    with pytest.raises(HTTPException) as e:
        _extra_policies_for(body({"top": "cam1"}))
    assert e.value.status_code == 400 and "wrist" in e.value.detail
    assert _extra_policies_for(body({"top": "cam1", "wrist": "cam2"}))[0]["name"] == "place_a"
    assert _extra_policies_for(body({})), "매핑이 없는 요청은 첫 정책도 대조하지 않는다 — 같은 규칙"


def test_start_refuses_before_touching_cameras(tmp_path, lerobot_home, monkeypatch):
    from app.routers import models as M

    monkeypatch.setattr(M, "require_idle", lambda *_: None)

    def boom(*_a, **_k):
        raise AssertionError("못 돌릴 정책인데 카메라를 준비했다")

    monkeypatch.setattr(M, "prepare_cameras", boom)
    body = M.InferenceStartRequest(checkpoint_path=str(_ckpt(tmp_path, "pick")), robot_type="piper_follower",
                                   robot_port="can0",
                                   extra_policies=[M.ExtraPolicy(name="x", checkpoint_path=str(tmp_path / "gone"))])
    with pytest.raises(HTTPException) as e:
        asyncio.run(M.start_inference(body))
    assert e.value.status_code == 400


# ── 4. 게이트웨이 상태 ───────────────────────────────────────────────────

@pytest.fixture()
def running(monkeypatch):
    from app.services import exclusivity

    monkeypatch.setattr(exclusivity, "is_running", lambda _a: True)
    inference_state.set_running("act", "/ck/pick", policies={"pick": "/ck/pick", "place_a": "/ck/place"},
                                active="pick")
    yield
    inference_state.clear()


def test_active_follows_the_wrappers_telemetry(running):
    inference_state.note("place_a", False)
    assert inference_state.get().active == "place_a"
    inference_state.note("nope", True)                      # 올리지 않은 이름 — 이름은 그대로, 유지는 따른다
    cur = inference_state.get()
    assert cur.active == "place_a" and cur.holding
    inference_state.note(None, False)                       # 정책이 하나면 텔레메트리에 이름이 없다
    assert inference_state.get().active == "place_a" and not inference_state.get().holding


def test_the_policy_endpoint_reports_the_active_checkpoint(running):
    from app.routers.models import inference_policy

    inference_state.note("place_a", True)
    got = asyncio.run(inference_policy())
    assert got["active"] == "place_a" and got["checkpoint_path"] == "/ck/place" and got["holding"]
    assert got["policies"] == {"pick": "/ck/pick", "place_a": "/ck/place"}


def test_switching_sends_only_loaded_names(running, monkeypatch):
    from app.routers import params as P

    sent = []

    async def fake_send(p):
        sent.append(p)

    monkeypatch.setattr(P.param_bridge, "send_params", fake_send)
    asyncio.run(P.switch_policy(P.PolicySwitch(name="place_a")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(P.switch_policy(P.PolicySwitch(name="place_z")))
    assert e.value.status_code == 400
    asyncio.run(P.hold_inference(P.Hold(on=True)))
    assert sent == [{"policy": "place_a"}, {"hold": True}]


def test_switching_without_inference_is_a_conflict(monkeypatch):
    from app.routers import params as P

    inference_state.clear()
    with pytest.raises(HTTPException) as e:
        asyncio.run(P.switch_policy(P.PolicySwitch(name="pick")))
    assert e.value.status_code == 409


def test_the_selection_remembers_the_policies():
    from app.routers import inference as I

    asyncio.run(I.save_selection(I.SelectionRequest(
        model_id="r/010000", policy_name="pick", extra_policies=[{"name": "place_a", "model_id": "r/020000"}])))
    got = asyncio.run(I.get_selection())
    assert got["policy_name"] == "pick" and got["extra_policies"] == [{"name": "place_a", "model_id": "r/020000"}]
    asyncio.run(I.save_selection(I.SelectionRequest()))          # 다른 테스트에 남기지 않는다
    assert asyncio.run(I.get_selection())["extra_policies"] == []


def test_plugins_reach_both_routes_with_params_write():
    from app.core.plugin_scopes import allows

    assert allows({"params.write"}, "POST", "/api/params/policy")
    assert allows({"params.write"}, "POST", "/api/params/hold")


def test_the_sdk_calls_the_documented_routes():
    from piper_plugin.api import Response
    from piper_plugin.robot import Robot

    calls = []

    class Api:
        def post(self, path, json=None, **kw):
            calls.append((path, json))
            return Response(200, b"{}")

    r = Robot(Api())
    r.set_policy("place_a")
    r.hold()
    r.hold(False)
    assert calls == [("/api/params/policy", {"name": "place_a"}), ("/api/params/hold", {"on": True}),
                     ("/api/params/hold", {"on": False})]


def test_the_gateway_reads_the_active_policy_from_telemetry():
    src = code_only((_REPO / "backend" / "app" / "routers" / "ws.py").read_text())
    tele = src.index('if msg_type == "telemetry":')
    assert "inference_state.note(" in src[tele:src.index("return", tele)]
