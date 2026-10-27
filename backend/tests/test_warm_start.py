"""워밍스타트 — 다른 정책의 체크포인트에서 이어 학습한다 (feature/smolvla-s.md §6).

SmolVLA-S 는 처음부터만 학습됐다. `lerobot/smolvla_base` 는 config `type: smolvla` 라 그대로
`--policy.path` 로 주면 LeRobot 이 type 을 체크포인트에서 가져와 **바닐라 SmolVLA 로** 학습한다.
type 만 바꾼 사본으로 열면 가중치는 그대로 실리고 stage 헤드만 무작위로 시작한다
(실측: smolvla_base 505개 중 500개, ACT 239개 중 234개 — 빈 것은 stage 헤드뿐).

여기서 지키는 것:
1. 허용된 타입에서만 바꾸고, 새 정책이 더한 필드만 채운다 — base 의 아키텍처 값은 base 가 이긴다
2. 길이 맞춤과 한 사본에 담기고, 길이 맞춤만 한 사본과 자리가 갈린다
3. 데이터셋 길이를 못 읽어도 워밍스타트는 한다 (빠지면 바닐라가 학습된다)
4. 원격은 환경변수로 같은 지시를 받는다 — 생성된 학습 스크립트를 **실제로 bash 로 돌려** 본다
5. 학습 시작·직접 편집·클라우드 세 길이 모두 지시를 싣는다
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import base_align
from app.core.cli_mapping import align_pretrained_to_dataset, warm_start_env, warm_start_plan
from app.services.training.runners.ssh import SSHRunner
from app.services.training.spec import TrainJobSpec

PLAN = {"to": "smolvla_s", "from": ["smolvla"],
        "fill": {"stage_loss_weight": 0.2, "chunk_size": 999}}


def _base(root: Path, *, type_="smolvla", state=6, action=6, padded=True) -> Path:
    d = root / "base"
    d.mkdir(parents=True)
    cfg = {"type": type_, "chunk_size": 50,
           "input_features": {"observation.state": {"type": "STATE", "shape": [state]}},
           "output_features": {"action": {"type": "ACTION", "shape": [action]}}}
    if padded:
        cfg.update(max_state_dim=32, max_action_dim=32)
    (d / "config.json").write_text(json.dumps(cfg))
    (d / "model.safetensors").write_text("WEIGHTS")
    return d


def _info(state=7, action=7) -> dict:
    return {"features": {"observation.state": {"shape": [state]}, "action": {"shape": [action]}}}


def _cfg(path: str) -> dict:
    return json.loads((Path(path) / "config.json").read_text())


# ── 1. 판정 ─────────────────────────────────────────────────────────────────

def test_only_the_allowed_type_is_reopened_and_only_new_fields_are_filled():
    cfg = {"type": "smolvla", "chunk_size": 50}
    new = base_align.retyped_config(cfg, PLAN)
    assert new["type"] == "smolvla_s"
    assert new["stage_loss_weight"] == 0.2, "새 정책이 더한 필드는 채운다"
    assert new["chunk_size"] == 50, "base 에 있는 아키텍처 값은 base 가 이긴다"
    assert cfg == {"type": "smolvla", "chunk_size": 50}, "원본 dict 를 고치면 안 된다"
    assert base_align.retyped_config({"type": "act"}, PLAN) is None, "허용 안 된 타입은 그대로"
    assert base_align.retyped_config({"type": "smolvla_s"}, PLAN) is None, "이미 그 정책이면 할 일이 없다"
    assert base_align.retyped_config(cfg, None) is None


# ── 2·3. 사본 ───────────────────────────────────────────────────────────────

def test_lengths_and_type_land_in_one_copy_apart_from_the_lengths_only_copy(tmp_path):
    base = _base(tmp_path)
    warm, note = base_align.align(base, _info(), tmp_path / "out", PLAN)
    plain, _ = base_align.align(base, _info(), tmp_path / "out")
    assert warm != plain and "-smolvla_s-" in Path(warm).name
    cfg = _cfg(warm)
    assert cfg["type"] == "smolvla_s"
    assert cfg["input_features"]["observation.state"]["shape"] == [7]
    assert _cfg(plain)["type"] == "smolvla", "길이만 맞춘 사본을 덮으면 안 된다"
    assert (Path(warm) / "model.safetensors").resolve() == (base / "model.safetensors").resolve()
    assert _cfg(str(base))["type"] == "smolvla", "base 는 한 글자도 안 바뀐다"
    assert "워밍스타트" in note


def test_a_policy_whose_weights_are_tied_to_lengths_is_only_retyped(tmp_path):
    """ACT → ACT-S — 길이 맞춤 대상이 아니어도(가중치가 길이에 묶여 있다) type 은 바꾼다."""
    base = _base(tmp_path, type_="act", state=7, action=7, padded=False)
    path, _ = base_align.align(base, _info(), tmp_path / "out",
                               {"to": "act_s", "from": ["act"], "fill": {}})
    assert path != str(base) and _cfg(path)["type"] == "act_s"


def test_warm_start_happens_even_without_dataset_lengths(tmp_path):
    base = _base(tmp_path)
    path, note = base_align.align(base, {}, tmp_path / "out", PLAN)
    assert _cfg(path)["type"] == "smolvla_s"
    assert "길이" in note, "길이를 못 맞춘 사실도 같이 말한다"


def test_the_local_start_retypes_without_a_dataset_root(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "log_dir", tmp_path / "data")
    base = _base(tmp_path)
    assert align_pretrained_to_dataset(str(base), "") == str(base), "지시가 없으면 예전 그대로"
    path = align_pretrained_to_dataset(str(base), "", PLAN)
    assert _cfg(path)["type"] == "smolvla_s"
    assert Path(path).is_relative_to(tmp_path / "data" / "derived_pretrained")


# ── 4. 원격 ─────────────────────────────────────────────────────────────────

def _run_script(tmp_path: Path, base: str, dataset: str, env: dict) -> subprocess.CompletedProcess:
    spec = TrainJobSpec(cmd=[sys.executable, "-c", "import sys; print('ARGS', *sys.argv[1:])",
                             f"--policy.path={base}", f"--dataset.repo_id={dataset}"],
                        total_steps=10, env=env)
    f = tmp_path / "run.sh"
    f.write_text(SSHRunner(host="d")._build_script(spec))
    return subprocess.run(["bash", str(f)], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path / "tmp")})


def _used(r: subprocess.CompletedProcess) -> str:
    args = next(ln for ln in r.stdout.splitlines() if ln.startswith("ARGS "))
    return next(a for a in args.split() if a.startswith("--policy.path=")).split("=", 1)[1]


def test_the_remote_script_trains_the_chosen_policy(tmp_path):
    (tmp_path / "tmp").mkdir()
    base = _base(tmp_path)
    ds = tmp_path / "ds"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(json.dumps(_info()))
    r = _run_script(tmp_path, str(base), str(ds), warm_start_env(PLAN))
    assert r.returncode == 0, r.stderr
    cfg = _cfg(_used(r))
    assert cfg["type"] == "smolvla_s" and cfg["input_features"]["observation.state"]["shape"] == [7]
    assert "워밍스타트" in r.stderr, "학습 로그에 남아야 한다"


def test_the_remote_script_still_retypes_when_the_dataset_cannot_be_read(tmp_path):
    """길이 맞춤이 빠지는 것보다 바닐라가 학습되는 것이 더 나쁘다."""
    (tmp_path / "tmp").mkdir()
    base = _base(tmp_path)
    broken = tmp_path / "ds"
    broken.mkdir()                                   # info.json 없음
    r = _run_script(tmp_path, str(base), str(broken), warm_start_env(PLAN))
    assert r.returncode == 0, r.stderr
    assert _cfg(_used(r))["type"] == "smolvla_s"
    assert "워밍스타트만" in r.stderr


def test_the_env_is_exported_before_the_alignment_step():
    script = SSHRunner(host="d")._build_script(TrainJobSpec(
        cmd=["python", "--policy.path=lerobot/smolvla_base", "--dataset.repo_id=org/ds"],
        env=warm_start_env(PLAN)))
    lines = script.splitlines()
    exported = next(i for i, ln in enumerate(lines) if ln.startswith(f"export {base_align.WARM_START_ENV}="))
    prelude = next(i for i, ln in enumerate(lines) if "PIPER_POLICY_PATH=" in ln)
    assert exported < prelude


# ── 5. 지시를 싣는 곳 ───────────────────────────────────────────────────────

def test_the_plan_comes_from_the_policy_spec():
    assert warm_start_plan("smolvla_s", {"stage_loss_weight": 0.3}) == {
        "to": "smolvla_s", "from": ["smolvla"], "fill": {"stage_loss_weight": 0.3}}
    assert warm_start_plan("act_s", None)["from"] == ["act"]
    assert warm_start_plan("smolvla", {}) is None, "바닐라는 다른 타입에서 출발하지 않는다"
    assert warm_start_env(None) == {}


def test_the_training_page_learns_the_allowed_types_from_the_spec():
    from app.core.policies import SPECS, ui_spec

    assert ui_spec("smolvla_s")["warm_start_from"] == ["smolvla"]
    assert ui_spec("act_s")["warm_start_from"] == ["act"]
    assert ui_spec("act")["warm_start_from"] == []
    assert ui_spec("no_such_policy")["warm_start_from"] == []
    # 권장 시작점이 돌아왔다 — 화면의 [Hub 에서 받기]
    assert SPECS["smolvla_s"].bases.policy == "lerobot/smolvla_base"


def test_remote_training_env_carries_the_plan():
    from app.routers.training import _with_warm_start

    assert _with_warm_start(None, None) is None
    env = _with_warm_start({"ACCELERATE_MIXED_PRECISION": "bf16"}, PLAN)
    assert env["ACCELERATE_MIXED_PRECISION"] == "bf16"
    assert json.loads(env[base_align.WARM_START_ENV])["to"] == "smolvla_s"


def test_rented_machines_get_the_same_plan(monkeypatch):
    from app.routers import cloud, training

    monkeypatch.setattr(training, "_train_env", lambda amp, remote=False: {"A": "1"})
    body = cloud.RentRequest.model_construct(
        amp="bf16", policy_type="smolvla_s", pretrained_path="lerobot/smolvla_base",
        policy_params={"stage_loss_weight": 0.1})
    env = cloud._rent_train_env(body)
    assert env["A"] == "1" and json.loads(env[base_align.WARM_START_ENV])["to"] == "smolvla_s"
    scratch = cloud.RentRequest.model_construct(amp="bf16", policy_type="smolvla_s",
                                                pretrained_path="", policy_params={})
    assert base_align.WARM_START_ENV not in cloud._rent_train_env(scratch), "처음부터 학습이면 지시가 없다"


@pytest.mark.parametrize("field", ["policy_type", "policy_params"])
def test_the_custom_start_accepts_the_page_policy(field):
    """직접 편집한 명령엔 `--policy.type` 이 없다 — 화면의 정책을 같이 받아야 워밍스타트를 안다."""
    from app.routers.training import TrainCustomRequest

    assert field in TrainCustomRequest.model_fields
    page = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "TrainingPage.tsx"
    start_custom = page.read_text().split("'/training/start-custom'", 1)[1][:400]
    assert field in start_custom, "화면이 직접 편집 시작에 정책을 안 보낸다"
