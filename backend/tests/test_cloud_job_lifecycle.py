"""임대 인스턴스 수명 — **불변식은 하나다: 어느 경로로 끝나든 파기를 지난다.**

(feature/vast-training.md §3·§6)

⚠ 이 파일이 지키는 것은 돈이다. 학습이 실패하면 다시 돌리면 되지만, 파기가 실패하면
**아무도 안 보는 동안 요금이 나간다.** 그래서 "성공했을 때 파기한다" 가 아니라
"무슨 일이 있어도 파기를 시도한다" 를 검사한다.
"""

import time

import pytest

from app.services.cloud.lifecycle import Budget, CloudJob, Phase, find_orphans


class _Provider:
    """파기가 되는 프로바이더. `destroyed` 에 부른 id 가 쌓인다."""

    def __init__(self, gone=True, boom=False, rows=None):
        self.gone, self.boom = gone, boom
        self.destroyed: list[int] = []
        self.rows = rows or []

    def destroy(self, iid):
        self.destroyed.append(iid)
        if self.boom:
            raise RuntimeError("네트워크 끊김")
        return self.gone

    def list_instances(self):
        return self.rows


def _job(**kw):
    j = CloudJob(job_id="j1", label="piper-j1", **kw)
    j.note_started(instance_id=77, rate_usd_h=0.1)
    return j


# ─────────────────────────────────────────────────────────────────────────────
# 불변식 — 파기는 반드시 지난다
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phase", list(Phase))
def test_every_phase_can_still_be_finished(phase):
    """⚠ **어느 칸에서 끝나든** 파기를 시도한다. 조달 도중이든 학습 중이든 상관없다."""
    p = _Provider()
    j = _job()
    j.phase = phase
    j.finish(p, "테스트")
    # ⚠ `FINISHED` 도 끝난 칸이다 — 사람이 빌려 둔 기계에서 학습만 끝난 상태이고,
    #   그 기계는 애초에 우리가 끌 대상이 아니다(§12-20).
    if phase in (Phase.DESTROYED, Phase.ORPHAN, Phase.FINISHED):
        assert p.destroyed == [], "이미 끝난 것을 또 파기하려 들었다"
    else:
        assert p.destroyed == [77], f"{phase.value} 에서 파기를 안 했다"


def test_finish_is_idempotent_because_finally_blocks_nest():
    """⚠ `finally` 는 겹치기 쉽다 — 두 번 불려도 두 번 파기하지 않는다."""
    p = _Provider()
    j = _job()
    j.finish(p, "첫 번째")
    j.finish(p, "두 번째")
    assert p.destroyed == [77]
    assert j.phase is Phase.DESTROYED


def test_a_failed_destroy_becomes_an_orphan_not_a_success():
    """⚠ **파기했다고 믿지 않는다.** 확인이 안 되면 살아 있을 수 있고, 그건 과금 중일
    수 있다는 뜻이다. 조용히 성공으로 처리하는 것이 이 경로에서 가장 비싼 거짓말이다."""
    j = _job()
    assert j.finish(_Provider(gone=False)) is Phase.ORPHAN
    assert "확인하지 못했습니다" in j.reason
    assert "vastai show instances" in j.reason, "사람이 무엇을 해야 하는지 안 적었다"


def test_an_exception_while_destroying_also_becomes_an_orphan():
    """⚠ 예외를 올리면 호출부의 `finally` 가 **다른 예외에 묻혀** 아무도 이 사실을
    못 본다. 삼키되 `ORPHAN` 으로 남긴다."""
    j = _job()
    assert j.finish(_Provider(boom=True)) is Phase.ORPHAN


def test_nothing_to_destroy_is_not_an_orphan():
    """조달 전에 실패했으면 빌린 것이 없다 — 경고할 일이 아니다."""
    j = CloudJob(job_id="j1", label="piper-j1")
    assert j.finish(_Provider()) is Phase.DESTROYED


def test_finish_survives_being_used_as_a_finally_guard():
    """실제 쓰임 — 학습이 터져도 파기는 지나간다."""
    p = _Provider()
    j = _job()
    with pytest.raises(ValueError):
        try:
            raise ValueError("학습이 터졌다")
        finally:
            j.finish(p, "예외로 종료")
    assert p.destroyed == [77] and j.phase is Phase.DESTROYED


# ─────────────────────────────────────────────────────────────────────────────
# 상한 — 사람이 없을 때 멈추는 것
# ─────────────────────────────────────────────────────────────────────────────

def test_time_cap_fires_and_says_why():
    b = Budget(usd=1000, max_hours=2, rate_usd_h=0.1,
               started_at=time.monotonic() - 3 * 3600)
    assert "시간 상한" in (b.exceeded() or "")


def test_money_cap_fires_and_says_how_much():
    b = Budget(usd=1.0, max_hours=1000, rate_usd_h=10.0,
               started_at=time.monotonic() - 0.5 * 3600)
    msg = b.exceeded() or ""
    assert "예산" in msg and "$5" in msg, msg


def test_within_both_caps_is_silent():
    b = Budget(usd=10, max_hours=6, rate_usd_h=0.1,
               started_at=time.monotonic() - 60)
    assert b.exceeded() is None


def test_the_clock_only_starts_when_the_machine_does():
    """⚠ 조달 전 시간은 과금되지 않는다 — 오퍼를 고르는 동안 예산을 까먹으면 안 된다."""
    b = Budget(rate_usd_h=0.1)
    assert b.elapsed_h() == 0.0 and b.accrued_usd() == 0.0


def test_accrued_is_labelled_as_an_estimate_not_the_invoice():
    """⚠ 이건 **우리 시계 × 요금**이지 Vast 의 정산이 아니다. 상한을 걸기 위한 근사다 —
    마감은 청구서로 한다(§11-5 의 세 번째 겹)."""
    import inspect

    from app.services.cloud import lifecycle

    doc = inspect.getdoc(lifecycle.Budget.accrued_usd) or ""
    assert "청구가 진실" in doc


# ─────────────────────────────────────────────────────────────────────────────
# 고아 — 레지스트리를 잃어도 알아본다
# ─────────────────────────────────────────────────────────────────────────────

class _Inst:
    def __init__(self, iid, label):
        self.id, self.label = iid, label


def test_orphans_are_ours_by_label_but_unknown_to_the_registry():
    """⚠ 라벨이 **유일한 단서**다 — 레지스트리를 잃으면 그것뿐이다(§6-3)."""
    p = _Provider(rows=[_Inst(1, "piper-a"), _Inst(2, "piper-b"), _Inst(3, "someone-else")])
    got = find_orphans(p, known={1})
    assert [i.id for i in got] == [2], "남의 인스턴스를 우리 것이라 했거나, 아는 것을 고아라 했다"


def test_orphan_scanning_never_destroys_anything():
    """⚠ **자동 파기는 안 한다** — 다른 기계의 게이트웨이가 돌리는 학습일 수 있다
    (§10 결정 5). 남의 것을 끄는 쪽이 더 나쁘다. 경고와 버튼까지가 우리 몫이다."""
    p = _Provider(rows=[_Inst(9, "piper-zzz")])
    find_orphans(p, known=set())
    assert p.destroyed == []


def test_the_report_carries_the_numbers_a_person_needs():
    j = _job()
    d = j.to_dict()
    assert d["instance_id"] == 77
    assert set(d["cost"]) == {"rate_usd_h", "accrued_usd", "budget_usd",
                              "elapsed_h", "max_hours"}


def test_a_job_on_a_machine_we_did_not_create_never_destroys_it():
    """⚠ 불변식이 **뒤집히는 유일한 자리**다. 한 묶음(`rent.start`)은 반드시 파기하지만,
    사람이 빌려 둔 기계에 학습만 얹은 경우(`rent.train_on`)는 **절대 안 끈다** — 학습
    한 번 끝났다고 끄면 다음 학습을 준비하던 사람의 기계가 사라진다.

    ⚠ 어느 칸에서 끝나든 그렇다. 예외를 한 칸이라도 두면 그 칸에서 남의 기계가 꺼진다.
    """
    for phase in Phase:
        p = _Provider()
        j = _job()
        j.owns_instance = False
        j.phase = phase
        j.finish(p, "테스트")
        assert p.destroyed == [], f"{phase.value} 에서 남의 기계를 껐다"
