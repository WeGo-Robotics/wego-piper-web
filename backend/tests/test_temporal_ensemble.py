"""시간 앙상블 — wrapper/temporal_ensemble.py 와 lerobot_wrapper 배선.

예전 슬라이더는 `temporal_ensemble_coeff` 를 `policy.config` 에 쓰기만 했다. LeRobot 의 앙상블은
`select_action` 안에서만 도는데 wrapper 는 `predict_action_chunk` 를 직접 부르므로, 처음 커밋부터
**아무 효과가 없었다.** 여기 테스트는 둘을 잡는다:

1. 평균이 ACT 의 정의와 같은가 — 동기·지연 0 이면 LeRobot `ACTTemporalEnsembler` 와 수치가 같다
2. wrapper 가 정말 그걸 쓰는가 — 다시 "설정만 하고 안 쓰는" 상태로 돌아가지 않게
"""

import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
from temporal_ensemble import TemporalEnsembler  # noqa: E402

C, D = 6, 3          # 청크 길이, 액션 차원


def _chunks(n, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(size=(n, C, D))


# ── 1. 평균 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("m", [0.0, 0.01, 0.5, -0.3])
def test_matches_lerobot_when_synchronous(m):
    """매 스텝 추론·지연 0 — LeRobot 의 select_action 경로와 같은 숫자가 나와야 한다."""
    torch = pytest.importorskip("torch")
    act = pytest.importorskip("lerobot.policies.act.modeling_act")

    ref = act.ACTTemporalEnsembler(m, C)
    ours = TemporalEnsembler(m)
    for t, chunk in enumerate(_chunks(4 * C)):
        want = ref.update(torch.tensor(chunk[None], dtype=torch.float64))[0].numpy()
        ours.add(t, chunk)
        got = ours.pop(t)
        assert np.allclose(got, want, atol=1e-9), (t, got, want)


def test_lagged_chunks_average_only_what_arrived():
    """스텝 s 의 관측으로 낸 청크가 L 스텝 뒤에 도착한다 — 그 사이 칸은 이미 보냈으니 버리고,
    남은 칸은 도착한 청크들로만 평균한다."""
    m, L = 0.1, 2
    chunks = _chunks(5 * C, seed=1)
    ens = TemporalEnsembler(m)
    pending: dict[int, int] = {}                       # 도착할 스텝 → 관측 스텝
    for t in range(len(chunks)):
        for s in [s for s, at in pending.items() if at == t]:
            ens.add(s, chunks[s])
            del pending[s]
        got = ens.pop(t)
        pending[t] = t + L                             # 이번 관측으로 추론 시작
        srcs = [s for s in range(t - L, t - C, -1) if s >= 0]
        if not srcs:
            assert got is None, t
            continue
        w = np.array([math.exp(m * (t - s)) for s in srcs])
        want = (w[:, None] * np.array([chunks[s][t - s] for s in srcs])).sum(0) / w.sum()
        assert np.allclose(got, want), t


def test_older_predictions_weigh_more_with_a_positive_coefficient():
    """m>0 이면 먼저 나온(멀리 내다본) 예측 쪽으로 기운다 — 원 논문의 선택."""
    ens = TemporalEnsembler(1.0)
    ens.add(0, [np.zeros(1), np.zeros(1)])            # 스텝 1 을 k=1 로 (오래된 예측)
    ens.add(1, [np.ones(1)])                          # 스텝 1 을 k=0 으로 (새 예측)
    ens.pop(0)
    assert ens.pop(1)[0] < 0.5


def test_nothing_covers_the_step():
    ens = TemporalEnsembler()
    assert ens.pop(0) is None                         # 첫 청크가 오기 전
    ens.add(1, np.ones((2, D)))
    assert ens.pop(1) is not None and ens.pop(2) is not None
    assert ens.pop(3) is None                         # 청크를 다 썼다 — 부르는 쪽이 직전 액션 유지
    assert len(ens) == 0


def test_late_chunks_cannot_refill_steps_already_sent():
    ens = TemporalEnsembler()
    ens.pop(0)
    ens.pop(1)
    assert ens.add(0, np.ones((4, D))) == 2           # 스텝 0·1 은 버리고 2·3 만
    assert len(ens) == 2


def test_clear_drops_chunks_from_observations_taken_before():
    """일시정지·모드 전환 전에 찍은 관측의 청크가 비운 뒤에 도착하면 버린다."""
    ens = TemporalEnsembler()
    old = ens.epoch
    ens.clear(5)
    assert ens.add(5, np.ones((3, D)), epoch=old) == 0
    assert ens.add(5, np.ones((3, D)), epoch=ens.epoch) == 3


def test_huge_coefficients_do_not_overflow():
    ens = TemporalEnsembler(1000.0)
    ens.add(0, np.ones((C, D)))
    ens.add(1, 2 * np.ones((C, D)))
    ens.pop(0)
    out = ens.pop(1)
    assert np.all(np.isfinite(out))


# ── 2. wrapper 배선 ────────────────────────────────────────────────────

_WRAPPER = (_REPO / "wrapper" / "lerobot_wrapper.py").read_text()


def test_the_coefficient_no_longer_just_sits_on_the_config():
    """`policy.config.temporal_ensemble_coeff` 는 wrapper 경로에서 아무도 안 읽는다."""
    assert 'setattr(p.config, "temporal_ensemble_coeff"' not in code_only(_WRAPPER)


def test_wrapper_feeds_the_ensembler_and_sends_its_average():
    src = code_only(_WRAPPER)
    assert "from temporal_ensemble import TemporalEnsembler" in src
    assert re.search(r"_ensembler\.add\(\s*obs_step", src), "추론 결과를 앙상블에 안 넣는다"
    assert "_ensembler.pop(step)" in src, "보낼 액션을 앙상블에서 안 꺼낸다"


def test_ensembling_infers_every_step():
    """앙상블은 매 스텝의 청크가 있어야 평균이 된다 — 큐 잔량 임계치로 막으면 안 된다."""
    src = code_only(_WRAPPER)
    assert re.search(r"if \(_te_active or qsize_after <= _refill_lead\)", src)


def test_ensembling_is_on_by_default_for_the_act_family_only():
    """기본은 켬(2026-10-29 실기). 하지만 wrapper 는 모든 정책을 돌리므로 제 기본값은 꺼짐이다 —
    화면 밖(플러그인·외부 API)에서 띄운 ACT 추론에는 시작 라우터가 스펙 기본값을 채운다."""
    assert re.search(r"^_temporal_ensemble = False", _WRAPPER, re.M)
    from app.core.inference_params import PARAM_SPEC

    assert PARAM_SPEC["temporal_ensemble"]["default"] is True
    assert PARAM_SPEC["temporal_ensemble"]["send_at_start"] and PARAM_SPEC["temporal_ensemble"]["realtime"]
    for policy in ("act", "act_s", "act_dinov3"):
        assert _overrides(policy, {"max_velocity": 100})["temporal_ensemble"] is True
        assert _overrides(policy, {"temporal_ensemble": False})["temporal_ensemble"] is False, "고른 값을 덮었다"
    for policy in ("smolvla", "smolvla_s", "pi0"):
        assert "temporal_ensemble" not in _overrides(policy, {"max_velocity": 100})
    assert "temporal_ensemble" not in _overrides("act", {"max_velocity": 100}, mode="server"), \
        "앙상블은 로컬 wrapper 의 것이다"


def test_the_coefficient_is_capped_where_it_stopped_helping():
    from app.core.inference_params import PARAM_SPEC
    from temporal_ensemble import MAX_COEFF, TemporalEnsembler

    assert PARAM_SPEC["temporal_ensemble_coeff"]["max"] == MAX_COEFF == 0.05
    e = TemporalEnsembler()
    e.set_coeff(0.764)
    assert e.coeff == MAX_COEFF, "프리셋·외부 API 의 큰 값이 그대로 들어간다"
    e.set_coeff(-1)
    assert e.coeff == 0.0
    src = code_only(_WRAPPER)
    assert src.count("_ensembler.set_coeff(value)") == 2 and "_ensembler.coeff =" not in src


def test_the_screen_resets_a_saved_old_default_once():
    """화면은 파라미터를 통째로 저장한다 — 예전 기본값(꺼짐)이 고른 값처럼 남아 새 기본값이 안 먹는다."""
    src = (_REPO / "frontend" / "src" / "pages" / "InferencePage.tsx").read_text()
    assert "temporal_ensemble: true," in src
    assert re.search(r"piper_inference_params_rev.*\n\s*delete saved\.temporal_ensemble", src)


def test_reset_and_pause_clear_the_ensemble():
    src = code_only(_WRAPPER)
    reset = src[src.index("if _reset_requested:"):src.index("if _paused:")]
    assert "_ensembler.clear(0)" in reset
    paused = src[src.index("if _paused:"):src.index("if _temporal_ensemble != _te_active:")]
    assert "_ensembler.clear(step)" in paused


# ── 3. 정책별 파라미터는 그 정책에만 ──────────────────────────────────

def _overrides(policy_type, params, mode="local"):
    import json

    from app.routers.models import InferencePreviewRequest, _build_args_for

    req = InferencePreviewRequest(checkpoint_path="/models/x/pretrained_model", inference_mode=mode,
                                  robot_port="can0", policy_type=policy_type, params=params)
    args = _build_args_for(req, "piper_follower", "can0")
    return json.loads(args[args.index("--config-overrides") + 1]) if "--config-overrides" in args else {}


def test_an_ensemble_switch_left_on_for_act_does_not_follow_into_smolvla():
    """화면은 localStorage 의 파라미터를 정책과 무관하게 통째로 싣는다. ACT 에서 켠 앙상블이
    SmolVLA 에 실리면 스위치가 안 보이는 화면에서 매 스텝 추론이 돈다."""
    stale = {"temporal_ensemble": True, "temporal_ensemble_coeff": 0.05, "max_velocity": 100}
    for policy in ("act", "act_s", "act_dinov3"):
        o = _overrides(policy, stale)
        assert o["temporal_ensemble"] is True and o["temporal_ensemble_coeff"] == 0.05
    for policy in ("smolvla", "smolvla_s"):
        o = _overrides(policy, stale)
        assert "temporal_ensemble" not in o and "temporal_ensemble_coeff" not in o
        assert o["max_velocity"] == 100, "공용 파라미터까지 지웠다"


def test_rtc_params_stay_with_rtc_policies():
    o = _overrides("act", {"max_guidance_weight": 5.0, "execution_horizon": 8})
    assert "max_guidance_weight" not in o and "execution_horizon" not in o
    o = _overrides("smolvla", {"max_guidance_weight": 5.0, "execution_horizon": 8})
    assert o["max_guidance_weight"] == 5.0


def test_runtime_updates_drop_params_the_running_policy_does_not_use(monkeypatch):
    """실행 중에도 같다 — 프리셋·외부 API 는 카드가 감춰져도 그대로 보낸다."""
    import asyncio

    from app.routers import params as R
    from app.services import inference_state

    sent = []

    async def fake_send(p):
        sent.append(p)

    monkeypatch.setattr(R.param_bridge, "send_params", fake_send)
    # get() 은 추론 프로세스가 살아 있을 때만 값을 준다 — 여기선 기록만 흉내 낸다
    monkeypatch.setattr(inference_state, "get", lambda: inference_state.RunningPolicy("smolvla", "/x"))
    r = asyncio.run(R.update_params(R.ParamUpdate(params={"temporal_ensemble": True, "max_velocity": 90})))
    assert sent == [{"max_velocity": 90}]
    assert any("temporal_ensemble" in w for w in r["warnings"])


def test_the_switch_reaches_the_act_family_only():
    from app.core.inference_params import spec_for_frontend

    rows = {r["key"]: r for r in spec_for_frontend()}
    assert rows["temporal_ensemble"]["policies"] == ["act", "act_dinov3", "act_s"]
    assert rows["temporal_ensemble"]["kind"] == "bool"
