"""학습 러너 이음매 + 인자 조립 (feature/cloud-training.md 0~2단계).

원래 문제: `TrainManager` 가 곧 `ProcessManager` 였다 — `state`/`is_running`/`stop` 이
전부 로컬 subprocess 에 위임됐고, 원격 job 에는 PID 도 SIGTERM 도 stdout 파이프도 없다.
"""

import json

import pytest

from app.core.cli_mapping import (
    apply_dim_overrides, build_train_args, checkpoint_feature_dims, resolve_rename_map,
)
from app.services.training import TrainJobSpec, train_manager
from app.services.training.metrics import MetricsTracker
from app.services.training.runners.base import TrainRunner
from app.services.training.runners.local import LocalRunner


# ── 메트릭: 실행 방식과 무관해야 한다 ──

_LOG = ("step:200 smpl:12.8K ep:106 epch:1.07 loss:0.342 grdn:2.157 "
        "lr:1.0e-03 updt_s:0.456 data_s:0.012")


def test_metrics_need_only_a_log_line():
    """로그 한 줄만 있으면 된다 — 로컬이든 SSH 든 같은 코드를 쓴다."""
    t = MetricsTracker()
    t.reset(total_steps=1000)
    assert t.feed(_LOG) is True
    assert t.metrics.step == 200
    assert t.metrics.loss == pytest.approx(0.342)
    assert t.progress() == pytest.approx(0.2)
    assert t.history.steps == [200]


def test_metrics_ignore_other_lines():
    t = MetricsTracker()
    assert t.feed("INFO 아무 로그") is False
    assert t.metrics.step == 0


def test_k_suffix_after_1000_steps():
    t = MetricsTracker()
    t.feed(_LOG.replace("step:200", "step:1.2K"))
    assert t.metrics.step == 1200


def test_history_is_bounded():
    """5000 포인트를 넘으면 오래된 절반을 버린다 (메모리 제한)."""
    t = MetricsTracker()
    t.history.max_points = 10
    for i in range(30):
        t.history.append(i, 0.0, 0.0, 0.0)
    assert len(t.history.steps) <= 10


# ── 러너 이음매 ──

def test_local_runner_satisfies_protocol():
    """`SSHRunner`/`SystemdRunner` 가 나란히 붙을 수 있어야 한다."""
    r: TrainRunner = LocalRunner()
    for attr in ("state", "is_running", "pid", "set_log_callback",
                 "set_state_callback", "start", "stop", "restore"):
        assert hasattr(r, attr), f"{attr} 누락"


def test_manager_exposes_no_process_manager():
    """`TrainManager` 는 실행 방식을 몰라야 한다 — `.pm` 이 다시 생기면 안 된다."""
    assert not hasattr(train_manager, "pm")
    assert hasattr(train_manager, "runner")


def test_spec_carries_env():
    """AMP 는 CLI 인자가 아니라 환경변수다 — `cmd` 만 받으면 조용히 빠진다."""
    spec = TrainJobSpec(cmd=["x"], env={"ACCELERATE_MIXED_PRECISION": "bf16"})
    assert spec.env["ACCELERATE_MIXED_PRECISION"] == "bf16"


# ── 인자 조립: 순수해야 한다 ──

@pytest.fixture
def fake_checkpoint(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "input_features": {"observation.state": {"shape": [7]}},
        "output_features": {"action": {"shape": [7]}},
    }))
    (tmp_path / "policy_preprocessor.json").write_text(json.dumps({
        "steps": [{
            "registry_name": "rename_observations_processor",
            "config": {"rename_map": {"top": "observation.images.top"}},
        }]
    }))
    return tmp_path


def test_build_train_args_is_pure(fake_checkpoint):
    """⚠ 이전에는 `build_train_args()` 가 체크포인트의 config.json 을 **수정했다** —
    그래서 `/training/preview`(미리보기)도 체크포인트를 바꿨다."""
    before = (fake_checkpoint / "config.json").read_text()
    build_train_args({
        "dataset_repo_id": "org/ds",
        "pretrained_path": str(fake_checkpoint),
        "state_dim": 8, "action_dim": 8,
    })
    assert (fake_checkpoint / "config.json").read_text() == before


def test_interpreter_is_injectable():
    """원격은 로컬 conda 경로가 아니다."""
    args = build_train_args({"dataset_repo_id": "org/ds"}, python="/remote/bin/python")
    assert args[0] == "/remote/bin/python"


def test_resolve_rename_map_reads_only(fake_checkpoint):
    rm = resolve_rename_map(str(fake_checkpoint))
    assert json.loads(rm) == {"top": "observation.images.top"}
    args = build_train_args({"dataset_repo_id": "org/ds"}, rename_map=rm)
    assert any(a.startswith("--rename_map=") for a in args)


def test_resolve_rename_map_missing(tmp_path):
    assert resolve_rename_map(str(tmp_path)) == ""


def test_apply_dim_overrides_is_the_only_writer(fake_checkpoint):
    assert apply_dim_overrides(str(fake_checkpoint), 8, 8) is True
    cfg = json.loads((fake_checkpoint / "config.json").read_text())
    assert cfg["input_features"]["observation.state"]["shape"] == [8]
    assert cfg["output_features"]["action"]["shape"] == [8]
    # 차원을 안 주면 아무것도 안 한다
    assert apply_dim_overrides(str(fake_checkpoint), 0, 0) is False


def _write_normalizer(ckpt, state_dim, action_dim):
    """safetensors 헤더만 있으면 된다 — 8바이트 길이 + JSON + (빈) 데이터."""
    import struct

    header = {
        "observation.state.mean": {"dtype": "F32", "shape": [state_dim], "data_offsets": [0, 4 * state_dim]},
        "action.mean": {"dtype": "F32", "shape": [action_dim],
                        "data_offsets": [4 * state_dim, 4 * (state_dim + action_dim)]},
    }
    raw = json.dumps(header).encode()
    (ckpt / "policy_preprocessor_step_3_normalizer_processor.safetensors").write_bytes(
        struct.pack("<Q", len(raw)) + raw + b"\0" * (4 * (state_dim + action_dim))
    )


def test_apply_dim_overrides_refuses_dims_that_differ_from_weights(fake_checkpoint):
    """실화(2026-08-28): 프론트 localStorage 의 옛 state_dim=7 이 14차원 두 팔 체크포인트의
    config.json 을 7로 덮어써 이어학습이 size mismatch 로 매번 죽었다."""
    _write_normalizer(fake_checkpoint, 14, 14)
    assert checkpoint_feature_dims(str(fake_checkpoint)) == {"observation.state": 14, "action": 14}
    before = (fake_checkpoint / "config.json").read_text()
    assert apply_dim_overrides(str(fake_checkpoint), 7, 7) is False
    assert (fake_checkpoint / "config.json").read_text() == before
    # 가중치와 같은 값은 여전히 쓴다 (config.json 이 이미 오염된 체크포인트 복구 경로)
    assert apply_dim_overrides(str(fake_checkpoint), 14, 14) is True
    cfg = json.loads((fake_checkpoint / "config.json").read_text())
    assert cfg["input_features"]["observation.state"]["shape"] == [14]


def test_pretrained_skips_policy_type():
    """`--policy.path` 와 `--policy.type` 은 동시 사용 불가."""
    args = build_train_args({"pretrained_path": "/m", "policy_type": "act"})
    assert not any(a.startswith("--policy.type=") for a in args)
