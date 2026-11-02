"""목표 위치 입력 (feature/target-conditioning.md) — 추론 쪽 전부.

학습 데이터는 `observation.state` 뒤에 목표 칸이 붙었다. 추론이 같은 칸을 같은 식으로 만들지 못하면 정책이
엉뚱한 자리를 집으러 가거나(사양이 다름) 첫 프레임부터 정규화에서 죽는다(사양이 빠짐). 그래서:

1. 식 — wrapper 의 `encode` 가 변환기의 것과 같다, 로봇 state 이름·길이를 대조한다
2. 버스 값 — 게이트웨이와 wrapper 가 같은 규칙으로 자르고 지운다
3. 체크포인트 판정 — 표식 > 학습 데이터셋 > 이름 > 목표 입력 아님
4. 추론 인자 — 목표 입력이면 사양이 실리고 아니면 명령이 한 글자도 안 바뀐다. 서버 모드·모름·손으로 고친 인자는 막는다
5. 이름 계약 — 백엔드가 다시 적은 것과 변환기·wrapper 가 같은가
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
import target_input as W  # noqa: E402

from app.services import target_input as B  # noqa: E402
from app.services.param_bridge import ParamBridge, target_value  # noqa: E402

NAMES = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]
SPEC = {"encoding": "rff_v1", "names": ["target.valid", "target.u", "target.v", "target.cos0", "target.sin0"],
        "freqs": [[1.5, -0.5]], "aspect": 0.75, "state_names": NAMES}


# ── 1. 식 ───────────────────────────────────────────────────────────────

def test_the_wrapper_encodes_like_the_converter():
    T = pytest.importorskip("lerobot_policy_piper.target")
    spec = T.make_spec(freqs=7, length_mm=28.0, mm_per_px=0.94, frame=(640, 480), with_valid=True,
                       state_names=NAMES)
    rng = np.random.default_rng(0)
    for u, v in rng.uniform(0, 1, size=(50, 2)):
        assert np.array_equal(W.encode((u, v), spec), T.encode((u, v), spec))
    assert np.array_equal(W.encode(None, spec), T.encode(None, spec))


def test_the_state_grows_by_the_target_slots_after_the_trained_robot_part():
    t = W.TargetInput(SPEC, NAMES + ["extra.pos"], expected_len=7 + 5)
    out = t.extend(np.arange(8, dtype=np.float32), (0.25, 0.5))
    assert out.shape == (12,) and np.array_equal(out[:7], np.arange(7))     # 학습한 앞부분만
    ph = 2 * np.pi * (1.5 * 0.25 - 0.5 * 0.5 * 0.75)
    assert np.allclose(out[7:], [1.0, 0.25, 0.5, np.cos(ph), np.sin(ph)])
    assert not t.extend(np.arange(7), None)[7:].any()                     # 목표 없음 = 0
    assert not t.needs_target


def test_a_different_robot_or_checkpoint_is_refused():
    with pytest.raises(ValueError, match="이름"):
        W.TargetInput(SPEC, ["gripper.pos"] + NAMES[:6], expected_len=12)
    with pytest.raises(ValueError, match="안 맞는다"):
        W.TargetInput(SPEC, NAMES, expected_len=14)
    with pytest.raises(ValueError, match="인코딩"):
        W.TargetInput({**SPEC, "encoding": "x"}, NAMES, expected_len=12)


def test_without_a_valid_slot_the_checkpoint_waits_for_a_target():
    spec = {**SPEC, "names": SPEC["names"][1:]}
    t = W.TargetInput(spec, NAMES, expected_len=11)
    assert t.needs_target
    with pytest.raises(ValueError, match="목표 없음"):
        t.extend(np.zeros(7), None)


# ── 2. 버스 값 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,want", [
    ([0.2, 0.7], [0.2, 0.7]), ((1.4, -0.1), [1.0, 0.0]), ({"u": 0.5, "v": 0.5}, [0.5, 0.5]),
    (None, None), ([], None),
])
def test_gateway_and_wrapper_read_targets_alike(value, want):
    assert target_value(value) == want
    got = W.parse_target(value)
    assert (got is None and want is None) or list(got) == want


@pytest.mark.parametrize("bad", [[0.1], [0.1, 0.2, 0.3], "0.1,0.2", [float("nan"), 0.1]])
def test_malformed_targets_are_dropped(bad):
    with pytest.raises((ValueError, TypeError)):
        target_value(bad)
    with pytest.raises((ValueError, TypeError)):
        W.parse_target(bad)
    safe, _ = ParamBridge(bus=None).validate_params({"target": bad, "task": "x"})
    assert "target" not in safe and safe["task"] == "x"


def test_a_cleared_target_still_goes_through():
    safe, _ = ParamBridge(bus=None).validate_params({"target": None})
    assert "target" in safe and safe["target"] is None


def test_the_params_endpoint_says_it_ignored_a_bad_target(monkeypatch):
    from app.routers import params as P

    sent = []

    async def fake_send(p):
        sent.append(p)

    monkeypatch.setattr(P.param_bridge, "send_params", fake_send)
    r = asyncio.run(P.update_params(P.ParamUpdate(params={"target": [3]})))
    assert r["applied"] == {} and any("목표 위치" in w for w in r["warnings"]) and not sent
    r = asyncio.run(P.update_params(P.ParamUpdate(params={"target": [0.3, 0.4]})))
    assert r["applied"] == {"target": [0.3, 0.4]} and sent[-1] == {"target": [0.3, 0.4]}


# ── 3. 판정 ─────────────────────────────────────────────────────────────

def _dataset(root: Path, repo: str, target: bool) -> Path:
    ds = root / repo
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text("{}")
    if target:
        (ds / "meta" / B.DATASET_META).write_text(json.dumps({"input": "rff_v1", "spec": SPEC}))
    return ds


def _ckpt(root: Path, repo: str | None) -> Path:
    c = root / "ckpt" / "pretrained_model"
    c.mkdir(parents=True)
    if repo is not None:
        (c / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": repo, "root": None}}))
    return c


@pytest.fixture()
def lerobot_home(tmp_path, monkeypatch):
    from app.core.config import settings

    home = tmp_path / "lerobot"
    monkeypatch.setattr(settings, "lerobot_dir", home)
    return home


def test_a_checkpoint_follows_its_training_dataset(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple_target_stage", target=True)
    info = B.resolve(_ckpt(tmp_path, "org/apple_target_stage"))
    assert info["input"] == "rff_v1" and info["source"] == "dataset" and info["spec"] == SPEC
    assert B.target_dims(info) == 5


def test_other_checkpoints_take_no_target(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple_stage", target=False)
    assert B.resolve(_ckpt(tmp_path, "org/apple_stage"))["input"] == "none"
    hub = tmp_path / "hub"
    hub.mkdir()
    assert B.resolve(hub)["input"] == "none"
    assert B.target_dims(B.resolve(hub)) == 0


def test_a_missing_target_dataset_blocks_instead_of_guessing(tmp_path, lerobot_home):
    assert B.resolve(_ckpt(tmp_path, "org/apple_target_stage"))["input"] == "unknown"


def test_the_marker_outlives_the_dataset(tmp_path, lerobot_home):
    import shutil

    ds = _dataset(lerobot_home, "org/apple_target", target=True)
    ck = _ckpt(tmp_path, "org/apple_target")
    assert B.remember(ck, B.resolve(ck))
    marker = json.loads((ck / B.CKPT_MARKER).read_text())
    assert W.same_spec(marker, SPEC)                                  # wrapper 가 이 표식과 인자를 대조한다
    shutil.rmtree(ds)
    again = B.resolve(ck)
    assert again["input"] == "rff_v1" and again["source"] == "marker" and again["spec"] == SPEC


def test_markers_are_not_written_into_the_hub_cache(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple_target", target=True)
    snap = tmp_path / "models--org--m" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": "org/apple_target"}}))
    assert not B.remember(snap, B.resolve(snap))
    assert not (snap / B.CKPT_MARKER).exists()


# ── 4. 추론 인자 ────────────────────────────────────────────────────────

def _args(ckpt: Path, mode="local"):
    from app.routers.models import InferencePreviewRequest, _build_args_for

    req = InferencePreviewRequest(checkpoint_path=str(ckpt), inference_mode=mode, robot_port="can0",
                                  policy_type="smolvla_s")
    return _build_args_for(req, "piper_follower", "can0")


def test_target_checkpoints_carry_their_spec(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple_target", target=True)
    args = _args(_ckpt(tmp_path, "org/apple_target"))
    assert json.loads(args[args.index("--target-spec") + 1]) == SPEC


def test_other_checkpoints_get_the_same_command_as_before(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple", target=False)
    assert "--target-spec" not in _args(_ckpt(tmp_path, "org/apple"))


def test_server_mode_and_unknown_are_refused(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple_target", target=True)
    with pytest.raises(HTTPException) as e:
        _args(_ckpt(tmp_path, "org/apple_target"), mode="server")
    assert e.value.status_code == 400 and "로컬" in e.value.detail
    with pytest.raises(HTTPException) as e:
        _args(_ckpt(tmp_path / "b", "org/gone_target"))
    assert e.value.status_code == 400


def _custom(ck: Path, extra: list[str]):
    from app.routers import models as M

    body = M.InferenceStartCustomRequest(args=["python", "lerobot_wrapper.py", "--policy-path", str(ck), *extra])
    with pytest.raises(HTTPException) as e:
        asyncio.run(M.start_inference_custom(body))
    return e.value


def test_edited_arguments_cannot_drop_or_change_the_spec(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/apple_target", target=True)
    _dataset(lerobot_home, "org/apple", target=False)
    ck = _ckpt(tmp_path, "org/apple_target")
    e = _custom(ck, [])
    assert e.status_code == 400 and "--target-spec 이 없습니다" in e.detail
    other = {**SPEC, "freqs": [[9.0, 9.0]]}
    e = _custom(ck, ["--target-spec", json.dumps(other)])
    assert e.status_code == 400 and "학습 사양과 다릅니다" in e.detail
    e = _custom(_ckpt(tmp_path / "b", "org/apple"), ["--target-spec", json.dumps(SPEC)])
    assert e.status_code == 400 and "받지 않는데" in e.detail


# ── 5. wrapper 순서·계약 ─────────────────────────────────────────────────

def test_the_wrapper_checks_targets_before_touching_the_robot():
    src = code_only((_REPO / "wrapper" / "lerobot_wrapper.py").read_text())
    connect = src.index("robot.connect()")
    assert src.index("_tmarker = Path(args.policy_path) / TARGET_MARKER") < connect
    assert src.index("_target_in = _target_input_for(") < connect, "연결한 뒤에 대조한다 — 실패하면 토크가 걸린 채 남는다"


def test_the_wrapper_extends_the_state_before_trimming_with_the_recorded_target():
    src = code_only((_REPO / "wrapper" / "lerobot_wrapper.py").read_text())
    extend = src.index("_target_in.extend(lerobot_obs[OBS_STATE], target)")
    assert extend < src.index("_expected_state_len = _state_dim.shape[0]"), "자른 뒤에 붙이면 목표가 잘려 나간다"
    assert "target_now = _current_target" in src
    assert "_prepare_observation(raw_obs, target_now)" in src, "모델에 넣은 목표와 기록한 목표가 달라진다"


def test_names_match_the_converter_and_the_wrapper():
    T = pytest.importorskip("lerobot_policy_piper.target")
    assert B.DATASET_META == T.META_FILE
    assert B.TARGET_V1 == T.ENCODING == W.TARGET_V1
    assert B.CKPT_MARKER == W.CKPT_MARKER
    assert T.VALID == W.VALID
    assert re.search(B._TARGET_NAME, "apple" + T.SUFFIX) and re.search(B._TARGET_NAME, "apple" + T.SUFFIX + "_stage")
