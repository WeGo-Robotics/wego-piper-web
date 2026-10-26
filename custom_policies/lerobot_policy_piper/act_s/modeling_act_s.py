"""`ActSPolicy` — ACT 그대로 + 인코더 출력에 붙은 stage 헤드 (feature/act-s.md §3).

## ACT 코드를 복사하지 않는다

`ACT.forward` 는 트랜스포머 인코더 출력(`(S, B, D)` 토큰 시퀀스)을 밖으로 내주지 않는다.
그걸 꺼내려고 forward 130줄을 베끼면 LeRobot 을 올릴 때마다 따라 고쳐야 한다. 대신
`self.model.encoder` 에 **forward hook** 을 걸어 출력을 받아둔다 — 상류가 `encoder`
모듈을 유지하는 한 그대로 산다 (§3.1).

⚠ VAE 인코더(`vae_encoder`)가 아니다. 그쪽은 학습 때 정답 액션을 보고 만들어지고
추론 때는 latent=0 이라 거기 붙인 헤드는 추론에서 무의미하다.

헤드·손실·confidence 는 SmolVLA-S 와 같은 코드다 (`stage_head.py`).
"""

import torch
from torch import Tensor

from lerobot.policies.act.modeling_act import ACTPolicy

from ..stage_head import StageHeadMixin
from .configuration_act_s import ActSConfig


class ActSPolicy(StageHeadMixin, ACTPolicy):
    config_class = ActSConfig
    name = "act_s"
    vanilla_type = "act"

    def __init__(self, config: ActSConfig, **kwargs):
        super().__init__(config, **kwargs)            # ACT 전부 그대로
        if config.pool == "state" and not config.robot_state_feature:
            raise ValueError("pool='state' 는 observation.state 입력이 있어야 합니다")
        encoder = getattr(self.model, "encoder", None)
        if encoder is None:
            raise RuntimeError(
                "상류 ACT 에 `model.encoder` 가 없다 — hook 을 붙일 자리가 사라졌다 "
                "(feature/act-s.md §11)")

        self._init_stage_head(config.dim_model)
        self._enc_out: Tensor | None = None
        encoder.register_forward_hook(self._capture)

    # ── 특징 ────────────────────────────────────────────────────────────────

    def _capture(self, _module, _inputs, output):
        self._enc_out = output

    def _pooled(self) -> Tensor:
        """`(S, B, D)` → `(B, D)`. forward/predict 안에서만 부른다 — 밖에서 부르면 stale 이다."""
        e = self._enc_out
        if e is None:
            raise RuntimeError("인코더 출력이 아직 없다 — forward()/predict_action_chunk() 안에서만 유효하다")
        if self.config.pool == "state":
            return e[1]            # 토큰 순서: [latent, robot_state, (env_state), 이미지…]
        return e.mean(0)

    # ── 학습 ────────────────────────────────────────────────────────────────

    def forward(self, batch: dict[str, Tensor]) -> tuple[Tensor, dict]:
        loss, loss_dict = super().forward(batch)   # l1 + kl_weight·kld. 이 호출이 hook 을 채운다
        loss = self._add_stage_loss(self._pooled(), batch, loss, loss_dict)
        return loss, loss_dict

    # ── 추론 ────────────────────────────────────────────────────────────────

    @torch.no_grad()
    def predict_action_chunk(self, batch: dict[str, Tensor]) -> Tensor:
        """시그니처 불변 — wrapper 는 액션만 받고, stage 는 `last_aux` 로 읽는다 (§6.1)."""
        actions = super().predict_action_chunk(batch)
        self.last_aux = self._aux_from(self._pooled())
        return actions
