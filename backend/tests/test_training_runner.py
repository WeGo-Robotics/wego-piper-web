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


# ── 로스 곡선 ───────────────────────────────────────────────────────────────


def test_the_same_step_does_not_become_a_vertical_line():
    """⚠ **실기에서 그래프가 톱니로 보였다.** LeRobot 은 step 을 `step:25K` 처럼
    **1000 단위로 반올림해** 찍는데 `log_freq` 는 그보다 작다 — 실측으로
    `step:25K` 가 연속 5번 나왔다. 그대로 쌓으면 같은 x 에 서로 다른 loss 가
    찍혀 **수직선**이 서고, 사람이 보기엔 학습이 발산하는 것처럼 보인다.
    실제 히스토리는 50점 중 39점이 중복이었다.
    """
    from app.services.training.metrics import TrainHistory

    h = TrainHistory()
    for loss in (0.10, 0.09, 0.11, 0.08, 0.095):
        h.append(25000, loss, 1.0, 1e-5)
    for loss in (0.088, 0.092):
        h.append(26000, loss, 1.0, 1e-5)

    assert h.steps == [25000, 26000], f"중복 step 이 남았다: {h.steps}"
    # 같은 구간에서는 **마지막 값**을 남긴다 — 화면의 "현재 loss" 와 같은 값이다
    assert h.losses == [0.095, 0.092]


def test_the_curve_never_goes_backwards():
    """x 가 뒤로 가면 선이 되짚어 그어진다 — 곡선이 아니라 그물이 된다.

    ⚠ 게이트웨이를 재시작하면 저널을 **처음부터 다시 읽어** 히스토리를 새로
    쌓는다(재부착). 그때 옛 줄이 새 줄 뒤에 붙으면 정확히 이 모양이 된다.
    """
    from app.services.training.metrics import TrainHistory

    h = TrainHistory()
    for st in (1000, 2000, 3000, 2000, 1000, 4000):
        h.append(st, 0.1, 1.0, 1e-5)
    assert all(b > a for a, b in zip(h.steps, h.steps[1:])), f"뒤로 간다: {h.steps}"


def test_the_k_suffix_is_read_as_thousands():
    """`step:38K` 는 38000 이다. 이걸 38 로 읽으면 그래프가 원점에 뭉친다."""
    from app.services.training.metrics import METRIC_RE, parse_num

    line = ("INFO 2026-09-01 10:58:26 ot_train.py:439 step:38K smpl:301K ep:854 "
            "epch:17.09 loss:0.092 grdn:6.860 lr:1.0e-05 updt_s:0.068 data_s:0.011")
    m = METRIC_RE.search(line)
    assert m, "실기 로그 줄을 못 읽는다"
    assert parse_num(m.group(1)) == 38000
    assert float(m.group(5)) == 0.092


def test_reattach_keeps_the_replayed_history():
    """⚠ **재부착이 채운 것을 그 직후에 지우고 있었다.**

    `runner.restore()` 는 저널을 처음부터 다시 읽는 스레드를 띄운다 — 게이트웨이가
    없던 동안의 진행을 화면에 채우려는 것이다. 그런데 그 뒤에 `tracker.reset()` 을
    부르면 방금 채운 히스토리가 통째로 사라진다. 실측으로 재시작 뒤 로스 곡선에
    점이 2개만 남았다.

    비우는 것은 **재부착보다 먼저**여야 하고, 총 스텝은 지우지 않고 채워야 한다.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "training"
           / "manager.py").read_text()
    body = src.split("def restore_running_process", 1)[1].split("\n    def ", 1)[0]
    assert body.index("self.tracker.reset()") < body.index("self.runner.restore()"), \
        "재부착 뒤에 비운다 — 재생된 히스토리가 사라진다"
    assert "reset(total_steps=" not in body, "총 스텝을 넣으면서 다시 비운다"


def test_the_curve_loads_without_waiting_for_the_state():
    """⚠ **곡선이 서버에 있는데 화면이 안 가져왔다.**

    히스토리 폴링이 `trainState === 'running'` 일 때만 돌았다. 화면을 열면 그 값은
    아직 `running` 이 아니라(상태를 받아오기 전이다) **한 번도 안 가져온다** —
    학습 도중에 들어가면 그래프가 한참 비어 있었고, 학습이 끝난 뒤에 열면 영영
    비어 있었다. 저장까지 고쳐 놨는데 화면이 안 읽으면 소용이 없다.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "TrainingPage.tsx").read_text()
    # 상태와 무관한 마운트 시 조회가 있어야 한다
    assert "useEffect(() => { fetchHistory() }, [fetchHistory])" in src, \
        "열자마자 한 번 가져오지 않는다"
    # 폴링 자체는 여전히 학습 중에만 (끝난 뒤 5초마다 두드릴 이유가 없다)
    poll = src.split("if (trainState === 'running')", 1)[1][:200]
    assert "setInterval" in poll, "학습 중 주기 갱신이 사라졌다"
