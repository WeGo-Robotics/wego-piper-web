"""act_dinov3 계약 (feature/act-dinov3.md).

1. 이름 규약 — 팩토리가 이름에서 유도하는 네 가지가 실제로 풀리는가
2. 백본만 바뀌었나 — 특징맵 격자가 ResNet18 ACT 와 같은 토큰 수인가 (ViT·ConvNeXt 둘 다)
3. 학습이 도는가 — 백본에 기울기가 가고, 고정하면 안 간다
4. 추론이 액션 시그니처를 안 바꾸는가
5. 저장/재로드가 `type: act_dinov3` 로 돌아오고, **체크포인트를 열 때는 가중치를 받지 않는가**

가중치는 받지 않는다 — `timm.create_model` 을 감싸 `pretrained` 요청만 기록하고 빈 모델을 만든다.
"""

import json
import tempfile
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("lerobot")
timm = pytest.importorskip("timm")

from lerobot.configs.policies import PreTrainedConfig  # noqa: E402
from lerobot.configs.types import FeatureType, PolicyFeature  # noqa: E402
from lerobot.policies.factory import get_policy_class, make_pre_post_processors  # noqa: E402

import lerobot_policy_piper  # noqa: E402,F401  — 등록
from lerobot_policy_piper.act_dinov3.configuration_act_dinov3 import ActDinov3Config  # noqa: E402
from lerobot_policy_piper.act_dinov3.modeling_act_dinov3 import (  # noqa: E402
    ActDinov3Policy,
    DinoBackbone,
)

B, CHUNK, STATE, H, W = 2, 8, 7, 64, 96


@pytest.fixture(autouse=True)
def requested(monkeypatch):
    """`pretrained` 요청을 기록하고 언제나 빈 모델을 만든다 — 테스트가 90MB 를 받지 않게."""
    calls: list[bool] = []
    real = timm.create_model

    def fake(name, pretrained=False, **kw):
        calls.append(pretrained)
        return real(name, pretrained=False, **kw)

    monkeypatch.setattr(timm, "create_model", fake)
    return calls


def _features(cfg):
    cfg.input_features = {
        "observation.state": PolicyFeature(FeatureType.STATE, (STATE,)),
        "observation.images.top": PolicyFeature(FeatureType.VISUAL, (3, H, W)),
    }
    cfg.output_features = {"action": PolicyFeature(FeatureType.ACTION, (STATE,))}
    return cfg


def _small(**kw):
    return dict(chunk_size=CHUNK, n_action_steps=CHUNK, dim_model=64, dim_feedforward=128,
                n_encoder_layers=1, n_decoder_layers=1, n_vae_encoder_layers=1, n_heads=2, **kw)


def _policy(**kw):
    torch.manual_seed(0)
    return ActDinov3Policy(_features(ActDinov3Config(**_small(**kw))))


def _batch(seed=0):
    g = torch.Generator().manual_seed(seed)
    return {
        "observation.state": torch.randn(B, STATE, generator=g),
        "observation.images.top": torch.randn(B, 3, H, W, generator=g),
        "action": torch.randn(B, CHUNK, STATE, generator=g),
        "action_is_pad": torch.zeros(B, CHUNK, dtype=torch.bool),
    }


# ── 1. 이름 규약 ──────────────────────────────────────────────────────────

def test_plugin_registers_and_factory_resolves_by_name():
    assert "act_dinov3" in PreTrainedConfig.get_known_choices()
    assert PreTrainedConfig.get_choice_class("act_dinov3") is ActDinov3Config
    assert get_policy_class("act_dinov3") is ActDinov3Policy
    pre, post = make_pre_post_processors(_features(ActDinov3Config(**_small())), dataset_stats=None)
    assert pre is not None and post is not None


def test_naming_convention_is_what_the_factory_derives():
    name = ActDinov3Config.__name__
    assert ActDinov3Policy.__name__ == name.removesuffix("Config") + "Policy"
    assert ActDinov3Config.__module__.replace("configuration_", "modeling_") == ActDinov3Policy.__module__
    import importlib
    proc = importlib.import_module(ActDinov3Config.__module__.replace("configuration_", "processor_"))
    assert callable(getattr(proc, "make_act_dinov3_pre_post_processors"))


def test_config_validates():
    with pytest.raises(ValueError):
        ActDinov3Config(**_small(dino_model="vit_small_patch16_224.augreg_in21k"))
    with pytest.raises(ValueError):
        ActDinov3Config(**_small(dino_feature_stride=0))
    # 버릴 ResNet18 에 ImageNet 가중치를 받지 않는다
    assert ActDinov3Config(**_small()).pretrained_backbone_weights is None


# ── 2. 백본 ─────────────────────────────────────────────────────────────

def test_vit_grid_matches_resnet18_token_count():
    """기본 보폭 32 = ResNet18 ACT 와 같은 격자. 480×640 → 15×20."""
    bb = DinoBackbone("vit_small_patch16_dinov3.lvd1689m", pretrained=False, feature_stride=32).eval()
    with torch.no_grad():
        f = bb(torch.randn(1, 3, 480, 640))["feature_map"]
    assert tuple(f.shape) == (1, 384, 15, 20)


def test_vit_at_native_stride_and_odd_sizes():
    bb = DinoBackbone("vit_small_patch16_dinov3.lvd1689m", pretrained=False, feature_stride=16).eval()
    with torch.no_grad():
        assert tuple(bb(torch.randn(1, 3, 64, 96))["feature_map"].shape) == (1, 384, 4, 6)
        # 보폭으로 안 나눠지는 크기(D405 848×480 를 줄인 것)도 격자로 맞춰 넣는다
        assert tuple(bb(torch.randn(1, 3, 50, 106))["feature_map"].shape)[2:] == (3, 7)


def test_convnext_needs_no_resize_at_stride_32():
    bb = DinoBackbone("convnext_tiny.dinov3_lvd1689m", pretrained=False, feature_stride=32).eval()
    assert bb.reduction == 32 and bb.channels == 768
    with torch.no_grad():
        assert tuple(bb(torch.randn(1, 3, 64, 96))["feature_map"].shape) == (1, 768, 2, 3)


def test_stride_finer_than_the_network_is_refused():
    with pytest.raises(ValueError, match="보폭"):
        DinoBackbone("convnext_tiny.dinov3_lvd1689m", pretrained=False, feature_stride=16)


def test_policy_swaps_backbone_and_projection():
    pol = _policy()
    assert isinstance(pol.model.backbone, DinoBackbone)
    assert pol.model.encoder_img_feat_input_proj.in_channels == 384
    assert pol.model.encoder_img_feat_input_proj.out_channels == 64
    assert not any("layer4" in n for n, _ in pol.named_parameters()), "ResNet 이 남았다"


# ── 3. 학습 ─────────────────────────────────────────────────────────────

def _backbone_grad(pol):
    pol.train()
    loss, d = pol(_batch())
    assert torch.isfinite(loss) and "l1_loss" in d
    loss.backward()
    grads = [p.grad for p in pol.model.backbone.parameters()]
    return sum(g.abs().sum().item() for g in grads if g is not None)


def test_backbone_learns_with_backbone_lr():
    pol = _policy()
    assert _backbone_grad(pol) > 0
    groups = pol.get_optim_params()
    bb = {id(p) for p in pol.model.backbone.parameters()}
    assert {id(p) for p in groups[1]["params"]} == bb
    assert groups[1]["lr"] == pol.config.optimizer_lr_backbone


def test_frozen_backbone_gets_no_gradient():
    pol = _policy(freeze_backbone=True)
    assert _backbone_grad(pol) == 0
    assert pol.model.encoder_img_feat_input_proj.weight.grad is not None    # 투영은 학습한다
    groups = pol.get_optim_params()
    assert groups[1]["params"] == []
    torch.optim.AdamW(groups, lr=1e-4)                                      # 빈 그룹도 받는다


# ── 4. 추론 ─────────────────────────────────────────────────────────────

def test_predict_keeps_action_signature():
    pol = _policy()
    pol.eval()
    obs = {k: v[:1] for k, v in _batch().items() if k.startswith("observation")}
    with torch.no_grad():
        chunk = pol.predict_action_chunk(obs)
    assert tuple(chunk.shape) == (1, CHUNK, STATE)


# ── 5. 저장/재로드 ─────────────────────────────────────────────────────

def test_fresh_policy_asks_for_pretrained_weights(requested):
    _policy()
    assert requested == [True]


def test_roundtrip_keeps_type_and_skips_download(requested):
    pol = _policy(dino_feature_stride=16, freeze_backbone=True)
    with tempfile.TemporaryDirectory() as d:
        pol.save_pretrained(d)
        saved = json.loads((Path(d) / "config.json").read_text())
        assert saved["type"] == "act_dinov3"
        assert saved["dino_model"] == "vit_small_patch16_dinov3.lvd1689m"
        assert saved["dino_feature_stride"] == 16 and saved["freeze_backbone"] is True
        requested.clear()
        back = ActDinov3Policy.from_pretrained(d)
    assert requested == [False], "체크포인트를 열면서 사전학습 가중치를 받으려 했다"
    assert isinstance(back, ActDinov3Policy)
    for (n, a), (_, b) in zip(pol.model.backbone.state_dict().items(),
                              back.model.backbone.state_dict().items()):
        assert torch.equal(a.cpu(), b.cpu()), n
    # from_pretrained 밖에서는 다시 사전학습 가중치를 받는다
    requested.clear()
    _policy()
    assert requested == [True]
