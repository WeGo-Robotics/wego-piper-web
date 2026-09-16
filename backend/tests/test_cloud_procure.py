"""조달 — **나가는 길은 하나다** (feature/vast-training.md §3·§6).

⚠ 이 저장소는 `pytest-asyncio` 를 쓰지 않는다 — async 코드는 `asyncio.run()` 으로
부른다(`test_llm_client.py` 등과 같은 관습). 테스트 하나 때문에 의존성을 늘리지 않는다.

⚠ 이 파일이 지키는 것은 `finally` 다. 성공이든 예외든 예산 초과든 SSH 실패든,
파기를 지나야 한다. 학습 실패는 다시 돌리면 되지만 **파기 실패는 아무도 안 보는
동안 요금이 나간다.**
"""

import asyncio

import pytest

from app.services.cloud.lifecycle import Budget, Phase
from app.services.cloud.procure import (
    ProcureError, procure_and_train, run_until_done, wait_for_ssh,
)
from app.services.training.runners.ssh import SSHTarget


class _Inst:
    def __init__(self, iid=5, status="running", ssh=True, msg=""):
        self.id, self.status, self.message = iid, status, msg
        self.ssh = SSHTarget(host="h", port=1) if ssh else None
        self.rate_usd_h = 0.1
        self.label = "piper-j1"

    @property
    def running(self):
        return self.status == "running"


class _Provider:
    def __init__(self, states=None, create_ok=True):
        self.states = list(states or [_Inst()])
        self.create_ok = create_ok
        self.destroyed: list[int] = []
        self.created = 0

    def create(self, offer_id, *, template_hash, disk_gb, label):
        self.created += 1
        if not self.create_ok:
            raise RuntimeError("오퍼가 사라졌습니다")
        return _Inst()

    def status(self, iid):
        return self.states.pop(0) if self.states else _Inst()

    def destroy(self, iid):
        self.destroyed.append(iid)
        return True


def _target_for(inst):
    return SSHTarget(host="h", port=1, user="root")


@pytest.fixture(autouse=True)
def _no_real_ssh(monkeypatch):
    """⚠ **테스트가 네트워크를 건드리면 안 된다.**

    `wait_for_ssh` 는 상태만 믿지 않고 실제로 붙어 본다(그게 요점이다). 스텁이 없으면
    스위트가 `h` 라는 호스트를 DNS 에 물어보고 30초를 태운다 — 실제로 그랬다.
    접속 가능 여부를 다루는 테스트는 이 스텁을 각자 덮어쓴다.
    """
    monkeypatch.setattr("app.services.cloud.procure.available", lambda t: (True, "OK"))


async def _run(provider, *, start=None, running=None, stop=None,
               budget=None, monkeypatch=None):
    calls = {"started": [], "stopped": 0}

    async def _start(target, cap_h):
        calls["started"].append((target, cap_h))
        if start:
            await start(target, cap_h)

    async def _stop():
        calls["stopped"] += 1
        if stop:
            await stop()

    n = {"i": 0}

    def _running():
        if running:
            return running(n)
        n["i"] += 1
        return n["i"] <= 1

    job = await procure_and_train(
        provider=provider, job_id="j1", offer_id=1, template_hash="h",
        disk_gb=40, budget=budget or Budget(usd=10, max_hours=6),
        start_training=_start, is_running=_running, stop=_stop,
        target_for=_target_for,
        # ⚠ 실제 틱은 30초다. 테스트가 그걸 기다리면 스위트가 멈춘다 — 실제로
        #   한 번 멈췄다. 상한을 **보는 주기**는 동작이 아니라 설정이므로 줄여도 된다.
        tick=0.01, ssh_timeout=2.0)
    return job, calls


# ─────────────────────────────────────────────────────────────────────────────
# 불변식
# ─────────────────────────────────────────────────────────────────────────────

def test_the_happy_path_still_destroys():
    """⚠ **성공했다고 켜 두지 않는다.** 학습이 끝나면 기계는 할 일이 없다."""
    p = _Provider()
    job, calls = asyncio.run(_run(p))
    assert p.destroyed == [5], "완주했는데 파기를 안 했다"
    assert job.phase is Phase.DESTROYED
    assert len(calls["started"]) == 1


def test_an_exception_during_training_still_destroys():
    """⚠ 여기가 `finally` 의 이유다 — 터진 채로 기계를 켜 두면 요금만 나간다."""
    p = _Provider()

    async def boom(_t, _c):
        raise RuntimeError("학습이 터졌다")

    with pytest.raises(RuntimeError, match="터졌다"):
        asyncio.run(_run(p, start=boom))
    assert p.destroyed == [5]


def test_failing_to_get_ssh_still_destroys():
    """⚠ **실측**: `running` 이 돼도 SSH 가 거부될 수 있다(authorized_keys 권한).

    그때 빌린 것을 그냥 두면 아무것도 못 하면서 요금만 나간다.
    """
    p = _Provider(states=[_Inst(status="exited", msg="이미지 pull 실패")])
    with pytest.raises(ProcureError):
        asyncio.run(_run(p))
    assert p.destroyed == [5], "SSH 를 못 얻었는데 파기를 안 했다"


def test_a_failed_create_leaves_nothing_to_destroy():
    """조달 전에 실패하면 빌린 것이 없다 — 경고할 일이 아니다."""
    p = _Provider(create_ok=False)
    with pytest.raises(RuntimeError):
        asyncio.run(_run(p))
    assert p.destroyed == []


# ─────────────────────────────────────────────────────────────────────────────
# 상한 — 사람이 없을 때 멈추는 것
# ─────────────────────────────────────────────────────────────────────────────

def test_going_over_budget_stops_the_training_not_just_warns():
    """⚠ 알림만 하고 두면 상한이 아니라 **장식**이다 — RENT 탭의 예산 칸이 한동안
    정확히 그랬다(화면에 찍히기만 했다)."""
    import time as _t

    job = type("J", (), {
        "job_id": "j1",
        "over_budget": lambda self: "예산 초과",
    })()
    stopped = {"n": 0}

    async def _stop():
        stopped["n"] += 1

    why = asyncio.run(run_until_done(job, lambda: True, _stop, tick=0.01))
    assert why == "예산 초과" and stopped["n"] == 1


def test_a_failed_stop_does_not_block_the_destroy():
    """⚠ 중지 실패가 파기를 막으면 안 된다 — 어차피 파기가 기계를 없앤다."""
    job = type("J", (), {"job_id": "j1", "over_budget": lambda self: "초과"})()

    async def _boom():
        raise RuntimeError("tmux 가 안 죽는다")

    assert asyncio.run(run_until_done(job, lambda: True, _boom, tick=0.01)) == "초과"


def test_the_training_gets_its_own_cap_so_it_ends_without_us():
    """⚠ §6-1. 게이트웨이가 죽어도 학습은 끝나야 한다 — 남은 **예산과 시간 중 짧은 쪽**."""
    p = _Provider()
    # $1 예산 · $0.1/h → 10시간치. 시간 상한 6시간이 더 짧다.
    _, calls = asyncio.run(_run(p, budget=Budget(usd=1.0, max_hours=6)))
    assert calls["started"][0][1] == pytest.approx(6.0)

    p2 = _Provider()
    # $0.2 예산 · $0.1/h → 2시간치가 더 짧다.
    _, calls2 = asyncio.run(_run(p2, budget=Budget(usd=0.2, max_hours=6)))
    assert calls2["started"][0][1] == pytest.approx(2.0)


# ─────────────────────────────────────────────────────────────────────────────
# SSH 대기 — 상태가 아니라 접속으로 판정한다
# ─────────────────────────────────────────────────────────────────────────────

def test_running_alone_is_not_enough_it_must_actually_connect(monkeypatch):
    """⚠ **실측(2026-09-16)**: `running` 이 된 뒤에도 SSH 가 거부됐다.

    상태만 보고 학습을 걸었다면 빌린 채로 아무것도 못 하고 요금만 나갔다.
    """
    tries = {"n": 0}

    def _avail(t):
        tries["n"] += 1
        return (tries["n"] >= 3, "아직 키가 안 심겼습니다")

    monkeypatch.setattr("app.services.cloud.procure.available", _avail)
    got = asyncio.run(wait_for_ssh(_Provider(states=[_Inst() for _ in range(5)]),
                                   5, _target_for, timeout=5, poll=0.01))
    assert got.host == "h" and tries["n"] == 3


def test_waiting_gives_up_and_says_what_it_last_saw(monkeypatch):
    """⚠ 느린 호스트를 시계를 켜 둔 채 기다리지 않는다 — 그리고 **왜** 못 붙었는지
    말해야 다음 시도가 빨라진다."""
    monkeypatch.setattr("app.services.cloud.procure.available",
                        lambda t: (False, "권한 거부"))
    with pytest.raises(ProcureError, match="권한 거부"):
        asyncio.run(wait_for_ssh(_Provider(states=[_Inst() for _ in range(9)]),
                                 5, _target_for, timeout=0.05, poll=0.01))


def test_a_vanished_instance_is_reported_not_waited_on():
    class _Gone(_Provider):
        def status(self, iid):
            return None

    with pytest.raises(ProcureError, match="사라졌습니다"):
        asyncio.run(wait_for_ssh(_Gone(), 5, _target_for, timeout=1, poll=0.01))


# ─────────────────────────────────────────────────────────────────────────────
# [빌리기] 엔드포인트 — **앱이 돈을 쓰기 시작하는 자리**
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rent_client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import cloud as router
    from app.services.cloud import rent as rent_mod

    monkeypatch.setattr(router, "_provider", lambda: _Provider())
    monkeypatch.setattr("app.routers.training._require_push_permission",
                        lambda *a, **k: _ok())
    monkeypatch.setattr("app.routers.cloud.require_idle", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("app.services.exclusivity.require_idle", lambda *a, **k: None)
    monkeypatch.setattr(rent_mod, "busy", lambda: False)
    started = {}

    async def _fake_start(**kw):
        started.update(kw)
        from app.services.cloud.lifecycle import CloudJob
        return CloudJob(job_id="local", label="piper-local", budget=kw["budget"])

    monkeypatch.setattr(rent_mod, "start", _fake_start)
    return TestClient(app), started


async def _ok():
    return None


_RENT = {"offer_id": 1, "template_hash": "h", "dataset_repo_id": "a/b",
         "policy_repo_id": "me/m", "steps": 100}


def test_renting_without_a_push_target_is_refused(rent_client):
    """⚠ 없으면 `cli_mapping` 이 `push_to_hub=false` 를 강제한다 — 학습은 멀쩡히
    끝나고 **가중치만 사라진다.** 그리고 푸시 기회는 종료 시점 한 번뿐이다."""
    c, _ = rent_client
    r = c.post("/api/cloud/rent", json={**_RENT, "policy_repo_id": "  "})
    assert r.status_code == 400 and "결과를 가져올 수 없습니다" in r.json()["detail"]


def test_renting_passes_the_remote_interpreter_and_the_caps(rent_client):
    """⚠ 로컬 절대경로를 넘기면 임대 서버에서 즉사한다. 상한은 **요청의 일부**여야
    기본값으로 빠져나갈 수 없다."""
    c, started = rent_client
    r = c.post("/api/cloud/rent", json={**_RENT, "budget_usd": 3, "max_hours": 2})
    assert r.status_code == 200, r.text
    assert started["args"][0] == "/opt/venv/bin/python"
    assert started["budget"].usd == 3 and started["budget"].max_hours == 2


def test_renting_sends_the_token_because_the_rented_box_has_none(rent_client, monkeypatch):
    monkeypatch.setattr("huggingface_hub.get_token", lambda: "hf_X")
    c, started = rent_client
    c.post("/api/cloud/rent", json=_RENT)
    assert started["env"]["HF_TOKEN"] == "hf_X"


def test_a_second_rent_is_refused_while_one_is_running(rent_client, monkeypatch):
    """⚠ 둘째를 받아 주면 **첫 인스턴스의 핸들을 잃는다** — 그게 고아의 정의다."""
    from app.services.cloud import rent as rent_mod

    monkeypatch.setattr(rent_mod, "busy", lambda: True)
    c, _ = rent_client
    assert c.post("/api/cloud/rent", json=_RENT).status_code == 409


def test_stopping_with_nothing_running_is_a_404(rent_client, monkeypatch):
    from app.services.cloud import rent as rent_mod

    async def _none(_p):
        return None

    monkeypatch.setattr(rent_mod, "stop_now", _none)
    c, _ = rent_client
    assert c.post("/api/cloud/rent/stop").status_code == 404


def test_stopping_destroys_rather_than_only_halting_training():
    """⚠ 학습만 세우고 끝내면 **기계가 남아 과금된다** — 중지는 파기까지다."""
    import inspect

    from app.services.cloud import rent as rent_mod

    src = inspect.getsource(rent_mod.stop_now)
    assert "job.finish" in src, "중지가 파기를 안 지난다"


def test_the_runner_is_put_back_after_a_rental():
    """⚠ 안 되돌리면 다음 로컬 학습이 **죽은 호스트**로 붙으려 한다. 러너 선택은
    프로세스 수명 동안 다시 평가되지 않는다."""
    import inspect

    from app.services.cloud import rent as rent_mod

    src = inspect.getsource(rent_mod.start)
    assert "finally:" in src and "train_manager.runner = original" in src
