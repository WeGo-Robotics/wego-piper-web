"""이어 학습 base 의 길이 맞춤 + 어긋난 체크포인트 검증 (2026-10-14).

실화: `lerobot/smolvla_base`(state 6)를 7축 데이터셋으로 이어 학습했더니 학습은 끝까지 갔고,
체크포인트가 **state 6 / 통계 7** 로 어긋나 저장돼 추론이 `(6) must match (7)` 로 죽었다.
세 군데를 막는다 — ① 로컬 학습(`test_training_runner.py`) ② 원격 학습(여기) ③ 추론 전 검증(여기).
"""

import ast
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import base_align
from app.core.cli_mapping import checkpoint_dim_problem
from app.main import app
from app.services.robot_manager import ArmInfo
from app.services.training.runners.ssh import _ALIGN_EOF, SSHRunner
from app.services.training.spec import TrainJobSpec


@pytest.fixture
def client():
    return TestClient(app)


# ── 도우미 ──────────────────────────────────────────────────────────────────


def _base(root: Path, *, state=6, action=6, padded=True) -> Path:
    d = root / "base"
    d.mkdir(parents=True)
    cfg = {"input_features": {"observation.state": {"type": "STATE", "shape": [state]}},
           "output_features": {"action": {"type": "ACTION", "shape": [action]}}}
    if padded:
        cfg.update(max_state_dim=32, max_action_dim=32)
    (d / "config.json").write_text(json.dumps(cfg))
    (d / "model.safetensors").write_text("WEIGHTS")
    return d


def _dataset(root: Path, state=7, action=7) -> Path:
    d = root / "ds"
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({"features": {
        "observation.state": {"shape": [state]}, "action": {"shape": [action]}}}))
    return d


def _checkpoint(root: Path, *, cfg_state: int, stats_state: int, action: int = 7) -> Path:
    """`config.json` 과 정규화 통계 헤더만 있는 체크포인트."""
    d = root / "ckpt"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({
        "input_features": {"observation.state": {"shape": [cfg_state]}},
        "output_features": {"action": {"shape": [action]}}}))
    header = {
        "observation.state.mean": {"dtype": "F32", "shape": [stats_state],
                                   "data_offsets": [0, 4 * stats_state]},
        "action.mean": {"dtype": "F32", "shape": [action],
                        "data_offsets": [4 * stats_state, 4 * (stats_state + action)]},
    }
    raw = json.dumps(header).encode()
    (d / "policy_preprocessor_step_5_normalizer_processor.safetensors").write_bytes(
        struct.pack("<Q", len(raw)) + raw + b"\0" * (4 * (stats_state + action)))
    return d


# ── base_align 자체 ─────────────────────────────────────────────────────────


def test_base_align_uses_only_the_standard_library():
    """⚠ 이 파일은 원격 스크립트에 원문 그대로 실려 간다 — 임대 서버에는 `app.*` 가 없다."""
    tree = ast.parse(Path(base_align.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    # huggingface_hub 는 원격 진입점 안에서만 늦게 불린다 (로컬 판정 경로는 안 쓴다)
    assert modules <= {"hashlib", "json", "re", "sys", "pathlib", "huggingface_hub"}, modules
    assert _ALIGN_EOF not in Path(base_align.__file__).read_text()


def test_align_gives_the_base_back_when_nothing_needs_doing(tmp_path):
    base, ds = _base(tmp_path, padded=False), _dataset(tmp_path)
    path, note = base_align.align(base, json.loads((ds / "meta/info.json").read_text()),
                                  tmp_path / "out")
    assert path == str(base) and note == ""
    assert not (tmp_path / "out").exists()


# ── 원격: 생성된 스크립트를 **실제로 bash 로 돌린다** ─────────────────────────


def _run_script(tmp_path: Path, base: str, dataset: str) -> subprocess.CompletedProcess:
    """원격에서 도는 것과 같은 스크립트. 마지막 명령만 받은 인자를 찍는 것으로 바꿨다."""
    spec = TrainJobSpec(cmd=[sys.executable, "-c", "import sys; print('ARGS', *sys.argv[1:])",
                             f"--policy.path={base}", f"--dataset.repo_id={dataset}",
                             "--steps=10"], total_steps=10)
    script = SSHRunner(host="d")._build_script(spec)
    f = tmp_path / "run.sh"
    f.write_text(script)
    return subprocess.run(["bash", str(f)], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path / "tmp")})


def test_remote_script_trains_from_the_aligned_copy(tmp_path):
    (tmp_path / "tmp").mkdir()
    base, ds = _base(tmp_path), _dataset(tmp_path)
    r = _run_script(tmp_path, str(base), str(ds))
    assert r.returncode == 0, r.stderr
    args = next(ln for ln in r.stdout.splitlines() if ln.startswith("ARGS "))
    used = next(a for a in args.split() if a.startswith("--policy.path=")).split("=", 1)[1]
    assert used != str(base) and used.startswith(str(tmp_path / "tmp" / "piper_derived"))
    cfg = json.loads((Path(used) / "config.json").read_text())
    assert cfg["input_features"]["observation.state"]["shape"] == [7]
    assert cfg["output_features"]["action"]["shape"] == [7]
    # 다른 인자는 그대로, base 는 한 글자도 안 바뀐다
    assert "--steps=10" in args
    assert json.loads((base / "config.json").read_text())["input_features"][
        "observation.state"]["shape"] == [6]
    assert "[piper]" in r.stderr, "맞췄다는 사실이 학습 로그에 남아야 한다"


def test_remote_script_still_trains_when_alignment_fails(tmp_path):
    """⚠ 맞춤이 실패해도 학습은 건다 — 대신 **이유를 로그에 남긴다**."""
    (tmp_path / "tmp").mkdir()
    base = _base(tmp_path)
    broken = tmp_path / "ds"          # info.json 이 없는 데이터셋
    broken.mkdir()
    r = _run_script(tmp_path, str(base), str(broken))
    assert r.returncode == 0, r.stderr
    args = next(ln for ln in r.stdout.splitlines() if ln.startswith("ARGS "))
    assert f"--policy.path={base}" in args, "실패하면 base 를 그대로 써야 한다"
    assert "건너뜁니다" in r.stderr


def test_script_without_a_base_has_no_alignment_step():
    script = SSHRunner(host="d")._build_script(TrainJobSpec(
        cmd=["python", "-m", "lerobot.scripts.lerobot_train", "--dataset.repo_id=org/ds"]))
    assert "piper-align" not in script and "PIPER_POLICY_PATH" not in script


def test_exit_marker_still_follows_the_command_directly():
    """`$?` 가 학습 명령의 종료 코드이려면 사이에 아무것도 끼면 안 된다."""
    script = SSHRunner(host="d")._build_script(TrainJobSpec(
        cmd=["python", "-m", "x", "--policy.path=lerobot/smolvla_base", "--dataset.repo_id=org/ds"]))
    lines = [ln for ln in script.splitlines() if ln.strip()]
    i = next(i for i, ln in enumerate(lines) if ln.startswith("python -m x "))
    assert '"--policy.path=$PIPER_POLICY_PATH"' in lines[i]
    assert lines[i + 1].startswith('echo "__PIPER_EXIT__')


def test_restore_does_not_mistake_the_alignment_step_for_the_command(monkeypatch):
    """`restore()` 는 스크립트를 다시 읽어 명령을 되찾는다 — 파이썬 소스가 명령이 되면 안 된다."""
    from app.services.training.runners import ssh as S

    script = SSHRunner(host="d")._build_script(TrainJobSpec(
        cmd=["python", "-m", "x", "--policy.path=lerobot/smolvla_base", "--dataset.repo_id=org/ds",
             "--steps=10"], total_steps=10, output_dir="/out/x"))

    class _R:
        returncode, stderr = 0, ""
        stdout = script

    monkeypatch.setattr(S, "_run", lambda *a, **k: _R())
    monkeypatch.setattr(S.SSHRunner, "_start_log_stream", lambda self, **kw: None)
    spec = S.SSHRunner(job_id="t", host="u@h").restore()
    assert spec is not None
    assert spec.total_steps == 10 and spec.output_dir == "/out/x"
    assert spec.cmd[:3] == ["python", "-m", "x"] and "--steps=10" in spec.cmd


# ── 추론 전 검증 ────────────────────────────────────────────────────────────


def test_a_checkpoint_whose_config_and_stats_disagree_is_a_problem(tmp_path):
    ckpt = _checkpoint(tmp_path, cfg_state=6, stats_state=7)
    msg = checkpoint_dim_problem(str(ckpt))
    assert msg and "observation.state" in msg and "6" in msg and "7" in msg


def test_a_consistent_checkpoint_is_fine(tmp_path):
    assert checkpoint_dim_problem(str(_checkpoint(tmp_path, cfg_state=7, stats_state=7))) is None


def test_what_cannot_be_checked_is_let_through(tmp_path):
    """통계 텐서가 없는 옛 체크포인트·없는 경로를 막으면 멀쩡한 모델이 못 돈다."""
    bare = tmp_path / "old"
    bare.mkdir()
    (bare / "config.json").write_text(json.dumps({
        "input_features": {"observation.state": {"shape": [6]}},
        "output_features": {"action": {"shape": [6]}}}))
    assert checkpoint_dim_problem(str(bare)) is None
    assert checkpoint_dim_problem(str(tmp_path / "nope")) is None
    assert checkpoint_dim_problem("") is None


def test_a_model_trained_without_the_gripper_is_not_blocked(tmp_path):
    """config 6 / 통계 6 — 래퍼가 7축 로봇의 state 를 6으로 자르는 **정상** 경우다."""
    assert checkpoint_dim_problem(str(_checkpoint(tmp_path, cfg_state=6, stats_state=6, action=6))) is None


def _ready_follower(monkeypatch):
    from app.services import robot_manager as rm

    a = ArmInfo(iface="can1")
    a.role, a.ready = "follower", True
    monkeypatch.setattr(rm.robot_manager, "arms", {"can1": a})


def test_validate_reports_the_mismatch_as_an_error(client, monkeypatch, tmp_path):
    from app.routers import inference as inf

    ckpt = _checkpoint(tmp_path, cfg_state=6, stats_state=7)
    monkeypatch.setattr(inf, "get_model", lambda mid: {
        "path": str(ckpt),
        "requirements": {"required_cameras": [], "state_dim": 6, "action_dim": 7}})
    _ready_follower(monkeypatch)
    body = client.post("/api/inference/validate", json={
        "model_id": "m", "follower_iface": "can1", "camera_mapping": {}}).json()
    assert body["valid"] is False
    assert any("어긋나" in e for e in body["errors"]), body["errors"]


def test_start_refuses_the_mismatch_without_going_through_the_screen(client, tmp_path):
    ckpt = _checkpoint(tmp_path, cfg_state=6, stats_state=7)
    r = client.post("/api/models/inference/start", json={"checkpoint_path": str(ckpt)})
    assert r.status_code == 400 and "어긋나" in r.json()["detail"]


@pytest.mark.parametrize("form", ["space", "equals"])
def test_start_custom_reads_the_checkpoint_out_of_hand_edited_args(client, tmp_path, form):
    ckpt = _checkpoint(tmp_path, cfg_state=6, stats_state=7)
    args = ["python", "-u", "w.py"] + (
        ["--policy-path", str(ckpt)] if form == "space" else [f"--policy-path={ckpt}"])
    r = client.post("/api/models/inference/start-custom", json={"args": args})
    assert r.status_code == 400 and "어긋나" in r.json()["detail"]
