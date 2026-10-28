"""학습 로더 진단 (feature/train-loader-workers.md §2·§3).

원래 문제: 카드가 `data_s < updt_s` 이면 "GPU 연산 병목 → AMP·Batch" 라고 처방했다.
대기 3% 는 GPU 가 쉬지 않는다는 뜻이지 고칠 게 있다는 뜻이 아니다.
"""

from app.services.training.loader import advise
from app.services.training.manager import _loader_context
from app.services.training.metrics import MetricsTracker


def _same(u: float, d: float, n: int = 5) -> list[tuple[float, float]]:
    return [(u, d)] * n


def test_small_wait_is_enough_and_prescribes_nothing():
    """스크린샷 그대로 — 늘려도 최대 1.03배라 권장값은 지금 값이다."""
    a = advise(_same(0.064, 0.002), 4, cores=20)
    assert a["reason"] == "enough"
    assert a["recommended"] == 4
    assert a["wait_pct"] == 3.0
    assert a["max_gain"] == 1.03


def test_loader_short_recommends_workers_from_the_batch_time():
    """T_b = 4 × 0.104 = 0.416 → 0.416 / 0.064 = 6.5 → × 1.2 → 8."""
    a = advise(_same(0.064, 0.040), 4, cores=20)
    assert a["reason"] == "add_workers"
    assert a["recommended"] == 8
    assert a["predicted_wait_pct"] == 0.0


def test_zero_workers_uses_the_wait_as_the_batch_time():
    a = advise(_same(0.064, 0.300), 0, cores=20)
    assert a["reason"] == "add_workers"
    assert a["recommended"] == 6


def test_core_cap_says_workers_cannot_close_the_gap():
    """37 이 필요한데 20코어 − 2 = 18 에서 잘린다 — 그래도 대기 41% 가 남는다고 말한다."""
    a = advise(_same(0.030, 0.200), 4, cores=20)
    assert a["reason"] == "cpu_limit"
    assert a["recommended"] == 18
    assert a["predicted_wait_pct"] == 41.3


def test_busy_cpu_does_not_extrapolate():
    """CPU 가 찼으면 워커 하나의 속도가 N 과 무관하다는 가정이 깨진다 — 권장하지 않는다."""
    a = advise(_same(0.064, 0.040), 4, cores=20, cpu_pct=97.0)
    assert a["reason"] == "cpu_limit"
    assert a["recommended"] == 4
    assert a["cpu_busy"] is True


def test_unknown_cores_does_not_cap():
    """원격 학습 — 남의 기계 코어를 모르면 자르지 않는다."""
    a = advise(_same(0.030, 0.200), 4)
    assert a["reason"] == "add_workers"
    assert a["recommended"] == 37


def test_median_ignores_one_epoch_restart_spike():
    """LeRobot 은 에폭마다 워커를 새로 띄운다 — 그 구간 하나로 판정이 바뀌면 안 된다."""
    samples = _same(0.064, 0.002, 4) + [(0.064, 0.500)]
    assert advise(samples, 4, cores=20)["reason"] == "enough"


def test_too_few_intervals_is_warming():
    assert advise(_same(0.064, 0.040, 2), 4, cores=20)["reason"] == "warming"
    assert advise(_same(0.064, 0.040), None, cores=20)["reason"] == "warming"


def test_amp_off_is_reported_only_from_the_setting():
    assert advise(_same(0.064, 0.002), 4, amp="off")["amp_off"] is True
    assert advise(_same(0.064, 0.002), 4, amp="bf16")["amp_off"] is False
    assert advise(_same(0.064, 0.002), 4)["amp_off"] is False   # 재부착 — 모른다


_LINE = ("step:{s} smpl:12.8K ep:106 epch:1.07 loss:0.342 grdn:2.157 "
         "lr:1.0e-03 updt_s:{u} data_s:{d}")


def test_tracker_drops_the_first_interval():
    """첫 구간은 워커 기동·cudnn 자동 튜닝이 섞여 있다."""
    t = MetricsTracker()
    t.reset()
    t.feed(_LINE.format(s=200, u=0.9, d=3.0))
    t.feed(_LINE.format(s=400, u=0.064, d=0.002))
    assert list(t.timings) == [(0.064, 0.002)]


def test_tracker_keeps_every_interval_even_when_the_step_repeats():
    """히스토리는 `step:25K` 가 되풀이되면 덮어쓰지만 타이밍은 구간마다 하나씩이다."""
    t = MetricsTracker()
    t.reset()
    for _ in range(4):
        t.feed(_LINE.format(s="25K", u=0.064, d=0.002))
    assert len(t.timings) == 3
    assert len(t.history.steps) == 1


def test_context_reads_workers_and_amp_from_the_command():
    cmd = ["python", "-m", "lerobot.scripts.lerobot_train", "--num_workers=8",
           "--dataset.repo_id=a/b", "--policy.type=act", "--batch_size=16"]
    ctx = _loader_context(cmd, {"ACCELERATE_MIXED_PRECISION": "bf16"})
    assert ctx["num_workers"] == 8 and ctx["amp"] == "bf16"
    assert ctx["dataset"] == "a/b" and ctx["policy"] == "act" and ctx["batch_size"] == "16"
    # AMP 를 끄면 환경변수를 안 넣는다
    assert _loader_context(cmd, {})["amp"] == "off"
    # 재부착은 유닛 argv 만 남는다
    assert _loader_context(cmd, None)["amp"] is None
    # 인자가 없으면 LeRobot 기본값
    assert _loader_context(["lerobot-train"], {})["num_workers"] == 4


# ── 프로파일: 다음 학습 폼이 지난 실측으로 권장값을 채운다 (§4) ──

class _MemBus:
    """레지스트리가 부르는 것만 — 실제 Redis 를 지우지 않으려고 메모리에 둔다."""

    def __init__(self):
        self.jobs, self.profiles = {}, {}

    def put_job(self, job_id, record): self.jobs[job_id] = record
    def get_job(self, job_id): return self.jobs.get(job_id)
    def delete_job(self, job_id): self.jobs.pop(job_id, None)
    def list_jobs(self): return list(self.jobs.values())
    def put_loader_profile(self, key, profile): self.profiles[key] = profile
    def get_loader_profile(self, key): return self.profiles.get(key)


class _Runner:
    is_running = True
    pid = None

    def __init__(self, remote=False):
        from app.services.process_manager import ProcessState
        self.state = ProcessState.RUNNING
        self.is_remote = remote

    def set_log_callback(self, cb): self.feed = cb
    def set_state_callback(self, cb): pass
    async def start(self, spec): pass
    async def stop(self): pass
    def restore(self): return None


_CMD = ["python", "-m", "lerobot.scripts.lerobot_train", "--dataset.repo_id=a/b",
        "--policy.type=act", "--batch_size=8", "--num_workers=4"]


def _run(remote=False):
    import asyncio

    from app.services.training.jobs import JobRegistry
    from app.services.training.manager import TrainManager

    bus, runner = _MemBus(), _Runner(remote)
    tm = TrainManager(runner=runner, registry=JobRegistry(bus))
    asyncio.run(tm.start(_CMD, total_steps=1000, env_extra={"ACCELERATE_MIXED_PRECISION": "bf16"}))
    for i in range(1, 5):
        runner.feed(_LINE.format(s=i * 100, u=0.064, d=0.040))
    return tm, bus


def test_local_run_leaves_a_profile_for_the_next_form(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 20)    # 코어가 적은 기계에선 상한에 걸린다
    tm, bus = _run()
    assert tm.get_status()["loader"]["reason"] == "add_workers"
    prof = bus.profiles["a/b|act|bf16"]
    assert prof["num_workers"] == 4 and prof["batch_size"] == "8"
    assert (prof["update_s"], prof["data_s"]) == (0.064, 0.04)


def test_remote_run_leaves_no_profile():
    """임대 기계는 매번 다르다 — 이 기계 폼에 남의 기계 실측을 채우면 틀린다."""
    tm, bus = _run(remote=True)
    assert tm.get_status()["loader"]["cap"] is None
    assert bus.profiles == {}


def test_hint_endpoint_judges_the_saved_profile(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import training
    from app.services.training.jobs import JobRegistry

    bus = _MemBus()
    bus.profiles["a/b|act|bf16"] = {"num_workers": 4, "batch_size": "8",
                                    "update_s": 0.064, "data_s": 0.040, "cpu_pct": 30.0}
    monkeypatch.setattr(training, "job_registry", JobRegistry(bus))
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    c = TestClient(app)
    got = c.get("/api/training/loader-hint", params={"dataset": "a/b", "policy": "act"}).json()
    assert got["advice"]["reason"] == "add_workers" and got["advice"]["recommended"] == 8
    none = c.get("/api/training/loader-hint", params={"dataset": "a/b", "policy": "smolvla"}).json()
    assert none == {"profile": None}
