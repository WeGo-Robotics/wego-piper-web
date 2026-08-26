"""`ActAuxPolicy` — ACT 그대로 + 인코더 출력에 붙은 stage 헤드 (feature/act-aux.md §3).

## ACT 코드를 복사하지 않는다

`ACT.forward` 는 트랜스포머 인코더 출력(`(S, B, D)` 토큰 시퀀스)을 밖으로 내주지 않는다.
그걸 꺼내려고 forward 130줄을 베끼면 LeRobot 을 올릴 때마다 따라 고쳐야 한다. 대신
`self.model.encoder` 에 **forward hook** 을 걸어 출력을 받아둔다 — 상류가 `encoder`
모듈을 유지하는 한 그대로 산다 (§3.1).

⚠ VAE 인코더(`vae_encoder`)가 아니다. 그쪽은 학습 때 정답 액션을 보고 만들어지고
추론 때는 latent=0 이라 거기 붙인 헤드는 추론에서 무의미하다.

## 배치 크기

`last_aux` 는 **첫 샘플**(index 0)의 값이다. wrapper 는 B=1 로 부른다.
"""

import logging

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from lerobot.policies.act.modeling_act import ACTPolicy

from .configuration_act_aux import SUBTASK_KEY, ActAuxConfig

logger = logging.getLogger(__name__)


class ActAuxPolicy(ACTPolicy):
    config_class = ActAuxConfig
    name = "act_aux"

    def __init__(self, config: ActAuxConfig, **kwargs):
        super().__init__(config, **kwargs)            # ACT 전부 그대로
        if config.pool == "state" and not config.robot_state_feature:
            raise ValueError("pool='state' 는 observation.state 입력이 있어야 합니다")
        encoder = getattr(self.model, "encoder", None)
        if encoder is None:
            raise RuntimeError(
                "상류 ACT 에 `model.encoder` 가 없다 — hook 을 붙일 자리가 사라졌다 "
                "(feature/act-aux.md §11)")

        d = config.dim_model
        self.stage_head = nn.Sequential(
            nn.Linear(d, 256), nn.ReLU(), nn.Dropout(0.1), nn.Linear(256, config.n_stages),
        )
        # 학습 중 본 라벨 빈도. 체크포인트에 같이 저장된다 — 이어 학습해도 가중이 이어진다.
        self.register_buffer(
            "stage_counts", torch.zeros(config.n_stages, dtype=torch.float64), persistent=True)

        self._enc_out: Tensor | None = None
        encoder.register_forward_hook(self._capture)
        self._n_forward = 0
        self.last_aux: dict | None = None

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

    def _targets(self, subtasks) -> Tensor:
        """배치의 `subtask`(문자열 목록) → 클래스 인덱스. 모르는 이름은 -1 (무시)."""
        if isinstance(subtasks, str):
            subtasks = [subtasks]
        if not hasattr(self, "_stage_index"):
            self._stage_index = {n: i for i, n in enumerate(self.config.stage_names)}
        idx = [self._stage_index.get(s, -1) for s in subtasks]
        return torch.tensor(idx, dtype=torch.long, device=self.stage_counts.device)

    def _class_weight(self) -> Tensor | None:
        cfg = self.config
        if cfg.stage_class_weights is not None:
            return torch.tensor(cfg.stage_class_weights, dtype=torch.float32,
                                device=self.stage_counts.device)
        if not cfg.stage_balance or self._n_forward < cfg.stage_balance_warmup:
            return None
        counts = self.stage_counts.clamp(min=1)
        w = (counts.sum() / counts).sqrt()     # √역빈도 — 역빈도 그대로는 희귀 클래스가 손실을 집어삼킨다
        return (w / w.mean()).float()          # 평균 1 → λ 의 의미가 안 바뀐다

    def forward(self, batch: dict[str, Tensor]) -> tuple[Tensor, dict]:
        loss, loss_dict = super().forward(batch)   # l1 + kl_weight·kld. 이 호출이 hook 을 채운다
        cfg = self.config
        if SUBTASK_KEY not in batch:
            # 조용히 건너뛰면 "act_aux 로 학습했는데 stage 가 늘 6" 같은 사고가 된다.
            raise KeyError(
                f"배치에 {SUBTASK_KEY!r} 가 없다 — act_aux 는 `python -m lerobot_policy_act_aux.bake` 로 "
                f"만든 `_stage` 데이터셋(meta/subtasks.parquet + subtask_index)이 필요하다 "
                f"(feature/act-aux.md §4). 바닐라 ACT 로 학습하려면 policy.type=act 를 쓴다.")

        tgt = self._targets(batch[SUBTASK_KEY])
        valid = tgt >= 0                                    # -1 = 라벨 없음 (세그먼트 밖 / reviewed-only 제외)
        logits = self.stage_head(self._pooled().float())

        if self.training:
            self._n_forward += 1
            if valid.any():
                self.stage_counts += torch.bincount(
                    tgt[valid], minlength=cfg.n_stages).to(self.stage_counts.dtype)

        if valid.any():
            ce = F.cross_entropy(logits, tgt, weight=self._class_weight(), ignore_index=-1,
                                 label_smoothing=cfg.label_smoothing)
            with torch.no_grad():
                acc = (logits.argmax(-1)[valid] == tgt[valid]).float().mean()
        else:
            ce = logits.sum() * 0.0        # 전부 무시 → CE 가 nan 이 되는 것을 막는다
            acc = torch.zeros((), device=logits.device)

        loss_dict["stage_ce"] = ce.item()
        loss_dict["stage_acc"] = acc.item()
        if cfg.stage_loss_weight > 0:
            loss = loss + cfg.stage_loss_weight * ce

        # lerobot-train 은 loss_dict 를 wandb 로만 보내고 로그 줄에는 안 찍는다 — 직접 찍는다
        if self.training and cfg.stage_log_freq and self._n_forward % cfg.stage_log_freq == 0:
            logger.info("act_aux stage_ce:%.4f stage_acc:%.3f", ce.item(), acc.item())
        return loss, loss_dict

    # ── 추론 ────────────────────────────────────────────────────────────────

    @torch.no_grad()
    def predict_action_chunk(self, batch: dict[str, Tensor]) -> Tensor:
        """시그니처 불변 — wrapper 는 액션만 받고, stage 는 `last_aux` 로 읽는다 (§6.1)."""
        actions = super().predict_action_chunk(batch)
        self.last_aux = self._aux_from(self._pooled().float())
        return actions

    def _aux_from(self, pooled: Tensor) -> dict:
        """§3.4 — softmax 확률 + 그보다 나은 값들. 인코더 1회 위라 비용이 안 는다."""
        cfg = self.config
        p = (self.stage_head(pooled) / cfg.temperature).softmax(-1)
        top2 = p.topk(2, dim=-1).values[0]
        entropy = -(p[0] * p[0].clamp_min(1e-12).log()).sum()
        stage = int(p[0].argmax())
        out = {
            "stage": stage,
            "stage_p": float(top2[0]),
            "margin": float(top2[0] - top2[1]),
            "entropy": float(entropy),
            "probs": p[0].tolist(),
            "mc_std": None,
        }
        if cfg.mc_samples > 0:
            was_training = self.stage_head.training
            self.stage_head.train()                        # dropout 만 켠다 — 인코더는 그대로
            samples = torch.stack([
                (self.stage_head(pooled) / cfg.temperature).softmax(-1)[0, stage]
                for _ in range(cfg.mc_samples)
            ])
            self.stage_head.train(was_training)
            out["mc_std"] = float(samples.std())
        return out
