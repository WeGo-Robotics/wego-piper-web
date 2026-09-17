"""고아 스캐너 — **아무도 안 보는 동안**에도 돈이 새는지 본다 (feature/vast-training.md §6-3).

⚠ 이 파일이 지키는 것은 두 가지다.

1. **울려야 할 때 운다** — 라벨이 우리 것인데 레지스트리가 모르면 경고한다.
2. **울리지 말아야 할 때 안 운다** — 지금 학습 중인 임대를 고아라고 부르면, 사람은
   그다음부터 이 경보를 안 믿는다. 늘 울리는 알람은 꺼진 알람이다.

그리고 어느 경우에도 **자동으로 파기하지 않는다**(§10 결정 5).
"""

import asyncio
import logging

import pytest

from app.services.cloud import sweeper
from app.services.cloud.lifecycle import Budget, CloudJob, Phase
from app.services.cloud.providers.base import Instance


def _inst(iid=1, label="piper-abc", status="running", rate=0.12):
    return Instance(id=iid, label=label, status=status, gpu_name="RTX 3060",
                    rate_usd_h=rate, ssh=None)


class _Fake:
    """`list_instances` 만 있는 프로바이더. **`destroy` 를 부르면 터진다.**"""

    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.listed = 0

    def list_instances(self):
        self.listed += 1
        if self.error:
            raise self.error
        return list(self.rows)

    def destroy(self, instance_id):                                  # pragma: no cover
        raise AssertionError("스캐너가 파기를 불렀다 — 남의 학습일 수 있다(§10 결정 5)")


#: 테스트가 레지스트리에 남기는 job — 테스트 버스는 파일끼리 공유되므로 치운다.
_JOBS = ("j1", "local", "jx", "jy")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """모듈 전역(마지막 스캔 결과)을 매번 비운다 — 테스트끼리 결과를 물려주면 안 된다."""
    from app.services.cloud import rent
    from app.services.training.jobs import job_registry

    sweeper._last.update({"orphans": [], "scanned_at": 0.0, "error": "", "scanned": False})
    sweeper._last_error = ""
    # `vastai` 가 깔린 기계에서도 같은 결과가 나오게 — 실제 CLI 는 안 부른다
    monkeypatch.setattr(sweeper.shutil, "which", lambda name: "/usr/bin/vastai")
    # 기본값은 "도는 임대 없음". 고아는 임대가 끝난 뒤에 남는 것이라 이쪽이 평시다.
    monkeypatch.setattr(rent, "current", lambda: None)
    monkeypatch.setattr(rent, "_job", None)
    sent: list[list] = []

    async def _fake_broadcast(orphans):
        sent.append(list(orphans))

    monkeypatch.setattr(sweeper, "_broadcast", _fake_broadcast)
    yield sent
    for jid in _JOBS:
        try:
            job_registry.delete(jid)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────────────────────

def test_it_finds_a_machine_nobody_is_managing():
    """레지스트리가 모르는 우리 라벨 = 고아. **이게 스캐너의 존재 이유다.**"""
    found = sweeper.scan(_Fake([_inst(77, "piper-gone")]))
    assert [o["id"] for o in found] == [77]
    assert "77" in found[0]["text"]


def test_it_does_not_touch_machines_that_are_not_ours(monkeypatch):
    """⚠ 라벨이 다르면 **남의 기계다.** 같은 계정에 다른 용도의 인스턴스가 있을 수 있다."""
    rows = [_inst(1, "someone-else"), _inst(2, ""), _inst(3, "piper-x")]
    assert [o["id"] for o in sweeper.scan(_Fake(rows))] == [3]


def test_a_running_rental_is_not_an_orphan(monkeypatch):
    """⚠ **거짓 경보가 진짜 경보를 죽인다.** 지금 학습 중인 인스턴스가 10분마다
    빨간 경보를 내면, 진짜 고아가 떴을 때 아무도 안 본다."""
    monkeypatch.setattr(sweeper, "known_instances", lambda: {5})
    assert sweeper.scan(_Fake([_inst(5, "piper-live")])) == []


def test_the_scanner_never_destroys_anything():
    """⚠ §10 결정 5. 다른 기계의 게이트웨이가 돌리는 6시간짜리 학습일 수 있다 —
    남의 것을 끄는 쪽이 요금 몇 푼보다 나쁜 실수다. `_Fake.destroy` 가 터진다."""
    provider = _Fake([_inst(9, "piper-orphan")])
    found = sweeper.scan(provider)        # AssertionError 가 나면 여기서 실패한다
    assert found and provider.listed == 1


def test_the_text_says_what_it_costs():
    """사람을 움직이는 숫자는 요금이다. 문구는 **백엔드가** 만든다(`DeviceAlerts` 규칙)."""
    text = sweeper.scan(_Fake([_inst(3, "piper-x", rate=0.25)]))[0]["text"]
    assert "0.25" in text and "RTX 3060" in text


# ─────────────────────────────────────────────────────────────────────────────
# known — 무엇을 "관리 중" 으로 볼 것인가
# ─────────────────────────────────────────────────────────────────────────────

def test_known_reads_the_registry(monkeypatch):
    from app.services.training.jobs import JobRecord, job_registry

    job_registry.put(JobRecord(job_id="j1", instance_id="4242"))
    assert 4242 in sweeper.known_instances()


def test_known_ignores_records_without_an_instance(monkeypatch):
    """로컬 학습 레코드는 `instance_id` 가 빈 문자열이다 — 숫자로 바꾸면 터진다."""
    from app.services.training.jobs import JobRecord, job_registry

    job_registry.put(JobRecord(job_id="local", instance_id=""))
    assert sweeper.known_instances() == set()


def test_known_falls_back_to_the_live_job(monkeypatch):
    """⚠ 레지스트리 쓰기가 실패해도 거짓 경보가 나면 안 된다 — 메모리의 job 이 보완한다."""
    from app.services.cloud import rent

    job = CloudJob(job_id="j", label="piper-j", budget=Budget())
    job.instance_id = 999
    monkeypatch.setattr(rent, "current", lambda: job)
    assert 999 in sweeper.known_instances()


def test_known_survives_a_broken_registry(monkeypatch):
    """레지스트리를 못 읽어도 스캔은 돈다. ⚠ 다만 그때는 `known` 이 비어 전부
    고아로 보이므로, 조용히 넘기지 않고 경고를 남긴다."""
    from app.services.training import jobs

    monkeypatch.setattr(jobs.job_registry, "list",
                        lambda: (_ for _ in ()).throw(RuntimeError("버스 없음")))
    assert sweeper.known_instances() == set()


# ─────────────────────────────────────────────────────────────────────────────
# 한 바퀴
# ─────────────────────────────────────────────────────────────────────────────

def test_a_scan_runs_with_no_rental_in_flight():
    """⚠ 임대가 도는 중에만 도는 스캐너는 쓸모가 없다 — 고아는 **임대가 끝난 뒤**에
    남는 것이다(게이트웨이가 죽었다 살아난 경우)."""
    from app.services.cloud import rent

    assert not rent.busy()
    found = asyncio.run(sweeper.scan_once(lambda: _Fake([_inst(1, "piper-z")])))
    assert [o["id"] for o in found] == [1]


def test_no_vastai_means_quietly_nothing(monkeypatch):
    """Vast 를 안 쓰는 설치가 대부분이다. **고장이 아니다** — 조용히 넘어간다."""
    monkeypatch.setattr(sweeper.shutil, "which", lambda name: None)
    provider = _Fake([_inst(1, "piper-z")])
    assert asyncio.run(sweeper.scan_once(lambda: provider)) == []
    assert provider.listed == 0


def test_a_provider_error_keeps_the_last_known_list(caplog):
    """조회가 실패했다고 "고아 없음" 으로 바꾸면 **있던 경고가 사라진다.**"""
    asyncio.run(sweeper.scan_once(lambda: _Fake([_inst(8, "piper-h")])))
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(sweeper.scan_once(
            lambda: _Fake(error=RuntimeError("API 키 없음"))))
    assert [o["id"] for o in out] == [8]
    assert sweeper.snapshot()["error"]


def test_the_same_error_is_not_warned_twice(caplog):
    """⚠ API 키가 없는 설치에서 10분마다 같은 경고를 찍으면 로그가 그 문장으로 덮인다."""
    def boom():
        return _Fake(error=RuntimeError("API 키 없음"))

    with caplog.at_level(logging.WARNING, logger="app.services.cloud.sweeper"):
        asyncio.run(sweeper.scan_once(boom))
        asyncio.run(sweeper.scan_once(boom))
        asyncio.run(sweeper.scan_once(boom))
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1, [r.message for r in warns]


def test_it_warns_every_scan_while_the_machine_is_alive(caplog):
    """⚠ 로그는 **전이만** 찍으면 안 된다. 재시작 뒤 로그를 보는 사람은 "지금도 돈이
    나가는 중" 인지 알아야 한다 — 화면 알림과 달리 로그는 반복이 싸다."""
    def alive():
        return _Fake([_inst(2, "piper-burn")])

    with caplog.at_level(logging.WARNING, logger="app.services.cloud.sweeper"):
        asyncio.run(sweeper.scan_once(alive))
        asyncio.run(sweeper.scan_once(alive))
    assert sum("고아 인스턴스" in r.message for r in caplog.records) == 2


def test_the_screen_is_told_only_when_it_changes(fresh):
    """화면 알림은 **전이에서만** (`device_alert` 와 같은 규칙)."""
    def alive():
        return _Fake([_inst(2, "piper-burn")])

    asyncio.run(sweeper.scan_once(alive))
    asyncio.run(sweeper.scan_once(alive))
    assert len(fresh) == 1
    asyncio.run(sweeper.scan_once(lambda: _Fake([])))     # 정리됨 → 한 번 더
    assert len(fresh) == 2 and fresh[-1] == []


def test_snapshot_tells_not_yet_scanned_apart_from_nothing_found():
    """⚠ `scanned=false` 는 **"고아가 없다" 가 아니다** — 아직 안 본 것이다.
    화면이 둘을 같게 그리면 기동 직후 1분이 "안전함" 으로 보인다."""
    assert sweeper.snapshot()["scanned"] is False
    asyncio.run(sweeper.scan_once(lambda: _Fake([])))
    snap = sweeper.snapshot()
    assert snap["scanned"] is True and snap["count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 루프
# ─────────────────────────────────────────────────────────────────────────────

def test_the_loop_keeps_going_after_a_failure():
    """⚠ 루프가 죽으면 **조용히** 죽는다 — 아무도 "스캐너가 멈췄다" 를 안 본다."""
    rounds = []

    class _Flaky:
        def list_instances(self):
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("일시적 실패")
            return [_inst(6, "piper-after-failure")]

    async def go():
        task = asyncio.create_task(sweeper.run_sweeper(
            interval=0.01, first_delay=0.0, provider_factory=_Flaky))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(rounds) >= 3:
                break
        task.cancel()
        return len(rounds)

    assert asyncio.run(go()) >= 3
    assert [o["id"] for o in sweeper.snapshot()["orphans"]] == [6]


def test_the_first_scan_waits_for_the_restores():
    """⚠ 기동 직후엔 복원이 먼저다. 레지스트리가 제자리를 찾기 전에 보면 **살아 있는
    임대를 고아로 부른다.** (테스트가 lifespan 을 열었다 닫는 동안 실제 조회를
    걸지 않는 효과도 같은 이유에서 나온다.)"""
    provider = _Fake([_inst(1, "piper-z")])

    async def go():
        task = asyncio.create_task(sweeper.run_sweeper(
            interval=0.01, first_delay=30.0, provider_factory=lambda: provider))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert provider.listed == 0
    assert sweeper.FIRST_SCAN_S > 0 and sweeper.SWEEP_S == 600.0


def test_cancelling_the_loop_does_not_log_an_error():
    """종료는 정상이다 — 배포 때마다 오류가 찍히면 진짜 오류가 묻힌다."""
    async def go():
        task = asyncio.create_task(sweeper.run_sweeper(
            interval=0.01, first_delay=0.0, provider_factory=lambda: _Fake([])))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


# ─────────────────────────────────────────────────────────────────────────────
# 레지스트리에 번호를 남기는 쪽 (거짓 경보의 뿌리)
# ─────────────────────────────────────────────────────────────────────────────

def test_renting_records_the_instance_so_the_scanner_stays_quiet(monkeypatch):
    """⚠ 이게 없으면 `known` 이 늘 비어서 **학습 중인 기계가 고아로 보인다.**"""
    from app.services.cloud import rent

    job = CloudJob(job_id="jx", label="piper-jx", budget=Budget())
    job.note_started(4321, 0.2)
    rent._remember(job)
    assert 4321 in sweeper.known_instances()
    assert sweeper.scan(_Fake([_inst(4321, "piper-jx")])) == []


@pytest.mark.parametrize("phase", [Phase.DESTROYED, Phase.ORPHAN])
def test_a_finished_job_stops_claiming_the_instance(monkeypatch, phase):
    """⚠ 특히 `ORPHAN` 에서 — 파기를 확인 못 한 기계는 "관리 중" 이 아니라 **사람이
    봐야 할 것**이다. 번호를 남겨두면 스캐너가 아는 척하고 조용해진다."""
    from app.services.cloud import rent

    job = CloudJob(job_id="jy", label="piper-jy", budget=Budget())
    job.note_started(777, 0.2)
    rent._remember(job)
    job.phase = phase
    rent._remember(job)
    monkeypatch.setattr(rent, "current", lambda: None)
    assert 777 not in sweeper.known_instances()
    assert [o["id"] for o in sweeper.scan(_Fake([_inst(777, "piper-jy")]))] == [777]


# ─────────────────────────────────────────────────────────────────────────────
# 기동 배선 — **아무도 부르지 않아도 돌아야** 의미가 있다
# ─────────────────────────────────────────────────────────────────────────────

def test_the_gateway_starts_the_scanner_at_boot(monkeypatch):
    """⚠ lifespan 에서 이 줄이 떨어져 나가면 **조용히 아무 일도 안 일어난다.**

    스캐너의 요점이 "화면을 안 봐도 안다" 라서, 배선이 끊긴 것을 알아챌 방법이
    없다 — 고아가 떴을 때 비로소 모르게 된다. 그래서 여기서 못을 박는다.
    """
    from fastapi.testclient import TestClient

    started: list[dict] = []

    async def _fake(**kw):
        started.append(kw)
        await asyncio.sleep(3600)

    monkeypatch.setattr(sweeper, "run_sweeper", _fake)
    from app.main import app

    with TestClient(app):
        pass
    assert started, "기동해도 고아 스캐너가 안 떴다"


# ─────────────────────────────────────────────────────────────────────────────
# 화면이 받아 가는 자리
# ─────────────────────────────────────────────────────────────────────────────

def test_the_snapshot_endpoint_does_not_call_vast(monkeypatch):
    """⚠ 배너 하나 때문에 페이지마다 20초짜리 조회를 걸면 안 된다 — 이 응답은
    **마지막 스캔 결과**다. 실제 조회는 10분마다 배경에서 한다.

    ⚠ `scanned=false` 는 "고아가 없다" 가 **아니다.** 기동 직후이거나 `vastai` 가
    없는 설치다 — 화면이 둘을 같게 그리면 위험한 1분이 "안전함" 으로 보인다.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import cloud as router

    def _boom():                                                     # pragma: no cover
        raise AssertionError("스냅샷이 Vast 를 불렀다")

    monkeypatch.setattr(router, "_provider", _boom)
    c = TestClient(app)

    d = c.get("/api/cloud/orphans").json()
    assert d["scanned"] is False and d["count"] == 0 and d["interval_s"] == 600.0

    asyncio.run(sweeper.scan_once(lambda: _Fake([_inst(3, "piper-lost")])))
    d = c.get("/api/cloud/orphans").json()
    assert d["scanned"] is True and d["count"] == 1
    assert d["orphans"][0]["id"] == 3 and "3" in d["orphans"][0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# 크래시 뒤 — **스캐너가 있어야 할 바로 그 경우**
# ─────────────────────────────────────────────────────────────────────────────

def test_a_machine_left_by_a_crash_is_found_after_restart(monkeypatch):
    """⚠ **실측(2026-09-17)**: 게이트웨이가 임대 도중 죽으면 그 기계는 아무도 관리하지
    않는데, 레코드의 `instance_id` 가 Redis 에 남아 `known` 이 "관리 중" 으로 읽었다.
    그래서 고아 스캐너가 **조용했다** — 조용히 과금되는 그 경우에.

    임대 태스크는 asyncio 태스크라 프로세스와 함께 죽는다. 학습처럼 tmux 에 남아
    재부착되지 않는다 — 그러니 **재기동했다면 관리 중인 임대는 하나도 없다.**
    """
    from app.services.training.jobs import JobRecord, job_registry

    job_registry.put(JobRecord(job_id="crashed", instance_id="4242", provider="vast"))
    assert 4242 in sweeper.known_instances(), "전제가 틀렸다"
    assert sweeper.scan(_Fake([_inst(4242, "piper-crashed")])) == [], "전제가 틀렸다"

    freed = sweeper.release_claims()

    assert 4242 in freed
    assert 4242 not in sweeper.known_instances()
    assert [o["id"] for o in sweeper.scan(_Fake([_inst(4242, "piper-crashed")]))] == [4242]
    job_registry.delete("crashed")


def test_releasing_claims_does_not_destroy_anything():
    """⚠ 비우는 것은 "아는 척을 그만두는 것" 일 뿐이다. 끄는 것은 사람이 한다 —
    비웠다고 남의 것일 가능성이 사라지지는 않는다(§10 결정 5)."""
    from app.services.training.jobs import JobRecord, job_registry

    job_registry.put(JobRecord(job_id="crashed2", instance_id="7", provider="vast"))
    sweeper.release_claims()
    # _Fake.destroy 는 불리면 터진다
    sweeper.scan(_Fake([_inst(7, "piper-crashed2")]))
    job_registry.delete("crashed2")


def test_a_local_training_record_is_left_alone():
    """로컬 학습 레코드는 `instance_id` 가 비어 있다 — 건드릴 것이 없다."""
    from app.services.training.jobs import JobRecord, job_registry

    job_registry.put(JobRecord(job_id="localjob", instance_id="", total_steps=500))
    assert sweeper.release_claims() == []
    assert job_registry.get("localjob").total_steps == 500, "멀쩡한 레코드를 건드렸다"
    job_registry.delete("localjob")


def test_the_gateway_releases_claims_before_it_starts_scanning():
    """⚠ 순서가 전부다. 스캔이 먼저 돌면 그 회차는 여전히 "관리 중" 으로 보고 넘어간다."""
    import inspect

    from app import main

    src = inspect.getsource(main.lifespan)
    assert src.index("release_claims()") < src.index("run_sweeper()"), \
        "주장을 비우기 전에 스캐너가 뜬다"


def test_the_record_still_says_which_machine_ran_the_job(monkeypatch):
    """⚠ **실측(2026-09-17)**: 학습이 시작된 뒤 레코드의 `instance_id` 가 빈
    문자열이었다. `train_manager.start()` 가 옛 로그를 치우려고 레코드를 통째로
    지우는데(`registry.delete`), 임대 번호가 같은 레코드에 실려 있어서다.

    같은 프로세스에서는 `known_instances()` 가 메모리의 `rent.current()` 로 보완하므로
    화면이 당장 거짓말을 하지는 않는다. 그래도 남겨야 하는 이유는, 레코드가 **"이
    학습이 어느 기계에서 돌았나"** 를 남기는 유일한 자리이기 때문이다 — 비어 있으면
    끝난 뒤에 아무도 답할 수 없다.
    """
    import asyncio

    from app.services.cloud import rent
    from app.services.training import train_manager
    from app.services.training.jobs import job_registry

    job = CloudJob(job_id=train_manager.job_id, label="piper-x", budget=Budget())
    job.note_started(31337, 0.5)
    rent._remember(job)
    assert job_registry.get(train_manager.job_id).instance_id == "31337"

    # 학습 시작이 레코드를 지우는 그 동작을 그대로 흉내 낸다
    job_registry.delete(train_manager.job_id)
    assert (job_registry.get(train_manager.job_id) or
            type("R", (), {"instance_id": ""})).instance_id == ""

    rent._remember(job)          # ← 다시 심는 자리
    assert job_registry.get(train_manager.job_id).instance_id == "31337"
    job_registry.delete(train_manager.job_id)


def test_the_reclaim_happens_after_the_start_not_before():
    """⚠ 순서가 전부다. `start()` 앞에서 심으면 그 `start()` 가 다시 지운다."""
    import inspect

    from app.services.cloud import rent

    src = inspect.getsource(rent.start)
    assert src.index("train_manager.start(") < src.index("_remember(_job)"), \
        "재기재가 학습 시작보다 앞이다 — 그러면 지워진다"


# ─────────────────────────────────────────────────────────────────────────────
# 사람이 일부러 빌린 기계 — **고아가 아니다** (2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

def test_a_deliberately_rented_box_is_not_an_orphan():
    """⚠ 클라우드 페이지에서 기계를 만들고 학습 페이지에서 골라 쓰는 흐름이라, 학습
    사이사이에는 아무 job 도 안 붙어 있다. 그걸 고아라고 부르면 10분마다 빨간 경보가
    울리고 — 늘 울리는 알람은 꺼진 알람이다."""
    rows = [_inst(1, "piper-abc"), _inst(2, "piper-box-42"), _inst(3, "someone-else")]
    assert [o["id"] for o in sweeper.scan(_Fake(rows))] == [1]


def test_the_label_is_where_ownership_is_recorded():
    """⚠ 레지스트리에 적으면 게이트웨이가 죽는 순간 잃는다(§12-14 가 그래서 기동 때
    임대 주장을 비운다). 그런데 "내가 이 기계를 빌렸다" 는 **사람의 결정**이라 프로세스
    보다 오래 살아야 한다 — 라벨은 Vast 가 들고 있으므로 우리가 무엇을 잊든 남는다."""
    from app.routers import cloud as router

    import inspect
    assert "sweeper.BOX_PREFIX" in inspect.getsource(router.create_instance)


def test_an_idle_box_is_reported_with_what_it_has_cost():
    """⚠ 고아와 다르다 — "잃어버린 기계" 가 아니라 "일부러 둔 기계" 다. 그래도 빈 기계에
    요금은 똑같이 나가므로 **얼마나 나갔는지** 같이 말한다."""
    rows = [_inst(7, "piper-box-7", rate=0.6)]
    assert sweeper.idle_boxes(rows, training=False, now=1000.0) == []      # 이제 막 봤다
    later = sweeper.idle_boxes(rows, training=False, now=1000.0 + sweeper.IDLE_WARN_S + 60)
    assert later and later[0]["id"] == 7
    assert "$" in later[0]["text"] and "분째" in later[0]["text"]


def test_training_clears_the_idle_clock():
    """⚠ 안 지우면 6시간짜리 학습이 끝난 직후 "6시간째 유휴" 라고 말한다."""
    rows = [_inst(7, "piper-box-7")]
    sweeper.idle_boxes(rows, training=False, now=0.0)
    sweeper.idle_boxes(rows, training=True, now=100.0)          # 학습 중 — 시계를 지운다
    assert sweeper.idle_boxes(rows, training=False, now=200.0) == []


def test_a_one_shot_rental_is_not_called_idle():
    """한 묶음(`piper-<job>`)은 스스로 파기한다 — 유휴라고 말할 대상이 아니다."""
    rows = [_inst(9, "piper-oneshot")]
    assert sweeper.idle_boxes(rows, training=False, now=1e9) == []


def test_finishing_a_job_on_someone_elses_box_does_not_destroy_it():
    """⚠ 사람이 [중지]를 눌러도 그 기계는 안 꺼진다. 학습 한 번 끝났다고 끄면 다음
    학습을 준비하던 사람의 기계가 사라진다."""
    from app.services.cloud.lifecycle import Phase as _P

    class _Boom:
        def destroy(self, iid):                                  # pragma: no cover
            raise AssertionError("남의 기계를 껐다")

    job = CloudJob(job_id="j", label="piper-box-1", owns_instance=False, budget=Budget())
    job.note_started(42, 0.5)
    assert job.finish(_Boom(), "사람이 중지했습니다") is _P.FINISHED
    assert job.finish(_Boom()) is _P.FINISHED, "멱등이 아니다"
