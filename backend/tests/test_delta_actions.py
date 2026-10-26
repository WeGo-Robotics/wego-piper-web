"""델타 액션 (feature/act-delta.md) — 추론 쪽 전부.

가장 위험한 사고는 **모드 불일치**다: 델타 체크포인트를 절대로 돌리면 팔이 영점 자세로 돌진하고, 절대를
델타로 돌리면 두 배 거리로 뛴다. 그래서 여기 테스트의 절반은 "고르지 않아도 맞게 실리는가, 어긋나면 막는가" 다.

1. 복원 수학 — wrapper/delta_action.py (변환기의 역과 같은 식인가)
2. 체크포인트 판정 — services/action_space.py (표식 > 학습 데이터셋 > 이름 > 절대)
3. 추론 인자 — 델타면 플래그가 실리고 절대면 명령이 한 글자도 안 바뀐다. 서버 모드·모름은 막는다
4. 변환 시작 API 의 거절
5. 이름·값 계약 — 백엔드가 다시 적은 것과 변환기·wrapper 가 같은가
"""

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
from delta_action import CKPT_MARKER as WRAPPER_MARKER  # noqa: E402
from delta_action import DeltaRestorer  # noqa: E402

from app.services import action_space as A  # noqa: E402

NAMES = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]
SPEC = {"delta_names": NAMES[:6], "clamp": {n: 5.0 for n in NAMES[:6]}}


# ── 1. 복원 ─────────────────────────────────────────────────────────────

def test_restore_adds_the_running_sum_to_the_anchor_and_leaves_the_gripper():
    r = DeltaRestorer(NAMES, NAMES[:6], {})
    chunk = np.zeros((3, 7))
    chunk[:, 0] = [1.0, 2.0, 3.0]
    chunk[:, 6] = [40.0, 41.0, 42.0]                    # 그리퍼 — 절대
    out = r.restore(chunk, np.full(6, 10.0))
    assert out[:, 0].tolist() == [11.0, 13.0, 16.0]
    assert out[:, 1].tolist() == [10.0, 10.0, 10.0]
    assert out[:, 6].tolist() == [40.0, 41.0, 42.0]


def test_the_clamp_cuts_each_step_before_summing():
    r = DeltaRestorer(NAMES, NAMES[:6], {"joint1.pos": 2.0})
    chunk = np.zeros((3, 7))
    chunk[:, 0] = [10.0, -10.0, 1.0]
    out = r.restore(chunk, np.zeros(6))
    assert out[:, 0].tolist() == [2.0, 0.0, 1.0]
    assert r.clipped_steps == 2


def test_channels_are_found_by_name_not_position():
    """양팔·다른 로봇은 순서가 다르다 — 순서로 맞추면 엉뚱한 관절에 더한다."""
    names = ["gripper.pos", "joint2.pos", "joint1.pos"]
    r = DeltaRestorer(names, ["joint1.pos", "joint2.pos"], {})
    out = r.restore(np.array([[50.0, 1.0, 2.0]]), r.anchor({"joint1.pos": 10.0, "joint2.pos": 20.0}))
    assert out[0].tolist() == [50.0, 21.0, 12.0]
    with pytest.raises(ValueError, match="없다"):
        DeltaRestorer(names, ["left_joint1.pos"], {})


def test_cutting_the_chunk_before_restoring_changes_nothing():
    """wrapper 는 청크를 앞에서 자른 뒤 되돌린다. 누적합은 앞쪽 항만 보므로 순서가 결과를 안 바꾼다."""
    rng = np.random.default_rng(0)
    chunk, anchor = rng.normal(size=(50, 7)), rng.normal(size=6)
    r = DeltaRestorer(NAMES, NAMES[:6], {})
    assert np.allclose(r.restore(chunk, anchor)[:20], r.restore(chunk[:20], anchor))


def test_inference_restores_with_the_converters_inverse():
    """추론의 복원이 변환기가 검증에 쓰는 역변환과 같은 식이어야 한다 — 다르면 학습한 것과 다른 궤적이 나온다."""
    D = pytest.importorskip("lerobot_policy_piper.delta")
    rng = np.random.default_rng(1)
    a, s0 = rng.normal(size=(30, 7)) * 10, rng.normal(size=6)
    d = D.to_delta(a, s0, list(range(6)))
    out = DeltaRestorer(NAMES, NAMES[:6], {}).restore(d, s0)
    assert np.allclose(out, D.from_delta(d, s0, list(range(6))))
    assert np.allclose(out, a)


# ── 2. 판정 ─────────────────────────────────────────────────────────────

def _dataset(root: Path, repo: str, delta: bool) -> Path:
    ds = root / repo
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text("{}")
    if delta:
        (ds / "meta" / A.DATASET_META).write_text(json.dumps(
            {"action_space": "delta_v1", "source_repo_id": "org/x", **SPEC}))
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
    _dataset(lerobot_home, "org/cube_delta", delta=True)
    info = A.resolve(_ckpt(tmp_path, "org/cube_delta"))
    assert info["action_space"] == "delta_v1" and info["source"] == "dataset"
    assert info["spec"] == SPEC


def test_existing_checkpoints_stay_absolute(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/cube", delta=False)
    assert A.resolve(_ckpt(tmp_path, "org/cube"))["action_space"] == "absolute"
    hub = tmp_path / "hub"
    hub.mkdir()
    assert A.resolve(hub)["action_space"] == "absolute"            # train_config 없음


def test_a_missing_delta_dataset_blocks_instead_of_guessing(tmp_path, lerobot_home):
    """데이터셋이 지워졌는데 이름이 `_delta` 면 모른다 — 절대로 짐작하면 팔이 영점으로 돌진한다."""
    assert A.resolve(_ckpt(tmp_path, "org/cube_delta_stage"))["action_space"] == "unknown"
    assert "돌진" in A.unknown_message({"dataset": "org/cube_delta"})


def test_the_marker_outlives_the_dataset(tmp_path, lerobot_home):
    ds = _dataset(lerobot_home, "org/cube_delta", delta=True)
    ck = _ckpt(tmp_path, "org/cube_delta")
    info = A.resolve(ck)
    assert A.remember(ck, info)
    import shutil

    shutil.rmtree(ds)                                              # 데이터셋을 지웠다
    again = A.resolve(ck)
    assert again["action_space"] == "delta_v1" and again["source"] == "marker" and again["spec"] == SPEC


def test_markers_are_not_written_into_the_hub_cache(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/cube_delta", delta=True)
    snap = tmp_path / "models--org--m" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": "org/cube_delta"}}))
    assert not A.remember(snap, A.resolve(snap))
    assert not (snap / A.CKPT_MARKER).exists()


# ── 3. 추론 인자 ────────────────────────────────────────────────────────

def _args(ckpt: Path, mode="local"):
    from app.routers.models import InferencePreviewRequest, _build_args_for

    req = InferencePreviewRequest(checkpoint_path=str(ckpt), inference_mode=mode, robot_port="can0",
                                  policy_type="act")
    return _build_args_for(req, "piper_follower", "can0")


def test_delta_checkpoints_carry_their_flags(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/cube_delta", delta=True)
    args = _args(_ckpt(tmp_path, "org/cube_delta"))
    assert args[args.index("--action-space") + 1] == "delta_v1"
    assert json.loads(args[args.index("--delta-spec") + 1]) == SPEC


def test_absolute_checkpoints_get_the_same_command_as_before(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/cube", delta=False)
    args = _args(_ckpt(tmp_path, "org/cube"))
    assert "--action-space" not in args and "--delta-spec" not in args


def test_server_mode_and_unknown_are_refused(tmp_path, lerobot_home):
    _dataset(lerobot_home, "org/cube_delta", delta=True)
    with pytest.raises(HTTPException) as e:
        _args(_ckpt(tmp_path, "org/cube_delta"), mode="server")
    assert e.value.status_code == 400 and "로컬" in e.value.detail
    with pytest.raises(HTTPException) as e:
        _args(_ckpt(tmp_path / "b", "org/gone_delta"))
    assert e.value.status_code == 400


def test_edited_arguments_cannot_drop_the_delta_flag(tmp_path, lerobot_home):
    """CLI 편집으로 플래그를 지우면 델타 체크포인트가 절대로 돈다 — 시작 전에 막는다."""
    import asyncio

    from app.routers import models as M

    _dataset(lerobot_home, "org/cube_delta", delta=True)
    ck = _ckpt(tmp_path, "org/cube_delta")
    body = M.InferenceStartCustomRequest(args=["python", "lerobot_wrapper.py", "--policy-path", str(ck)])
    with pytest.raises(HTTPException) as e:
        asyncio.run(M.start_inference_custom(body))
    assert e.value.status_code == 400 and "어긋" in e.value.detail


def test_the_wrapper_checks_the_marker_before_touching_the_robot():
    src = code_only((_REPO / "wrapper" / "lerobot_wrapper.py").read_text())
    check = src.index("_marker_path = Path(args.policy_path) / CKPT_MARKER")
    assert check < src.index("robot.connect()"), "로봇을 연결한 뒤에 대조한다 — 실패하면 토크가 걸린 채 남는다"
    assert "raise SystemExit" in src[check:src.index("robot.connect()")]


def test_the_wrapper_restores_before_queueing_with_the_observation_it_inferred_on():
    src = code_only((_REPO / "wrapper" / "lerobot_wrapper.py").read_text())
    restore = src.index("_delta.restore(")
    assert "_delta.anchor(raw_obs)" in src[restore:restore + 200], "앵커가 그 추론에 넣은 관측이 아니다"
    assert restore < src.index("_action_queue = new_queue"), "큐에 넣은 뒤에 되돌린다"
    assert restore < src.index("_ensembler.add(obs_step"), "앙상블에 델타가 들어간다"


# ── 4. 변환 시작 API ────────────────────────────────────────────────────

def test_convert_endpoint_refuses_what_it_should(tmp_path, lerobot_home, monkeypatch):
    import asyncio

    from app.routers import datasets as R

    started = []

    async def fake_start(args, **_):
        started.append(args)

    monkeypatch.setattr(R._edit_pm, "start", fake_start)
    monkeypatch.setattr(R, "require_idle", lambda *_: None)

    _dataset(lerobot_home, "org/done_delta", delta=True)
    baked = _dataset(lerobot_home, "org/cube_stage", delta=False)
    (baked / "meta" / "stage_bake.json").write_text("{}")
    _dataset(lerobot_home, "org/twice", delta=False)
    _dataset(lerobot_home, "org/twice_delta", delta=True)
    _dataset(lerobot_home, "org/fresh", delta=False)

    def call(ds, force=False):
        return asyncio.run(R.convert_to_delta(ds, R.DeltaRequest(force=force)))

    for ds, code in (("org/done_delta", 400), ("org/cube_stage", 400), ("org/twice", 409), ("org/none", 404)):
        with pytest.raises(HTTPException) as e:
            call(ds)
        assert e.value.status_code == code, ds
    assert not started

    r = call("org/fresh")
    assert r["output_id"] == "org/fresh_delta"
    assert started[-1][-4:] == ["lerobot_policy_piper.delta", str(lerobot_home / "org" / "fresh"),
                                "--out", str(lerobot_home / "org" / "fresh_delta")]
    call("org/twice", force=True)
    assert started[-1][-1] == "--force"


def test_the_dataset_list_marks_delta_copies(tmp_path):
    from app.services.dataset_scanner import delta_info

    ds = _dataset(tmp_path, "org/cube_delta", delta=True)
    assert delta_info(ds)["delta_names"] == NAMES[:6]
    assert delta_info(_dataset(tmp_path, "org/cube", delta=False)) is None


# ── 5. 계약 ─────────────────────────────────────────────────────────────

def test_names_match_the_converter_and_the_wrapper():
    D = pytest.importorskip("lerobot_policy_piper.delta")
    assert A.DATASET_META == D.META_FILE
    assert A.DELTA_V1 == D.ACTION_SPACE
    assert A.CKPT_MARKER == WRAPPER_MARKER
    # 이름 가드가 변환기의 출력 이름을 알아본다 — 굽기까지 한 것도
    assert re.search(A._DELTA_NAME, "cube" + D.SUFFIX) and re.search(A._DELTA_NAME, "cube" + D.SUFFIX + "_stage")


def test_edit_status_says_why_it_failed():
    from app.routers import datasets as R

    src = code_only(Path(R.__file__).read_text())
    status = src[src.index("async def edit_status"):src.index("@router.get(\"/hf-cli\")")]
    assert "recent_log" in status
