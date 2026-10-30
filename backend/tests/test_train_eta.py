"""학습 경과·남은 시간·완료 예정.

메트릭 줄의 step 은 1000 단위로 반올림돼(`step:37K`) 그걸로 남은 시간을 내면 ±6분씩 출렁인다.
tqdm 진행 줄에는 정확한 step 과 루프 경과가 한 줄에 있다 — 그걸 쓴다.
"""

from datetime import datetime, timedelta

import pytest

from app.services.process_manager import ProcessState
from app.services.training import TrainJobSpec
from app.services.training.jobs import JobRecord
from app.services.training.metrics import MetricsTracker, parse_clock


def _bar(n: int, total: int, elapsed: str) -> str:
    """LeRobot 이 실제로 찍는 모양 (2026-10-30 저널에서)."""
    return f"Training:  19%|█▊        | {n}/{total} [{elapsed}<34:15:32,  1.32step/s]"


def _metric(step: str, updt: float, data: float) -> str:
    return (f"INFO 2026-10-30 08:31:30 ot_train.py:439 step:{step} smpl:2M ep:13K epch:42.07 "
            f"loss:0.427 grdn:0.463 lr:2.5e-06 updt_s:{updt} data_s:{data}")


def test_clock_hours_go_past_24():
    assert parse_clock("40:13") == 40 * 60 + 13
    assert parse_clock("7:40:13") == 7 * 3600 + 40 * 60 + 13
    assert parse_clock("34:15:32") == 34 * 3600 + 15 * 60 + 32


def test_the_progress_line_is_not_a_metric_line():
    """매 step 오는 줄이다 — 메트릭 콜백(화면·Redis 쓰기)을 부르면 안 된다."""
    t = MetricsTracker()
    assert t.feed(_bar(37000, 200000, "7:40:13")) is False


def test_remaining_uses_the_measured_speed():
    """처음 본 뒤의 평균 속도 — 체크포인트 저장까지 들어간 벽시계다."""
    t = MetricsTracker()
    t.feed(_bar(100, 5000, "01:00"))
    t.feed(_bar(1100, 5000, "13:30"))          # 1000 step 에 750초 = 0.75초/step
    remaining, basis = t.remaining()
    assert basis == "progress"
    assert remaining == pytest.approx((5000 - 1100) * 0.75)


def test_other_progress_bars_are_ignored():
    """체크포인트 샤드 로딩 같은 막대도 `n/total [경과<` 모양이다."""
    t = MetricsTracker()
    t.feed("Loading checkpoint shards: 100%|██████████| 2/2 [00:01<00:00,  1.20it/s]")
    t.feed("Fetching 3 files: 100%|██████████| 3/3 [00:00<00:00, 12.1it/s]")
    assert t.remaining() is None


def test_no_estimate_in_the_first_minute():
    """첫 step 들엔 워커 기동·cudnn 튜닝이 섞인다 — 그 속도로 내면 몇 배 길게 나온다."""
    t = MetricsTracker()
    t.feed(_bar(1, 5000, "00:05"))
    t.feed(_bar(40, 5000, "00:35"))
    assert t.remaining() is None


def test_resume_counts_from_where_the_loop_started():
    """resume 하면 tqdm 은 total=`steps − 시작 step`, n=0 에서 다시 센다 — `total − n` 이 남은 수다."""
    t = MetricsTracker()
    t.feed(_bar(0, 20000, "00:00"))
    t.feed(_bar(2000, 20000, "25:00"))         # 0.75초/step
    remaining, _ = t.remaining()
    assert remaining == pytest.approx(18000 * 0.75)


def test_glued_updates_take_the_last_one():
    """러너가 `\\r` 에서 안 끊으면 갱신이 한 줄에 붙고 끝에 메트릭 줄이 온다 (저널에서 실제로 그랬다)."""
    t = MetricsTracker()
    t.reset(total_steps=200000)
    t.feed(_bar(100, 200000, "01:15"))
    line = _bar(1100, 200000, "13:44") + _bar(1101, 200000, "13:45") + _metric("1K", 0.713, 0.021)
    assert t.feed(line) is True                 # 메트릭도 그대로 읽는다
    remaining, _ = t.remaining()
    assert remaining == pytest.approx((200000 - 1101) * (825 - 75) / 1001)


def test_a_new_loop_starts_the_measurement_over():
    """같은 트래커에 새 학습이 이어 붙으면(n 이 줄면) 지난 학습의 시작점으로 재면 안 된다."""
    t = MetricsTracker()
    t.feed(_bar(100, 5000, "01:00"))
    t.feed(_bar(4000, 5000, "50:00"))
    t.feed(_bar(10, 3000, "00:20"))
    t.feed(_bar(1010, 3000, "20:20"))          # 1000 step 에 1200초
    remaining, _ = t.remaining()
    assert remaining == pytest.approx((3000 - 1010) * 1.2)


def test_without_tqdm_it_falls_back_to_step_time():
    """tqdm 이 없으면(원격 로그·LeRobot 이 바뀜) 메트릭 줄의 updt_s+data_s 중앙값으로."""
    t = MetricsTracker()
    t.reset(total_steps=10000)
    t.feed(_metric("200", 0.9, 0.5))           # 첫 구간은 버린다
    t.feed(_metric("400", 0.7, 0.05))
    t.feed(_metric("600", 0.7, 0.05))
    remaining, basis = t.remaining()
    assert basis == "step_time"
    assert remaining == pytest.approx((10000 - 600) * 0.75)


# ── 매니저: 경과·총 시간 ────────────────────────────────────────────────────


class _Runner:
    state = ProcessState.RUNNING
    is_running = True
    pid = 1

    def __init__(self, spec=None):
        self._spec = spec

    def set_log_callback(self, cb): pass
    def set_state_callback(self, cb): pass
    async def start(self, spec): pass
    async def stop(self): pass
    def restore(self): return self._spec


class _Registry:
    """버스 없이 — 레지스트리는 dict 하나면 된다."""

    def __init__(self):
        self.records: dict[str, JobRecord] = {}

    def get(self, job_id): return self.records.get(job_id)
    def put(self, record): self.records[record.job_id] = record
    def delete(self, job_id): self.records.pop(job_id, None)
    def append_log(self, job_id, line): pass
    def put_loader_profile(self, key, profile): pass


def _manager(spec=None):
    from app.services.training.manager import TrainManager

    reg = _Registry()
    return TrainManager(runner=_Runner(spec), registry=reg), reg


def test_timing_adds_up():
    tm, _ = _manager()
    now = datetime(2026, 10, 30, 12, 0, tzinfo=datetime.now().astimezone().tzinfo)
    tm.started_at = now - timedelta(hours=2)
    tm.tracker.feed(_bar(100, 5000, "01:00"))
    tm.tracker.feed(_bar(1100, 5000, "13:30"))

    t = tm.timing(now=now)
    assert t["elapsed_s"] == 7200
    # 마지막 줄을 본 뒤 흐른 몇 ms 를 빼므로 초 단위로 1 까지 어긋날 수 있다
    assert t["remaining_s"] == pytest.approx(3900 * 0.75, abs=1)
    assert t["total_s"] == pytest.approx(t["elapsed_s"] + t["remaining_s"], abs=1)
    finish = datetime.fromisoformat(t["finish_at"])
    assert abs((finish - (now + timedelta(seconds=3900 * 0.75))).total_seconds()) <= 1
    assert t["basis"] == "progress"


def test_time_since_the_last_line_is_taken_off():
    """journald 는 `\\n` 에서만 줄을 끊어 tqdm 갱신이 메트릭 줄과 함께 몰려 온다(실측: 재시작 전후
    status 가 같은 step·같은 남은 시간이었다). 그 뒤 흐른 시간을 안 빼면 완료 예정이 그만큼 밀린다."""
    import time

    tm, _ = _manager()
    tm.started_at = datetime.now().astimezone()
    tm.tracker.feed(_bar(100, 5000, "01:00"))
    tm.tracker.feed(_bar(1100, 5000, "13:30"))
    tm.tracker.seen_at = time.monotonic() - 100
    assert tm.timing()["remaining_s"] == pytest.approx(3900 * 0.75 - 100, abs=1)


def test_no_timing_when_nothing_runs():
    tm, _ = _manager()
    tm.runner.is_running = False
    assert tm.timing() is None


def test_reattach_keeps_the_start_time():
    """게이트웨이를 재시작해도 경과는 학습을 건 때부터다 — 재시작한 때부터가 아니다."""
    tm, reg = _manager(TrainJobSpec(cmd=["lerobot-train"], total_steps=5000))
    reg.put(JobRecord(job_id="local", state=ProcessState.RUNNING.value,
                      created_at="2026-10-30T08:00:00+09:00"))
    assert tm.restore_running_process() is True
    assert tm.started_at == datetime.fromisoformat("2026-10-30T08:00:00+09:00")
