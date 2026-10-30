"""RTC 배선 — wrapper/rtc_feed.py 와 lerobot_wrapper.

RTC 슬라이더는 처음부터 효과가 없었다: wrapper 가 `predict_action_chunk(obs)` 로만 불렀고 체크포인트의
`rtc_config` 는 None 이었다. 여기 테스트는

1. 이어 받을 원본이 큐와 칸 단위로 맞물리는가 — 0 번째가 관측 스텝에 방금 보낸 칸
2. wrapper 가 정말 그걸 넘기는가 — 다시 "설정만 하고 안 쓰는" 슬라이더로 돌아가지 않게
3. RTC 일 때 inference_mode 를 쓰지 않는가 — 가이던스가 거기서 죽는다(SmolVLA-S 실모델 확인)
"""

import re
import sys
from pathlib import Path

import numpy as np

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
from rtc_feed import RtcFeed  # noqa: E402


def _chunk(start, n=10, dim=2):
    """k 번째 칸이 값 start+k — 어느 칸이 어디로 갔는지 값으로 읽는다."""
    return np.array([[start + k] * dim for k in range(n)], dtype=np.float32)


# ── 1. 맞물림 ───────────────────────────────────────────────────────────

def test_nothing_to_carry_before_anything_was_sent():
    f = RtcFeed()
    assert f.left_over() is None
    f.replace(_chunk(0), queue_len=10)
    assert f.left_over() is None, "첫 청크는 이어 받을 것이 없다"


def test_left_over_starts_at_the_action_just_sent():
    """메인 루프: 관측(스텝 s) → 스텝 s 몫을 꺼내 보냄 → 그 관측으로 추론. 새 청크 0 번째 = 스텝 s."""
    f = RtcFeed()
    f.replace(_chunk(0), queue_len=10)
    for _ in range(4):          # 0,1,2,3 을 보냈다 — 마지막이 3
        f.pop()
    np.testing.assert_array_equal(f.left_over()[:, 0], [3, 4, 5, 6, 7, 8, 9])


def test_the_raw_ledger_follows_an_aligned_queue():
    """이어 붙이기는 지나간 앞칸을 버린다 — 원본도 같은 칸을 버려야 큐와 나란하다."""
    f = RtcFeed()
    f.replace(_chunk(100), queue_len=7)          # 앞 3칸을 버린 큐
    f.pop()
    np.testing.assert_array_equal(f.left_over()[:, 0], [103, 104, 105, 106, 107, 108, 109])


def test_holding_an_empty_queue_keeps_the_last_sent():
    f = RtcFeed()
    f.replace(_chunk(0, n=2), queue_len=2)
    f.pop(); f.pop(); f.pop()                    # 세 번째는 빈 큐 — 팔은 마지막 값에 서 있다
    np.testing.assert_array_equal(f.left_over()[:, 0], [1])


def test_delay_is_the_worst_recent_and_clears():
    f = RtcFeed(history=3)
    assert f.delay(fallback=2) == 2
    for n in (2, 5, 3):
        f.note_elapsed(n)
    assert f.delay() == 5, "작게 잡으면 고정 안 된 칸을 실행한다 — 최댓값"
    for n in (2, 2, 2):
        f.note_elapsed(n)
    assert f.delay() == 2, "오래된 느린 추론은 잊는다"
    f.note_elapsed(0)
    assert f.delay() == 2 and min(f._elapsed) >= 1
    f.clear()
    assert f.left_over() is None and f.delay(fallback=1) == 1


# ── 2·3. 배선 ───────────────────────────────────────────────────────────

_WRAPPER = (_REPO / "wrapper" / "lerobot_wrapper.py").read_text()


def test_wrapper_passes_the_left_over_and_delay():
    src = code_only(_WRAPPER)
    assert "from rtc_feed import RtcFeed" in src
    assert re.search(r"policy\.predict_action_chunk\(observation, \*\*rtc_kwargs\)", src)
    assert '"prev_chunk_left_over"' in src and '"inference_delay"' in src
    # 관측과 같은 순간의 큐에서 뜬다 — 트리거 튜플에 실려 간다
    assert re.search(r"left = _rtc_feed\.left_over\(\)", src)
    assert "obs_step, obs_epoch, raw_obs, rtc = pending" in src


def test_rtc_does_not_run_under_inference_mode():
    src = code_only(_WRAPPER)
    assert "torch.no_grad() if rtc_kwargs else torch.inference_mode()" in src


def test_rtc_config_is_created_for_capable_policies():
    """체크포인트의 rtc_config 는 None 이다 — 만들지 않으면 슬라이더가 'rtc_config is None' 으로 버려진다."""
    src = code_only(_WRAPPER)
    assert re.search(r'hasattr\(policy, "init_rtc_processor"\)', src)
    assert "policy_cfg.rtc_config = RTCConfig(enabled=False)" in src
    assert "policy.init_rtc_processor()" in src
    # 이어 받을 것이 있을 때만 켠다 — 없으면 예전 경로 그대로
    assert "policy.config.rtc_config.enabled = rtc is not None" in src


def test_the_ledger_moves_with_the_queue():
    src = code_only(_WRAPPER)
    assert re.search(r"_action_queue\.pop\(0\)\s*_rtc_feed\.pop\(\)", src)
    assert "_rtc_feed.replace(raw_chunk, len(_action_queue))" in src
    reset = src[src.index("if _reset_requested:"):src.index("if _paused:")]
    assert "_rtc_feed.clear()" in reset
    start = src.index("if _temporal_ensemble != _te_active:")
    toggle = src[start:src.index("logger.info(\"Temporal ensemble %s\"", start)]
    assert "_rtc_feed.clear()" in toggle


def test_delta_checkpoints_and_ensembles_do_not_get_rtc():
    """델타는 앵커가 관측마다 달라 이전 원본을 새 청크 공간으로 못 옮긴다. 앙상블은 스스로 합친다."""
    src = code_only(_WRAPPER)
    assert "if _rtc_enabled and _rtc_capable and _delta is None and not _te_active:" in src


def test_the_switch_reaches_rtc_policies_only():
    from app.core.inference_params import PARAM_SPEC, foreign_to

    spec = PARAM_SPEC["rtc_enabled"]
    assert spec["default"] is False and spec["realtime"] and spec["send_at_start"] and spec["scoped"]
    for policy in ("smolvla", "smolvla_s", "pi0", "pi05"):
        assert "rtc_enabled" not in foreign_to(policy)
    for policy in ("act", "act_s", "act_dinov3", "diffusion", "pi0_fast"):
        assert "rtc_enabled" in foreign_to(policy)
