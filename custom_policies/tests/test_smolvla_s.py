"""smolvla_s 계약 (feature/smolvla-s.md).

1. 이름 규약 — 팩토리가 이름에서 유도하는 것들이 풀리는가
2. λ=0 이면 바닐라 SmolVLA 와 손실이 **수치 동일**한가
3. 배치에 `subtask` 가 없으면 죽는가
4. 학습 때 본 특징과 추론 때 본 특징이 **같은가** — 다르면 헤드가 학습과 다른 입력을 받는다
5. 추론이 액션 시그니처를 안 바꾸고 `last_aux` 를 채우는가
6. 저장/재로드, 전처리가 `subtask` 를 살려 두는가

VLM 구조는 진짜 SmolVLM2 config 로 만든다(가중치는 무작위, 층은 2개). HF 캐시에 그 config 가
없으면 건너뛴다 — 테스트가 네트워크를 타지 않는다.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
torch = pytest.importorskip("torch")
pytest.importorskip("lerobot")
pytest.importorskip("transformers")

from huggingface_hub import try_to_load_from_cache  # noqa: E402

from lerobot.configs.policies import PreTrainedConfig  # noqa: E402
from lerobot.configs.types import FeatureType, PolicyFeature  # noqa: E402
from lerobot.policies.factory import get_policy_class, make_pre_post_processors  # noqa: E402
from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig  # noqa: E402

import lerobot_policy_piper  # noqa: E402,F401  — 등록
from lerobot_policy_piper.smolvla_s.configuration_smolvla_s import SmolVLASConfig  # noqa: E402
from lerobot_policy_piper.stage_config import DEFAULT_STAGE_NAMES  # noqa: E402

VLM = SmolVLAConfig().vlm_model_name
if not isinstance(try_to_load_from_cache(VLM, "config.json"), str):
    pytest.skip(f"{VLM} config 가 HF 캐시에 없다", allow_module_level=True)

from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy  # noqa: E402

from lerobot_policy_piper.smolvla_s.modeling_smolvla_s import SmolVLASPolicy  # noqa: E402

B, CHUNK, STATE, H, W = 2, 8, 7, 48, 64
N = len(DEFAULT_STAGE_NAMES)


def _cfg(cls, **kw):
    cfg = cls(load_vlm_weights=False, num_vlm_layers=2, chunk_size=CHUNK, n_action_steps=CHUNK,
              num_steps=2, resize_imgs_with_padding=(64, 64), device="cpu", **kw)
    cfg.input_features = {
        "observation.state": PolicyFeature(FeatureType.STATE, (STATE,)),
        "observation.images.top": PolicyFeature(FeatureType.VISUAL, (3, H, W)),
    }
    cfg.output_features = {"action": PolicyFeature(FeatureType.ACTION, (STATE,))}
    return cfg


def _batch(seed=0):
    g = torch.Generator().manual_seed(seed)
    L = SmolVLAConfig().tokenizer_max_length
    lang_mask = torch.zeros(B, L, dtype=torch.bool)
    lang_mask[0, :5] = True
    lang_mask[1, :9] = True                      # 샘플마다 패딩 길이가 다르다
    return {
        "observation.state": torch.randn(B, STATE, generator=g),
        "observation.images.top": torch.rand(B, 3, H, W, generator=g),
        "observation.language.tokens": torch.randint(0, 100, (B, L), generator=g),
        "observation.language.attention_mask": lang_mask,
        "action": torch.randn(B, CHUNK, STATE, generator=g),
        "subtask": ["GRASP", "RELEASE"],
    }


def _obs(batch, i=0):
    return {k: v[i:i + 1] for k, v in batch.items() if k.startswith("observation")}


@pytest.fixture(scope="module")
def pair():
    """바닐라와 SmolVLA-S — SmolVLA 부분 가중치가 같다. 만들기가 느려서(수 초) 모듈에 하나."""
    torch.manual_seed(0)
    vanilla = SmolVLAPolicy(_cfg(SmolVLAConfig))
    pol = SmolVLASPolicy(_cfg(SmolVLASConfig))
    pol.model.load_state_dict(vanilla.model.state_dict())
    return vanilla, pol


# ── 1. 이름 규약 ──────────────────────────────────────────────────────────

def test_plugin_registers_and_factory_resolves_by_name():
    assert "smolvla_s" in PreTrainedConfig.get_known_choices()
    assert PreTrainedConfig.get_choice_class("smolvla_s") is SmolVLASConfig
    assert get_policy_class("smolvla_s") is SmolVLASPolicy


def test_naming_convention_is_what_the_factory_derives():
    import importlib

    name = SmolVLASConfig.__name__
    assert SmolVLASPolicy.__name__ == name.removesuffix("Config") + "Policy"
    assert SmolVLASConfig.__module__.replace("configuration_", "modeling_") == SmolVLASPolicy.__module__
    proc = importlib.import_module(SmolVLASConfig.__module__.replace("configuration_", "processor_"))
    assert callable(getattr(proc, "make_smolvla_s_pre_post_processors"))


def test_config_defaults_and_validation():
    cfg = SmolVLASConfig()
    assert cfg.n_stages == N and cfg.pool == "state"      # ACT-S 와 다르다 — smolvla-s.md §2
    with pytest.raises(ValueError):
        SmolVLASConfig(pool="cls")
    with pytest.raises(ValueError):
        SmolVLASConfig(stage_names=["a", "a"])


# ── 2. λ=0 ⇒ 바닐라와 동일 ───────────────────────────────────────────────

def _loss(policy, batch, **kw):
    """flow matching 의 노이즈·시간을 고정한다 — 그래야 두 정책을 숫자로 비교할 수 있다."""
    noise = torch.randn(B, CHUNK, policy.config.max_action_dim, generator=torch.Generator().manual_seed(1))
    time = torch.full((B,), 0.5)
    policy.eval()                                  # 드롭아웃을 끈다 — 비교 대상은 배선이다
    with torch.no_grad():
        return policy.forward(batch, noise=noise, time=time, **kw)


def test_zero_weight_matches_vanilla_loss_exactly(pair):
    vanilla, pol = pair
    pol.config.stage_loss_weight = 0.0
    try:
        l0, _ = _loss(vanilla, _batch())
        l1, d1 = _loss(pol, _batch())
    finally:
        pol.config.stage_loss_weight = 0.1
    assert torch.equal(l0, l1), (l0, l1)
    assert "stage_ce" in d1 and "stage_acc" in d1   # 계산은 하되 손실엔 안 더한다


def test_positive_weight_adds_ce(pair):
    vanilla, pol = pair
    l0, _ = _loss(vanilla, _batch())
    l1, d1 = _loss(pol, _batch())
    assert l1.item() == pytest.approx(l0.item() + 0.1 * d1["stage_ce"], rel=1e-5)
    assert d1["loss"] == pytest.approx(l1.item())  # 로그의 loss 도 합계다


def test_per_sample_reduction_keeps_its_shape(pair):
    """RA-BC 가 `reduction="none"` 으로 부른다 — 샘플별 (B,) 모양이 유지돼야 한다."""
    _, pol = pair
    loss, _ = _loss(pol, _batch(), reduction="none")
    assert loss.shape == (B,)


def test_gradient_reaches_the_head_and_state_projection_only(pair):
    """VLM 이 얼어 있으면(train_expert_only) CE 는 헤드와 state_proj 로만 흐른다 (smolvla-s.md §3)."""
    _, pol = pair
    pol.train()
    pol.zero_grad()
    loss, _ = pol.forward(_batch())
    loss.backward()
    assert pol.stage_head[-1].weight.grad.abs().sum() > 0
    assert pol.model.state_proj.weight.grad.abs().sum() > 0
    frozen = [p for p in pol.model.vlm_with_expert.vlm.parameters() if not p.requires_grad]
    assert frozen and all(p.grad is None for p in frozen)
    pol.zero_grad()


# ── 3. 라벨 없으면 죽는다 ────────────────────────────────────────────────

def test_missing_subtask_raises(pair):
    _, pol = pair
    b = _batch()
    del b["subtask"]
    with pytest.raises(KeyError, match="subtask"):
        pol.forward(b)
    pol.model.vlm_with_expert._stage_prefix = None


# ── 4. 학습 특징 == 추론 특징 ────────────────────────────────────────────

@pytest.mark.parametrize("pool", ["state", "mean"])
def test_training_and_inference_see_the_same_features(pair, pool):
    """학습은 prefix+suffix 를 한 번에, 추론은 prefix 만 돌린다. **유효 토큰**의 출력은 같고
    언어 패딩 토큰만 다르다(마스크가 전부 막힌 행의 softmax 가 보는 열 수가 다르다).
    그래서 풀링이 패딩을 빼야 한다 — 빼지 않으면 헤드가 학습과 다른 입력을 받는다."""
    _, pol = pair
    pol.config.pool = pool
    seen = []
    orig = pol._pooled
    pol._pooled = lambda: seen.append(orig()) or seen[-1]
    try:
        _loss(pol, _batch())
        with torch.no_grad():
            # 같은 배치 크기로 — SigLIP 의 행렬곱이 배치 크기에 따라 1e-5 쯤 갈리고 무작위 층이 키운다
            pol.predict_action_chunk({k: v for k, v in _batch().items() if k.startswith("observation")})
    finally:
        del pol._pooled
        pol.config.pool = "state"
    train, infer = seen
    assert torch.allclose(train, infer, atol=1e-6), (train - infer).abs().max()


# ── 5. 추론 ─────────────────────────────────────────────────────────────

def test_predict_keeps_action_signature_and_fills_last_aux(pair):
    _, pol = pair
    pol.config.mc_samples = 4
    try:
        with torch.no_grad():
            chunk = pol.predict_action_chunk(_obs(_batch()))
    finally:
        pol.config.mc_samples = 0
    assert tuple(chunk.shape) == (1, CHUNK, STATE)
    a = pol.last_aux
    assert set(a) == {"stage", "stage_p", "margin", "entropy", "probs", "mc_std"}
    assert 0 <= a["stage"] < N and len(a["probs"]) == N
    assert sum(a["probs"]) == pytest.approx(1.0, abs=1e-5)
    assert a["mc_std"] is not None
    # 한 번 읽은 prefix 는 비운다 — 다음 호출이 낡은 것을 못 쓴다
    assert pol.model.vlm_with_expert._stage_prefix is None
    with pytest.raises(RuntimeError):
        pol._pooled()


def test_select_action_refreshes_stage_only_with_a_new_chunk(pair):
    _, pol = pair
    pol.reset()
    pol.last_aux = None
    with torch.no_grad():
        pol.select_action(_obs(_batch()))
    first = pol.last_aux
    assert first is not None
    with torch.no_grad():
        pol.select_action(_obs(_batch()))           # 큐에서 꺼낼 뿐 — prefix 가 안 돈다
    assert pol.last_aux is first
    pol.reset()


# ── 6. 저장/재로드 · 전처리 ──────────────────────────────────────────────

def test_roundtrip_keeps_type_and_head(pair):
    _, pol = pair
    with tempfile.TemporaryDirectory() as d:
        pol.save_pretrained(d)
        assert json.loads((Path(d) / "config.json").read_text())["type"] == "smolvla_s"
        back = SmolVLASPolicy.from_pretrained(d)
    assert isinstance(back, SmolVLASPolicy)
    assert torch.equal(back.stage_head[-1].weight.cpu(), pol.stage_head[-1].weight.cpu())
    with torch.no_grad():                           # 감싼 forward 가 재로드한 쪽에도 붙어 있다
        back.predict_action_chunk(_obs(_batch()))
    assert back.last_aux is not None


def test_preprocessing_keeps_the_subtask_label():
    """`subtask` 가 전처리를 지나 배치에 남아야 CE 타깃이 된다 (act-s.md §4.1)."""
    pytest.importorskip("lerobot.policies.smolvla.processor_smolvla")
    z = {"mean": torch.zeros(STATE), "std": torch.ones(STATE),
         "min": -torch.ones(STATE), "max": torch.ones(STATE)}
    pre, _ = make_pre_post_processors(_cfg(SmolVLASConfig),
                                      dataset_stats={"observation.state": z, "action": z})
    out = pre({"observation.state": torch.randn(B, STATE), "observation.images.top": torch.rand(B, 3, H, W),
               "action": torch.randn(B, CHUNK, STATE), "task": ["pick", "pick"],
               "subtask": ["GRASP", "_unlabeled"]})
    assert out["subtask"] == ["GRASP", "_unlabeled"]
