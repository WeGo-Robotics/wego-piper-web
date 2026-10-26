"""act_s 계약 (feature/act-s.md).

1. 이름 규약 — 팩토리가 이름에서 유도하는 네 가지가 실제로 풀리는가
2. λ=0 이면 바닐라 ACT 와 손실이 **수치 동일**한가 (배선 검증 = §8 런 B)
3. 배치에 `subtask` 가 없으면 조용히 넘어가지 않고 죽는가
4. 추론이 액션 시그니처를 안 바꾸고 `last_aux` 를 채우는가
5. 저장/재로드가 `type: act_s` 로 돌아오는가
6. 이름을 바꾸기 전(`act_aux`)의 체크포인트가 그대로 열리는가
"""

import json
import tempfile
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("lerobot")

from lerobot.configs.policies import PreTrainedConfig  # noqa: E402
from lerobot.configs.types import FeatureType, PolicyFeature  # noqa: E402
from lerobot.policies.act.configuration_act import ACTConfig  # noqa: E402
from lerobot.policies.act.modeling_act import ACTPolicy  # noqa: E402
from lerobot.policies.factory import get_policy_class, make_pre_post_processors  # noqa: E402

import lerobot_policy_piper  # noqa: E402,F401  — 등록
from lerobot_policy_piper.act_s.configuration_act_s import ActSConfig  # noqa: E402
from lerobot_policy_piper.act_s.modeling_act_s import ActSPolicy  # noqa: E402
from lerobot_policy_piper.stage_config import DEFAULT_STAGE_NAMES  # noqa: E402

B, CHUNK, STATE, H, W = 2, 8, 7, 64, 80
N = len(DEFAULT_STAGE_NAMES)


def _features(cfg):
    cfg.input_features = {
        "observation.state": PolicyFeature(FeatureType.STATE, (STATE,)),
        "observation.images.top": PolicyFeature(FeatureType.VISUAL, (3, H, W)),
    }
    cfg.output_features = {"action": PolicyFeature(FeatureType.ACTION, (STATE,))}
    cfg.pretrained_backbone_weights = None     # 테스트에서 ImageNet 가중치를 받지 않는다
    return cfg


def _small(**kw):
    return dict(chunk_size=CHUNK, n_action_steps=CHUNK, dim_model=64, dim_feedforward=128,
                n_encoder_layers=1, n_decoder_layers=1, n_vae_encoder_layers=1, n_heads=2, **kw)


def _batch(seed=0):
    g = torch.Generator().manual_seed(seed)
    return {
        "observation.state": torch.randn(B, STATE, generator=g),
        "observation.images.top": torch.rand(B, 3, H, W, generator=g),
        "action": torch.randn(B, CHUNK, STATE, generator=g),
        "action_is_pad": torch.zeros(B, CHUNK, dtype=torch.bool),
        "subtask": ["GRASP", "RELEASE"],          # 로더가 넣어주는 프레임별 이름 (3, 5)
    }


# ── 1. 이름 규약 ──────────────────────────────────────────────────────────

def test_plugin_registers_and_factory_resolves_by_name():
    assert "act_s" in PreTrainedConfig.get_known_choices()
    assert PreTrainedConfig.get_choice_class("act_s") is ActSConfig
    assert get_policy_class("act_s") is ActSPolicy
    cfg = _features(ActSConfig(**_small()))
    pre, post = make_pre_post_processors(cfg, dataset_stats=None)
    assert pre is not None and post is not None


def test_naming_convention_is_what_the_factory_derives():
    """팩토리 규칙을 그대로 재현 — 상류가 규칙을 바꾸면 여기가 먼저 깨진다."""
    name = ActSConfig.__name__
    assert name.endswith("Config")
    assert ActSPolicy.__name__ == name.removesuffix("Config") + "Policy"
    assert ActSConfig.__module__.replace("configuration_", "modeling_") == ActSPolicy.__module__
    import importlib
    proc = importlib.import_module(ActSConfig.__module__.replace("configuration_", "processor_"))
    assert callable(getattr(proc, "make_act_s_pre_post_processors"))


def test_config_validates():
    with pytest.raises(ValueError):
        ActSConfig(**_small(pool="cls"))
    with pytest.raises(ValueError):
        ActSConfig(**_small(stage_names=["a", "b", "c"], stage_class_weights=[1.0, 2.0]))
    with pytest.raises(ValueError):
        ActSConfig(**_small(stage_names=["a", "a"]))
    assert ActSConfig(**_small()).n_stages == N
    with pytest.raises(ValueError):
        ActSConfig(**_small(temperature=0))


# ── 2. λ=0 ⇒ 바닐라와 동일 ───────────────────────────────────────────────

def _vanilla_and_stage(**stage_kw):
    torch.manual_seed(0)
    vanilla = ACTPolicy(_features(ACTConfig(**_small())))
    torch.manual_seed(0)
    pol = ActSPolicy(_features(ActSConfig(**_small(**stage_kw))))
    # ACT 부분 가중치를 동일하게 — 초기화 순서가 달라질 수 있으므로 복사한다
    pol.model.load_state_dict(vanilla.model.state_dict())
    return vanilla, pol


def _same_rng_forward(policy, batch):
    """ACT 의 손실 forward 는 train 모드여야 한다 (eval 이면 VAE 가 안 돌아 mu=None).
    dropout 난수를 같은 시드로 맞춘다 — ACT-S 는 ACT forward 를 **먼저** 돌리므로 그 구간의
    난수 소비가 바닐라와 같다."""
    policy.train()
    torch.manual_seed(123)
    with torch.no_grad():
        return policy(batch)


def test_zero_weight_matches_vanilla_loss_exactly():
    vanilla, pol = _vanilla_and_stage(stage_loss_weight=0.0)
    b = _batch()
    l0, d0 = _same_rng_forward(vanilla, b)
    l1, d1 = _same_rng_forward(pol, b)
    assert torch.allclose(l0, l1), (l0, l1)
    assert d0["l1_loss"] == pytest.approx(d1["l1_loss"])
    assert "stage_ce" in d1 and "stage_acc" in d1   # 계산은 하되 손실엔 안 더한다


def test_positive_weight_adds_ce():
    vanilla, pol = _vanilla_and_stage(stage_loss_weight=0.5, label_smoothing=0.0)
    b = _batch()
    l0, _ = _same_rng_forward(vanilla, b)
    l1, d1 = _same_rng_forward(pol, b)
    assert l1.item() == pytest.approx(l0.item() + 0.5 * d1["stage_ce"], rel=1e-5)


def test_stage_head_gets_gradient():
    _, pol = _vanilla_and_stage(stage_loss_weight=0.1)
    pol.train()
    loss, _ = pol(_batch())
    loss.backward()
    g = pol.stage_head[-1].weight.grad
    assert g is not None and g.abs().sum() > 0


# ── 3. 라벨 없으면 죽는다 ────────────────────────────────────────────────

def test_missing_subtask_raises():
    _, pol = _vanilla_and_stage()
    b = _batch(); del b["subtask"]
    with pytest.raises(KeyError, match="subtask"):
        pol(b)


def test_all_ignored_labels_do_not_nan():
    _, pol = _vanilla_and_stage(stage_loss_weight=0.1)
    b = _batch(); b["subtask"] = ["_unlabeled", "no-such-stage"]   # 모르는 이름 = 무시
    loss, d = pol(b)
    assert torch.isfinite(loss) and d["stage_ce"] == 0.0


def test_balance_counts_labels_and_weights_after_warmup():
    _, pol = _vanilla_and_stage(stage_balance_warmup=2)
    pol.train()
    assert pol._class_weight() is None           # 워밍업 전
    for _ in range(2):
        pol(_batch())
    assert pol.stage_counts[3] == 2 and pol.stage_counts[5] == 2
    w = pol._class_weight()
    assert w is not None and w.shape == (N,) and w.mean().item() == pytest.approx(1.0)
    # 한 번도 안 본 클래스가 본 클래스보다 무겁다
    assert w[0] > w[3]


# ── 4. 추론 ─────────────────────────────────────────────────────────────

def test_predict_keeps_action_signature_and_fills_last_aux():
    _, pol = _vanilla_and_stage(mc_samples=4)
    pol.eval()
    obs = {k: v[:1] for k, v in _batch().items() if k.startswith("observation")}
    with torch.no_grad():
        chunk = pol.predict_action_chunk(obs)
    assert tuple(chunk.shape) == (1, CHUNK, STATE)
    a = pol.last_aux
    assert set(a) == {"stage", "stage_p", "margin", "entropy", "probs", "mc_std"}
    assert 0 <= a["stage"] < N and len(a["probs"]) == N
    assert 0 < a["stage_p"] <= 1 and 0 <= a["margin"] <= a["stage_p"] and a["entropy"] >= 0
    assert a["mc_std"] is not None and a["mc_std"] >= 0
    assert not pol.stage_head.training            # MC 가 eval 상태를 되돌렸다
    assert sum(a["probs"]) == pytest.approx(1.0, abs=1e-5)


def test_pooled_outside_forward_is_refused():
    pol = ActSPolicy(_features(ActSConfig(**_small())))
    with pytest.raises(RuntimeError):
        pol._pooled()


# ── 5. 저장/재로드 ─────────────────────────────────────────────────────

def test_roundtrip_keeps_type_and_head():
    _, pol = _vanilla_and_stage()
    with tempfile.TemporaryDirectory() as d:
        pol.save_pretrained(d)
        assert json.loads((Path(d) / "config.json").read_text())["type"] == "act_s"
        back = ActSPolicy.from_pretrained(d)
    assert isinstance(back, ActSPolicy)
    assert torch.equal(back.stage_head[-1].weight.cpu(), pol.stage_head[-1].weight.cpu())


# ── 6. 옛 이름 ─────────────────────────────────────────────────────────

def test_checkpoints_saved_as_act_aux_still_open():
    """이름을 바꾸기 전(ACT-Aux)에 학습한 체크포인트는 config.json 이 `"type": "act_aux"` 다.
    그대로 열려야 하고, 다시 저장하면 새 이름이 된다."""
    assert PreTrainedConfig.get_choice_class("act_aux") is ActSConfig
    assert get_policy_class("act_aux") is ActSPolicy
    assert ActSConfig(**_small()).type == "act_s"     # 등록 순서 — 옛 이름이 이기면 안 된다
    _, pol = _vanilla_and_stage()
    with tempfile.TemporaryDirectory() as d:
        pol.save_pretrained(d)
        cfg = Path(d) / "config.json"
        cfg.write_text(cfg.read_text().replace('"type": "act_s"', '"type": "act_aux"'))
        assert json.loads(cfg.read_text())["type"] == "act_aux"
        back = ActSPolicy.from_pretrained(d)
        assert torch.equal(back.stage_head[-1].weight.cpu(), pol.stage_head[-1].weight.cpu())
        back.save_pretrained(d)
        assert json.loads(cfg.read_text())["type"] == "act_s"
