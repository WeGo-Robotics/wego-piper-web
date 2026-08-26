"""판단이 **로봇에 닿는가** — 정책 종류에 따라 경로가 다르다.

분리수거 루프는 판단을 `task` 문장으로 내보낸다. VLA 는 그 문장을 읽지만
ACT 는 안 읽는다(`policies.takes_language`). 그래서 ACT 를 올려둔 채 루프를
돌리면 LLM 이 매 회차 다른 결정을 내려도 로봇은 같은 동작만 반복하고, 저널에는
결정이 꼬박꼬박 쌓였다 — 에러도 경고도 없이. **돌아가는 것처럼 보이는 실패**다.
"""

import pytest

from app.services.orchestrator import (
    JudgeSlots, OrchestratorConfig, _require_actionable_policy, slots_model,
)


@pytest.fixture(autouse=True)
def _no_real_inference(monkeypatch):
    """`inference_state.get()` 만 갈아끼운다 — 실제 프로세스와 무관하게."""
    from app.services import inference_state

    box = {"running": None}

    def _set(policy_type):
        box["running"] = (inference_state.RunningPolicy(policy_type, "/ckpt")
                          if policy_type is not None else None)

    monkeypatch.setattr(inference_state, "get", lambda: box["running"])
    return _set


def test_a_language_policy_is_allowed(_no_real_inference):
    """VLA 는 지금 경로 그대로 — 문장이 버스로 간다."""
    _no_real_inference("pi0")
    _require_actionable_policy(OrchestratorConfig())


def test_act_without_a_skill_map_is_refused(_no_real_inference):
    """⚠ **회귀** — 이걸 허용했더니 판단이 아무 효과가 없었다."""
    _no_real_inference("act")
    with pytest.raises(RuntimeError) as e:
        _require_actionable_policy(OrchestratorConfig())
    assert "닿지 않습니다" in str(e.value), "왜 막혔는지 안 알려준다"


def test_an_unknown_policy_is_treated_as_not_reading_language(_no_real_inference):
    """직접 편집한 CLI 로 띄우면 정책을 확실히 알 수 없다.

    모를 때 낙관하면 조용히 헛도는 쪽으로 떨어진다 — 막는 쪽으로 기운다.
    """
    _no_real_inference("")
    with pytest.raises(RuntimeError):
        _require_actionable_policy(OrchestratorConfig())


def test_a_dead_inference_is_not_mistaken_for_a_running_one(_no_real_inference):
    """기록만 남기면 추론이 죽은 뒤에도 '무엇이 돈다'고 말하게 된다."""
    _no_real_inference(None)
    with pytest.raises(RuntimeError):
        _require_actionable_policy(OrchestratorConfig())


# ── 판단 스키마 ──────────────────────────────────────────────────────────────

def test_without_skills_the_schema_is_unchanged():
    """언어 정책 루프는 예전 그대로여야 한다 — 슬롯이 늘면 소형 모델이 흔들린다."""
    assert slots_model({}) is JudgeSlots


def test_the_allowed_skills_come_from_the_registry():
    """⚠ 허용값을 코드에 박으면 가중치를 추가할 때마다 여기를 고쳐야 하고,
    안 고치면 **LLM 이 없는 스킬을 고르거나 있는 스킬을 못 고른다.**

    `Field(description=...)` 은 장식이 아니라 guided decoding 스키마로 들어간다.
    """
    m = slots_model({"can_bin_pick": "/a.ckpt", "plastic_bin_pick": "/b.ckpt"})
    desc = m.model_fields["skill"].description
    assert "can_bin_pick" in desc and "plastic_bin_pick" in desc


def test_the_skill_slot_keeps_the_original_slots():
    """target/destination 은 저널과 task 문장이 계속 쓴다."""
    m = slots_model({"x": "/x.ckpt"})
    assert {"target", "destination", "reason", "skill"} <= set(m.model_fields)
