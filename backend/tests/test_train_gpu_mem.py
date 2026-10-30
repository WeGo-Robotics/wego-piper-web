"""학습 GPU 메모리 — 실측·메모리 부족 기록으로 다음 배치를 가늠한다 (training/gpu_mem.py).

숫자는 2026-10-30 실측이다: SmolVLA-S 전체 학습(VLM·비전 인코더 안 얼림), 카메라 3대, bf16, RTX 5090.
배치 16 → 26,828MiB 로 돌았고 배치 20 → 30.28GiB 쓴 채 메모리 부족으로 죽었다.
"""

import pytest

from app.services.process_manager import ProcessState
from app.services.training import TrainJobSpec, gpu_mem
from app.services.training.metrics import MetricsTracker

PARAMS = {"total": 450_294_248, "learnable": 402_985_448}
TOTAL_MB, OTHER_MB = 32_607, 537          # nvidia-smi 총량, RustDesk 가 쥔 몫
OOM_LINE = (
    "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 34.00 MiB. GPU 0 has a total "
    "capacity of 31.35 GiB of which 29.62 MiB is free. Process 1171574 has 537.20 MiB memory in use. "
    "Including non-PyTorch memory, this process has 30.28 GiB memory in use. Of the allocated memory "
    "29.53 GiB is allocated by PyTorch, and 143.66 MiB is reserved by PyTorch but unallocated."
)


def _measured():
    return {"params": PARAMS, "batch": 16, "peak_mb": 26_828}


def test_the_two_runs_agree_with_one_model():
    """배치 16 실측 하나로 낸 계산이 배치 20 의 메모리 부족을 맞힌다."""
    e20 = gpu_mem.estimate(_measured(), 20, TOTAL_MB, OTHER_MB)
    assert e20["level"] == "over"
    assert e20["need_mb"] > 31_007, "죽은 순간 30.28GiB 를 쓰고 있었다 — 그보다 적게 나오면 틀렸다"
    e16 = gpu_mem.estimate(_measured(), 16, TOTAL_MB, OTHER_MB)
    assert e16["level"] == "ok" and e16["need_mb"] == 26_828
    assert e16["max_batch"] == 16


def test_tight_before_over():
    """쓸 수 있는 메모리의 90% 를 넘으면 빠듯하다 — 조각화·저장 순간 출렁임에 죽을 수 있다."""
    assert gpu_mem.estimate(_measured(), 17, TOTAL_MB, OTHER_MB)["level"] == "tight"


def test_other_processes_shrink_what_is_left():
    """추론·원격 데스크톱이 쥔 메모리는 학습이 못 쓴다."""
    alone = gpu_mem.estimate(_measured(), 16, TOTAL_MB, 0)
    shared = gpu_mem.estimate(_measured(), 16, TOTAL_MB, 6_000)
    assert shared["max_batch"] < alone["max_batch"]
    assert shared["level"] == "over"


def test_the_oom_line_is_read():
    o = gpu_mem.parse_oom(OOM_LINE)
    assert o == {"capacity_mb": round(31.35 * 1024), "in_use_mb": round(30.28 * 1024)}
    assert gpu_mem.parse_oom("INFO step:1K loss:0.3") is None


def test_an_oom_alone_still_gives_an_upper_bound():
    """성공한 실측이 없어도 죽은 기록으로 권한다 — 죽은 순간까지 쓴 양이라 하한, 권장 배치는 상한."""
    prof = {"params": PARAMS, "oom": {"batch": 20, "in_use_mb": round(30.28 * 1024)}}
    e = gpu_mem.estimate(prof, 20, TOTAL_MB, OTHER_MB)
    assert e["basis"] == "oom" and e["level"] == "over"
    assert 0 < e["max_batch"] < 20
    assert gpu_mem.estimate(prof, 16, TOTAL_MB, OTHER_MB)["level"] == "ok"


def test_a_batch_that_died_is_never_recommended():
    """계산이 뭐라든 같은 기계·같은 설정에서 실제로 죽은 배치다."""
    prof = {**_measured(), "oom": {"batch": 17, "in_use_mb": 26_000}}
    e = gpu_mem.estimate(prof, 17, TOTAL_MB, OTHER_MB)
    assert e["level"] == "over" and e["max_batch"] <= 16


def test_no_params_no_estimate():
    assert gpu_mem.estimate({"batch": 16, "peak_mb": 26_828}, 16, TOTAL_MB, OTHER_MB) is None


def test_form_and_command_make_the_same_key():
    """폼(불리언)과 실제 명령(문자열)이 같은 키를 만들어야 지난 실측을 찾는다."""
    cmd = ["python", "-m", "lerobot.scripts.lerobot_train", "--batch_size=20",
           "--policy.freeze_vision_encoder=false", "--policy.train_expert_only=false",
           "--policy.stage_loss_weight=0.1"]
    from_cmd = gpu_mem.profile_key("d", "smolvla_s", "bf16", gpu_mem.switches_from_cmd(cmd))
    from_form = gpu_mem.profile_key("d", "smolvla_s", "bf16", {
        "freeze_vision_encoder": False, "train_expert_only": False})
    assert from_cmd == from_form
    assert "stage_loss_weight" not in from_cmd, "메모리와 무관한 값이 키를 쪼갠다"


def test_tracker_reads_param_counts_and_oom():
    t = MetricsTracker()
    t.feed("INFO 2026-10-30 10:50:17 ot_train.py:342 num_learnable_params=402985448 (403M)")
    t.feed("INFO 2026-10-30 10:50:17 ot_train.py:343 num_total_params=450294248 (450M)")
    t.feed(OOM_LINE)
    assert t.params == PARAMS
    assert t.oom["in_use_mb"] == round(30.28 * 1024)


# ── 매니저: 재고 남기고 알린다 ──────────────────────────────────────────────


class _Runner:
    state = ProcessState.RUNNING
    is_running = True
    pid = 4242

    def set_log_callback(self, cb): pass
    def set_state_callback(self, cb): pass
    async def start(self, spec): pass
    async def stop(self): pass
    def restore(self): return None


class _Registry:
    def __init__(self):
        self.mem: dict[str, dict] = {}

    def get(self, job_id): return None
    def put(self, record): pass
    def delete(self, job_id): pass
    def append_log(self, job_id, line): pass
    def put_loader_profile(self, key, profile): pass
    def put_mem_profile(self, key, profile): self.mem[key] = profile
    def get_mem_profile(self, key): return self.mem.get(key)


CMD = ["python", "-m", "lerobot.scripts.lerobot_train", "--dataset.repo_id=wego-hansu/apple",
       "--policy.type=smolvla_s", "--batch_size=20",
       "--policy.freeze_vision_encoder=false", "--policy.train_expert_only=false"]


@pytest.fixture
def tm(monkeypatch):
    import asyncio

    from app.services.training.manager import TrainManager

    monkeypatch.setattr(gpu_mem, "query", lambda pid: {
        "total_mb": TOTAL_MB, "used_mb": 26_828 if pid else None, "other_mb": OTHER_MB})
    m = TrainManager(runner=_Runner(), registry=_Registry())
    asyncio.run(m.start(CMD, total_steps=30000, env_extra={"ACCELERATE_MIXED_PRECISION": "bf16"}))
    m.tracker.feed("num_learnable_params=402985448 (403M)")
    m.tracker.feed("num_total_params=450294248 (450M)")
    return m


def test_an_oom_is_announced_without_a_metric_line(tm):
    """메모리 부족은 첫 메트릭 줄 전에 죽는다 — 메트릭 줄을 기다리면 화면에 아무것도 안 뜬다."""
    sent = []
    tm.set_metrics_callback(sent.append)
    tm._intercept_log(OOM_LINE)
    assert sent and sent[-1]["oom"]["batch"] == 20
    assert 0 < sent[-1]["oom"]["max_batch"] < 20
    (prof,) = tm.registry.mem.values()
    assert prof["oom"]["batch"] == 20


def test_a_running_job_leaves_its_peak(tm):
    tm.tracker.feed("step:200 smpl:4K ep:20 epch:0.07 loss:0.9 grdn:2.0 lr:1.0e-05 "
                    "updt_s:0.4 data_s:0.01")
    (key, prof) = next(iter(tm.registry.mem.items()))
    assert key == "wego-hansu/apple|smolvla_s|bf16|freeze_vision_encoder=false,train_expert_only=false"
    assert prof["peak_mb"] == 26_828 and prof["batch"] == 20 and prof["params"] == PARAMS
    assert tm.get_status()["gpu_mem"]["used_mb"] == 26_828


def test_reattach_recovers_amp_from_the_unit(monkeypatch):
    """AMP 는 argv 가 아니라 환경변수로 들어갔다 — 재부착이 그걸 못 되찾으면 키를 못 만든다."""
    from app.services import systemd_process as S

    class _Out:
        stdout = ("Environment=PYTHONPATH=/x DISPLAY=:1 PIPER_REDIS_URL=redis://127.0.0.1:6379/0 "
                  "ACCELERATE_MIXED_PRECISION=bf16\n")

    monkeypatch.setattr(S, "_systemctl", lambda *a, **k: _Out())
    env = S.SystemdProcess.__new__(S.SystemdProcess)
    env.unit = "piper-train-local.service"
    assert env.exec_env()["ACCELERATE_MIXED_PRECISION"] == "bf16"
