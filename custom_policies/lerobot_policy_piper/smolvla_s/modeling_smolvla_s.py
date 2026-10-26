"""`SmolVLASPolicy` — SmolVLA 그대로 + VLM prefix 출력에 붙은 stage 헤드 (feature/smolvla-s.md).

## 어디서 특징을 꺼내나 — VLM prefix 출력

SmolVLA 는 VLM 이 `[이미지 토큰…, 언어 토큰…, state 토큰]`(prefix)을 한 번 돌리고, 행동 전문가가
그 KV 를 보며 flow matching 으로 청크를 낸다. prefix 출력은 **관측만의 함수**다 — 노이즈 액션·
타임스텝이 안 섞인다. 그리고 공짜다:

- 학습 손실 forward: prefix + suffix 를 한 번에 돌리며 `prefix_out` 을 함께 돌려준다
- 추론: 디노이즈 루프 전에 prefix 로 KV 캐시를 채우는 호출이 `prefix_out` 을 돌려준다

블록 마스크 덕에 학습 때의 `prefix_out` 과 추론 때의 그것은 같은 계산이다 (suffix 는 prefix 를
못 바꾼다). 디노이즈 스텝 호출(`[None, suffix]`)은 prefix 가 None 이라 건너뛴다.

## hook 이 아니라 forward 감싸기

ACT-S 는 `register_forward_hook` 이지만 여기선 안 된다 — `VLAFlowMatching` 이
`self.vlm_with_expert.forward(...)` 를 **직접** 불러서 `__call__` 을 안 거치고, 그러면 hook 이
안 돈다. 그래서 그 인스턴스의 `forward` 하나만 감싼다 (클래스는 그대로). SmolVLA 코드는 복사하지
않는다.

## 패딩 마스크

언어 토큰은 `tokenizer_max_length` 까지 패딩된다. 패딩은 평균에서 빼야 한다. prefix 의 패딩
마스크는 VLM 에 넘어가는 2D 어텐션 마스크의 **대각선**과 같다 (`make_att_2d_masks` 가
`att & pad_i & pad_j` 이고 자기 자신은 늘 자기를 본다) — 그래서 `embed_prefix` 를 또 감싸지 않는다.

## 헤드로 가는 그래디언트

`train_expert_only=True`(기본)면 VLM 은 얼어 있다. CE 는 헤드와 `state_proj`(state 토큰을 만드는
층, 학습됨)로만 흐른다 — 이미지·언어 표현은 바뀌지 않는다. "보조 손실로 표현을 정돈한다" 는 ACT-S
의 가설은 여기선 state 투영에만 해당한다. λ=0 이면 바닐라와 손실이 수치 동일한 것은 같다.
"""

import types

import torch
from torch import Tensor

from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy

from ..stage_head import StageHeadMixin
from .configuration_smolvla_s import SmolVLASConfig


def _capturing_forward(self, *args, **kwargs):
    """`SmolVLMWithExpertModel.forward` 그대로 + prefix 출력·패딩 마스크를 `self._stage_prefix` 에.

    인스턴스에 `types.MethodType` 으로 붙인다 — 모듈 수준 함수라 deepcopy·pickle 이 깨지지 않는다.
    """
    outputs, past_key_values = type(self).forward(self, *args, **kwargs)
    prefix_out = outputs[0]
    if prefix_out is not None:
        mask = kwargs.get("attention_mask", args[0] if args else None)
        n = prefix_out.shape[1]
        pad = mask.diagonal(dim1=1, dim2=2)[:, :n] if mask is not None else None
        self._stage_prefix = (prefix_out, pad)
    return outputs, past_key_values


class SmolVLASPolicy(StageHeadMixin, SmolVLAPolicy):
    config_class = SmolVLASConfig
    name = "smolvla_s"
    vanilla_type = "smolvla"

    def __init__(self, config: SmolVLASConfig, **kwargs):
        super().__init__(config, **kwargs)            # SmolVLA 전부 그대로
        vwe = getattr(self.model, "vlm_with_expert", None)
        if vwe is None:
            raise RuntimeError(
                "상류 SmolVLA 에 `model.vlm_with_expert` 가 없다 — prefix 를 꺼낼 자리가 사라졌다 "
                "(feature/smolvla-s.md §5)")
        vwe._stage_prefix = None
        vwe.forward = types.MethodType(_capturing_forward, vwe)
        self._init_stage_head(vwe.config.text_config.hidden_size)

    # ── 특징 ────────────────────────────────────────────────────────────────

    def _pooled(self) -> Tensor:
        """prefix `(B, P, D)` → `(B, D)`. 한 번 읽으면 비운다 — 낡은 prefix 를 다음 호출이 못 쓴다."""
        vwe = self.model.vlm_with_expert
        cap, vwe._stage_prefix = vwe._stage_prefix, None
        if cap is None:
            raise RuntimeError("VLM prefix 출력이 아직 없다 — forward()/predict_action_chunk() 안에서만 유효하다")
        h, pad = cap
        h = h.float()                      # VLM 은 bf16 이다 — 평균은 fp32 로
        if pad is None:
            pad = torch.ones(h.shape[:2], dtype=torch.bool, device=h.device)
        if self.config.pool == "state":
            # state 토큰은 실제 토큰의 마지막이다 — 그 뒤는 prefix_length 맞춤 패딩뿐이다
            last = pad.shape[1] - 1 - pad.flip(1).float().argmax(1)
            return h[torch.arange(h.shape[0], device=h.device), last]
        w = pad.float().unsqueeze(-1)
        return (h * w).sum(1) / w.sum(1).clamp_min(1)

    # ── 학습 ────────────────────────────────────────────────────────────────

    def forward(self, batch: dict[str, Tensor], noise=None, time=None, reduction: str = "mean"):
        loss, loss_dict = super().forward(batch, noise=noise, time=time, reduction=reduction)
        loss = self._add_stage_loss(self._pooled(), batch, loss, loss_dict)
        loss_dict["loss"] = loss.mean().item()
        return loss, loss_dict

    # ── 추론 ────────────────────────────────────────────────────────────────

    @torch.no_grad()
    def predict_action_chunk(self, batch: dict[str, Tensor], noise: Tensor | None = None, **kwargs) -> Tensor:
        """시그니처 불변 — RTC 인자도 그대로 넘긴다. stage 는 `last_aux` 로 읽는다."""
        actions = super().predict_action_chunk(batch, noise, **kwargs)
        self.last_aux = self._aux_from(self._pooled())
        return actions

    @torch.no_grad()
    def select_action(self, batch: dict[str, Tensor], noise: Tensor | None = None, **kwargs) -> Tensor:
        """큐가 빌 때만 청크를 새로 낸다 — 그때만 prefix 가 새로 돈다."""
        action = super().select_action(batch, noise, **kwargs)
        if self.model.vlm_with_expert._stage_prefix is not None:
            self.last_aux = self._aux_from(self._pooled())
        return action
