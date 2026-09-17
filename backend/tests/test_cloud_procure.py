"""조달 — **나가는 길은 하나다** (feature/vast-training.md §3·§6).

⚠ 이 저장소는 `pytest-asyncio` 를 쓰지 않는다 — async 코드는 `asyncio.run()` 으로
부른다(`test_llm_client.py` 등과 같은 관습). 테스트 하나 때문에 의존성을 늘리지 않는다.

⚠ 이 파일이 지키는 것은 `finally` 다. 성공이든 예외든 예산 초과든 SSH 실패든,
파기를 지나야 한다. 학습 실패는 다시 돌리면 되지만 **파기 실패는 아무도 안 보는
동안 요금이 나간다.**
"""

import asyncio

import pytest

from app.services.cloud import procure
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
    # ⚠ 스택 준비 확인도 **원격 명령**이다 — 같은 이유로 막는다. 이걸 빠뜨렸더니
    #   스위트가 `h` 로 ssh 를 시도하며 통째로 멈췄다.
    #
    # ⚠ `stack_ready` 자체가 아니라 그 **아래의 `_run`** 을 막는다. 위를 막으면
    #   `stack_ready` 를 직접 시험하는 테스트까지 스텁을 보게 되고, 그러면 정작
    #   판정 로직은 아무도 안 본 채 초록불이 된다.
    class _Ready:
        returncode, stdout, stderr = 0, "2026-09-17 full (build)", ""

    monkeypatch.setattr("app.services.training.runners.ssh._run",
                        lambda target, cmd, **kw: _Ready())


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
        tick=0.01, ssh_timeout=2.0, stack_timeout=2.0, stack_poll=0.01)
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


def test_the_ssh_wait_matches_the_runbook_watchpoint():
    """⚠ 사람이 손으로 돌릴 때의 기준(§11-5 감시① = 8분)과 코드의 기준이 다르면,
    둘 중 하나는 **틀린 기대**를 만든다.

    실측: 빠른 호스트는 6분 20초에 통과했고, 느린 호스트는 12분을 줘도 못 벗어났다 —
    기다림을 늘려도 그런 호스트는 안 온다. 12분이었을 때 $0.013 vs $0.008 을 태웠다.
    """
    from app.services.cloud import procure

    assert procure.SSH_WAIT_S == 480.0, "감시① 8분과 어긋난다"


# ─────────────────────────────────────────────────────────────────────────────
# 접속된 것 ≠ 학습할 수 있는 것 — **$0.0145 짜리 교훈** (2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

class _Rc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_readiness_is_decided_by_exit_code_not_by_wording():
    """⚠ `.ready` 의 내용을 문자열로 뒤지면 `bootstrap.sh` 의 문구에 묶인다 — 거기
    한 글자만 바뀌어도 여기가 **조용히** 틀린다."""
    ok, detail = procure.stack_ready("t", lambda t, c: _Rc(0, "아무 문구나"))
    assert ok is True and detail == "아무 문구나"
    ok, _ = procure.stack_ready("t", lambda t, c: _Rc(3, "== 설치 시작"))
    assert ok is False


def test_the_check_asks_for_the_log_in_the_same_round_trip():
    """⚠ 안 됐을 때 다시 붙어서 로그를 읽으면, 그 왕복 동안에도 요금이 나간다."""
    seen = []
    procure.stack_ready("t", lambda t, cmd: (seen.append(cmd), _Rc(3))[1])
    assert procure.READY_FILE in seen[0] and procure.BOOTSTRAP_LOG in seen[0]
    assert len(seen) == 1, "왕복이 두 번이다"


def test_training_does_not_start_before_the_stack_is_installed(monkeypatch):
    """⚠ **실측(2026-09-17)**: slim 이미지는 접속이 된 뒤에도 torch·lerobot 을 받는
    중이다. 그 구간에 학습을 걸어 3초 만에 `No module named 'lerobot'` 로 죽었다 —
    기계를 빌리고 이미지를 받고 접속까지 한 뒤였다($0.0145).

    `bootstrap.sh` 머리말은 "SSHRunner 는 `.ready` 를 보고 시작한다" 고 적고 있었는데
    **그걸 보는 코드가 어디에도 없었다.** 이 테스트가 그 자리다.
    """
    order = []
    tries = {"n": 0}

    def _ready(target, run=None):
        tries["n"] += 1
        order.append("check")
        return (tries["n"] >= 3), "설치 중"

    monkeypatch.setattr(procure, "stack_ready", _ready)

    async def _start(target, cap_h):
        order.append("train")

    provider = _Provider()
    asyncio.run(_run(provider, start=_start))
    assert "train" in order, "학습이 아예 안 걸렸다"
    assert order.index("train") > 0 and order[0] == "check"
    assert tries["n"] >= 3, "준비되기 전에 학습을 걸었다"


def test_giving_up_on_the_stack_says_where_the_log_is(monkeypatch):
    """⚠ 설치가 실패했으면 **사유가 있는 자리**를 그대로 알려 준다 — 사람이 찾아
    헤매는 동안에도 다음 시도의 요금이 나간다."""
    monkeypatch.setattr(procure, "stack_ready",
                        lambda t, run=None: (False, "ERROR: no space left on device"))
    with pytest.raises(procure.ProcureError) as e:
        asyncio.run(procure.wait_for_stack("t", timeout=0.05, poll=0.01))
    assert procure.BOOTSTRAP_LOG in str(e.value) and "no space left" in str(e.value)


def test_a_blip_while_waiting_does_not_abort_the_whole_rental(monkeypatch):
    """⚠ 설치 중에 ssh 가 한 번 튕겼다고 빌린 기계를 버리면 안 된다 — 다시 본다."""
    n = {"i": 0}

    def _flaky(target, run=None):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("ssh 일시 실패")
        return True, "됐다"

    monkeypatch.setattr(procure, "stack_ready", _flaky)
    assert asyncio.run(procure.wait_for_stack("t", timeout=5, poll=0.01)) == "됐다"


def test_the_in_house_box_is_not_gated_on_a_rental_only_path():
    """⚠ 이 검사를 `runners.ssh.available()` 에 넣으면 안 된다. 그건 사내 박스
    (`PIPER_TRAIN_SSH_HOST`)에도 쓰이는데 거기엔 `/opt/piper` 가 없다 — 넣으면 임대와
    상관없는 원격 학습이 통째로 막힌다."""
    import inspect

    from app.services.training.runners import ssh

    assert "/opt/piper" not in inspect.getsource(ssh.available)


def test_the_progress_line_is_the_install_log_not_shell_noise():
    """⚠ **실측**: stdout 과 stderr 를 이어 붙였더니 사람이 본 한 줄이
    `bash: warning: setlocale: …` 이었다. 설치 로그는 stdout 이고 저건 stderr 인데,
    이어 붙이면 잡음이 마지막 줄이 된다 — 진행 상황 대신 로케일 경고를 보게 된다."""
    _, detail = procure.stack_ready(
        "t", lambda t, c: _Rc(3, "== torch 2.11.0 (cu126)", "bash: warning: setlocale"))
    assert detail == "== torch 2.11.0 (cu126)"
    # stdout 이 비면 그때는 stderr 라도 보여 준다 — 빈칸보다 낫다
    _, only_err = procure.stack_ready("t", lambda t, c: _Rc(255, "", "ssh: connect refused"))
    assert only_err == "ssh: connect refused"


def test_the_budget_stops_the_waiting_too_not_only_the_training(monkeypatch):
    """⚠ **대기 구간에도 요금은 똑같이 나간다.** 예전에는 상한을 학습 중에만 봤는데,
    그때는 학습 전 대기가 최대 8분이라 눈에 안 띄었다. 스택 설치 대기(15분)가 붙으면서
    학습 한 줄도 안 돌고 23분까지 갈 수 있게 됐다 — 그 사이 예산을 넘기면
    "$0.20 상한" 은 상한이 아니라 장식이다.

    ⚠ 그리고 이 예외로 나가도 `finally` 가 파기를 지난다 — 그게 이 파일의 요점이다.
    """
    monkeypatch.setattr(procure, "stack_ready", lambda t, run=None: (False, "설치 중"))
    provider = _Provider()
    with pytest.raises(ProcureError, match="예산"):
        asyncio.run(procure.wait_for_stack(
            "t", timeout=5, poll=0.01,
            guard=lambda: "예산 $0.2 를 넘겼습니다 (약 $0.21)"))

    # 대기에서 터져도 기계는 파기된다
    async def _go():
        return await procure_and_train(
            provider=provider, job_id="j", offer_id=1, template_hash="h", disk_gb=40,
            budget=Budget(usd=10, max_hours=6),
            start_training=lambda t, c: asyncio.sleep(0),
            is_running=lambda: False, stop=lambda: asyncio.sleep(0),
            target_for=_target_for, tick=0.01, ssh_timeout=1.0,
            stack_timeout=0.05, stack_poll=0.01)

    with pytest.raises(ProcureError):
        asyncio.run(_go())
    assert provider.destroyed, "대기에서 실패했는데 기계가 남았다"


# ─────────────────────────────────────────────────────────────────────────────
# 화면이 고르는 기본 템플릿 — **실측이 정한다**
# ─────────────────────────────────────────────────────────────────────────────

def test_the_rent_tab_defaults_to_the_small_image():
    """⚠ **실측(2026-09-17)**: 같은 4090 호스트(5.9Gbps)에서 full(14GB)은 8분 상한에
    걸릴 때까지 pull 을 못 끝냈고, slim(1.3GB)은 pull 16초 · SSH 62초 · 스택 준비
    4분 23초로 완주했다. full 의 가장 좋았던 기록(6분 20초)과 비교해도 slim 이 빠르다.

    예전 기본값은 full 이었고 주석은 "네트워크가 빠르면 full 이 낫다" 였다 — 정작
    네트워크가 빠른 호스트에서 14GB 가 안 끝났다. 회선이 아니라 크기가 문제였다.

    ⚠ 그리고 slim 이 되는 것은 **스택 준비 게이트가 생긴 뒤부터다.** 기본값만 바꾸고
    게이트가 없으면, 처음 쓰는 사람이 기본값 그대로 눌러서 3초 만에 죽는다.
    """
    from pathlib import Path

    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                     / "components" / "CloudRentTab.tsx").read_text())
    picked = src.split("setTemplateId((cur)")[1].split("\n")[0]
    assert "'slim'" in picked, f"기본 템플릿이 slim 이 아니다: {picked.strip()}"
    # 게이트가 같이 있어야 이 기본값이 안전하다
    assert callable(procure.wait_for_stack)


def test_the_picker_shows_what_the_two_variants_cost_you():
    """⚠ 백엔드가 "스택 포함, 부팅 즉시 학습" / "부팅 때 bootstrap.sh 가 스택 설치" 를
    이미 주는데 화면은 `full`·`slim` 네 글자만 보여 주고 있었다 — 그 차이가 임대 시간과
    요금을 가르는데 고르는 사람은 알 길이 없었다."""
    from pathlib import Path

    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                     / "components" / "CloudRentTab.tsx").read_text())
    opts = src.split("templates.map(")[1][:400]
    assert "t.description" in opts, "설명을 화면이 버리고 있다"


# ─────────────────────────────────────────────────────────────────────────────
# 화면이 보여 준 값으로 돈다 — **숨은 기본값 없이** (2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

def test_the_preview_and_the_rental_build_the_same_command():
    """⚠ 인자를 두 벌로 만들면 반드시 갈린다. 실제로 갈릴 뻔한 자리가 있었다 —
    `TrainPreviewRequest` 와 `RentRequest` 의 기본값이 다르다(steps 100000 대 5000,
    save_freq 20000 대 1000). 그래서 **같은 함수**를 쓴다.
    """
    import inspect

    from app.routers import cloud as router

    assert "_rent_train_args(body)" in inspect.getsource(router.rent_and_train)
    assert "_rent_train_args(body)" in inspect.getsource(router.rent_preview)


def test_the_preview_shows_the_remote_interpreter():
    """⚠ 원격은 `/opt/venv/bin/python` 이고 이 기계는 conda 경로다. 첫 단어가 다르면
    그 미리보기는 다른 명령을 보여 주는 것이다."""
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    r = TestClient(app).post("/api/cloud/rent/preview", json={
        "offer_id": 1, "template_hash": "h",
        "dataset_repo_id": "me/d", "policy_repo_id": "me/p", "steps": 250})
    assert r.status_code == 200
    assert r.json()["args"][0] == settings.train_remote_python
    assert "--steps=250" in r.json()["command"]


def test_the_preview_never_hands_back_the_token():
    """⚠ `_train_env()` 에는 HF 토큰이 들어 있다. 미리보기 한 번에 키가 화면으로 새면
    학습 설정을 감춘 것보다 큰 사고다."""
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).post("/api/cloud/rent/preview", json={
        "offer_id": 1, "template_hash": "h",
        "dataset_repo_id": "me/d", "policy_repo_id": "me/p"})
    assert set(r.json()) == {"args", "command"}
    assert "HF_TOKEN" not in r.text and "hf_" not in r.text


def test_the_rent_tab_only_makes_a_machine():
    """⚠ **빌리는 곳과 학습하는 곳을 갈랐다.** 클라우드 페이지는 기계만 만들고, 학습은
    학습 페이지 한 곳에서만 설정한다 — 학습 폼이 두 곳에 있으면 반드시 어긋난다
    (한때 RENT 탭이 입력 둘만 들고 나머지를 서버 기본값으로 돌렸다).
    """
    from pathlib import Path

    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                     / "components" / "CloudRentTab.tsx").read_text())
    assert "'/cloud/instances'" in src, "기계 만들기를 안 부른다"
    for gone in ("dataset_repo_id", "batch_size", "save_freq", "policy_type"):
        assert gone not in src, f"RENT 탭에 학습 설정({gone})이 남아 있다"


def test_making_a_machine_says_it_will_not_turn_itself_off():
    """⚠ 한 묶음([빌리기]→학습→파기)과 **반대**라서, 모르고 있으면 빈 기계가 밤새 돈다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src"
           / "components" / "CloudRentTab.tsx").read_text()
    assert "자동으로 꺼지지 않습니다" in src and "인스턴스 탭" in src


# ─────────────────────────────────────────────────────────────────────────────
# 임대가 학습 설정을 **하나도 안 흘린다** (2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

def test_renting_accepts_every_field_the_training_page_sends():
    """⚠ `RentRequest` 가 학습 필드를 따로 적어 두던 시절, 학습 페이지가 보내는 22개 중
    **9개만** 받았고 나머지는 Pydantic 이 조용히 버렸다. 거기 `pretrained_path` 가
    있었다 — 파인튜닝을 걸어도 **말없이 처음부터** 학습이 되고, 아는 시점은 몇 시간 뒤다.
    """
    from app.routers.cloud import RentRequest
    from app.routers.training import TrainStartRequest

    missing = set(TrainStartRequest.model_fields) - set(RentRequest.model_fields)
    assert not missing, f"임대가 못 받는 학습 필드: {missing}"


def test_a_fine_tune_from_the_hub_reaches_the_command():
    """예전에는 이 인자가 통째로 사라졌다."""
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).post("/api/cloud/rent/preview", json={
        "offer_id": 1, "template_hash": "h", "dataset_repo_id": "me/d",
        "policy_repo_id": "me/p", "pretrained_path": "lerobot/act_aloha"})
    assert "--policy.path=lerobot/act_aloha" in r.json()["command"]


def test_a_checkpoint_on_this_machine_is_refused_before_renting():
    """⚠ 빌린 기계에는 이 기계의 경로가 없다. 그대로 걸면 몇 분 뒤 `No such file` 로
    죽거나, 더 나쁘게는 lerobot 이 그걸 Hub 이름으로 읽어 엉뚱한 것을 받는다.

    ⚠ **빌리기 전에** 막는다 — 돈이 나간 뒤에 아는 것과 다르다.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import cloud as router

    rented = []

    class _P:
        def create(self, *a, **kw):                                  # pragma: no cover
            rented.append(a)
            raise AssertionError("못 쓸 설정인데 기계를 빌렸다")

    import pytest as _pytest
    _pytest.MonkeyPatch().setattr(router, "_provider", lambda: _P())
    r = TestClient(app).post("/api/cloud/rent", json={
        "offer_id": 1, "template_hash": "h", "dataset_repo_id": "me/d",
        "policy_repo_id": "me/p", "pretrained_path": "/home/me/checkpoints/last"})
    assert r.status_code == 400 and "이 기계의 경로" in r.json()["detail"]
    assert rented == []


def test_a_hub_name_is_not_mistaken_for_a_local_path():
    """⚠ `org/name` 은 원격에서도 멀쩡하다 — 막으면 쓸 수 있는 것을 못 쓰게 된다."""
    from app.routers.cloud import RentRequest, _reject_local_only_settings

    body = RentRequest(offer_id=1, template_hash="h", dataset_repo_id="me/d",
                       policy_repo_id="me/p", pretrained_path="lerobot/act_aloha")
    _reject_local_only_settings(body)          # 예외가 나면 실패한다


def test_the_two_start_paths_build_params_with_one_function():
    """⚠ 학습 페이지에 필드가 하나 늘었을 때 임대 경로에서만 빠지는 일을 **구조로** 막는다."""
    import inspect

    from app.routers import cloud as router
    from app.routers import training as t

    assert "train_cli_params(body)" in inspect.getsource(t.start_training)
    assert "train_cli_params(body)" in inspect.getsource(router._rent_train_args)


def test_renting_keeps_its_cheaper_step_default():
    """⚠ 학습 페이지 기본값은 10만 스텝이다. 상속만 하고 두면 값을 안 보낸 호출 하나가
    **시간당 과금되는 기계에서** 10만 스텝을 돈다."""
    from app.routers.cloud import RentRequest

    assert RentRequest.model_fields["steps"].default == 5000


# ─────────────────────────────────────────────────────────────────────────────
# 학습 페이지에서 빌려 돌린다 — **설정은 한 곳** (W4, 2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

def _page(name: str) -> str:
    from pathlib import Path

    from conftest import code_only

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    for sub in ("pages", "components"):
        f = root / sub / name
        if f.exists():
            return code_only(f.read_text())
    raise AssertionError(f"{name} 없음")


def test_the_training_page_can_send_its_own_settings_to_a_rented_gpu():
    """⚠ **폼은 한 벌이다.** 임대용 학습 폼을 따로 만들면 반드시 어긋난다 — RENT 탭이
    실제로 그랬다(입력 둘만 들고 나머지는 서버 기본값, §12-17)."""
    src = _page("TrainingPage.tsx")
    assert "api.post('/cloud/train-on', { ...trainParams(), ...rentPick }" in src, \
        "학습 페이지의 설정을 그대로 안 보낸다"


def test_where_to_run_sits_next_to_the_start_button():
    """⚠ 위쪽 어딘가에 두면 "어디서 도는지" 를 모른 채 누른다 — 임대는 그 한 번이 돈이다."""
    src = _page("TrainingPage.tsx")
    assert src.index("TrainWhereForm") < src.index("고른 기계에서 학습 시작")
    gap = src[src.index("<TrainWhereForm"):src.index("고른 기계에서 학습 시작")]
    assert gap.count("<button") <= 1, "실행 위치와 시작 버튼 사이에 다른 것이 끼어 있다"


def test_the_two_choices_are_local_and_cloud():
    """실행 위치는 **둘**이다 — 로컬 / 클라우드."""
    src = _page("TrainWhereForm.tsx")
    assert "'로컬'" in src and "'클라우드'" in src


def test_local_still_says_so_when_it_is_really_the_in_house_box():
    """⚠ 러너는 서버 설정이 정한다(`PIPER_TRAIN_SSH_HOST`). 그게 채워진 기계에서는
    [로컬]이 사실 **사내 SSH 박스**로 간다 — 칸을 셋으로 늘리지는 않되(회차마다 러너를
    고르는 것은 서버가 아직 못 한다) 그 사실은 말한다. 화면이 "로컬" 이라 적어 두고
    실제로는 다른 기계에서 도는 것이 제일 나쁘다."""
    src = _page("TrainWhereForm.tsx")
    assert "runner === 'ssh'" in src, "사내 박스로 가는데 아무 말도 안 한다"
    assert "사내 서버(SSH)로 보냅니다" in src


def test_a_hand_edited_cli_cannot_be_rented_and_says_why():
    """⚠ `/cloud/rent` 는 인자를 스스로 조립한다 — 직접 고친 CLI 는 반영될 자리가 없다.
    조용히 무시하면 **사용자가 고친 것과 다른 명령**이 임대 GPU 에서 돈다."""
    src = _page("TrainingPage.tsx")
    assert "disabledReason=" in src and "cliEdited" in src


def test_the_picker_offers_only_machines_you_rented_on_purpose():
    """⚠ 한 묶음으로 도는 기계(`piper-<job>`)는 자기 학습이 끝나면 **스스로 파기된다** —
    거기에 다른 학습을 걸면 도중에 기계가 사라진다. 고를 수 있는 것은 사람이 일부러
    빌린 `piper-box-` 뿐이다."""
    src = _page("TrainWhereForm.tsx")
    assert "'piper-box-'" in src and "startsWith(BOX)" in src
    assert "i.status === 'running'" in src, "아직 안 뜬 기계를 고르게 둔다"


def test_the_picker_does_not_rent():
    """⚠ 빌리기가 두 곳에 있으면 "끄는 책임" 도 두 곳으로 갈라진다 — 한쪽은 끝나면 끄고
    한쪽은 안 끄는 식이 되면 아무도 규칙을 못 외운다. 여기서는 **고르기만** 한다."""
    src = _page("TrainWhereForm.tsx")
    assert "/cloud/instances" in src, "목록을 안 읽는다"
    assert "api.post" not in src, "고르는 화면이 무언가를 만들고 있다"
    assert 'href="/cloud"' in src, "빌리러 갈 곳을 안 알려 준다"


def test_the_picker_says_the_machine_stays_up():
    """⚠ 학습이 끝나도 안 꺼진다는 것을 **고르는 자리에서** 말한다 — 확인 창에만 있으면
    이미 시작한 뒤다."""
    src = _page("TrainWhereForm.tsx")
    assert "꺼지지 않습니다" in src and "/h 가 계속" in src


def test_the_confirm_preview_follows_where_it_will_run():
    """⚠ 임대 기계는 `/opt/venv/bin/python` 이고 이 기계는 conda 경로다 — 같은
    미리보기를 쓰면 확인 창이 **안 도는 명령**을 보여 준다."""
    src = _page("TrainingPage.tsx")
    assert "'/cloud/rent/preview'" in src and "where === 'rent'" in src
