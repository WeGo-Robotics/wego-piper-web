"""stage 헤드 — ACT-S · SmolVLA-S 가 같이 쓰는 부분 (feature/act-s.md §3).

정책마다 다른 것은 **특징을 어디서 꺼내나** 하나뿐이다: ACT-S 는 트랜스포머 인코더 출력,
SmolVLA-S 는 VLM prefix 출력. 풀링한 `(B, D)` 벡터부터 헤드·손실·confidence 는 여기서 같다 —
두 정책의 `last_aux` 가 같은 모양이어야 wrapper 가 정책 타입 분기 없이 읽는다.

## 쓰는 법

`class XxxSPolicy(StageHeadMixin, XxxPolicy)` 로 섞고:

1. `__init__` 끝에서 `self._init_stage_head(d)` — `d` 는 풀링한 특징 폭
2. 손실 forward 에서 `loss = self._add_stage_loss(pooled, batch, loss, loss_dict)`
3. 추론에서 `self.last_aux = self._aux_from(pooled)`

## 배치 크기

`last_aux` 는 **첫 샘플**(index 0)의 값이다. wrapper 는 B=1 로 부른다.
"""

import logging

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from .stage_config import SUBTASK_KEY

logger = logging.getLogger(__name__)


class StageHeadMixin:
    """`self.config` 가 `StageConfig` 를 상속한 정책에 섞는다."""

    #: 이 정책의 바닐라 타입 — 라벨 없는 데이터셋으로 학습하려 할 때 안내한다
    vanilla_type: str = ""

    def _init_stage_head(self, d: int) -> None:
        cfg = self.config
        self.stage_head = nn.Sequential(
            nn.Linear(d, 256), nn.ReLU(), nn.Dropout(0.1), nn.Linear(256, cfg.n_stages),
        )
        # 학습 중 본 라벨 빈도. 체크포인트에 같이 저장된다 — 이어 학습해도 가중이 이어진다.
        self.register_buffer(
            "stage_counts", torch.zeros(cfg.n_stages, dtype=torch.float64), persistent=True)
        self._n_forward = 0
        self.last_aux: dict | None = None

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

    def _add_stage_loss(self, pooled: Tensor, batch: dict, loss: Tensor, loss_dict: dict) -> Tensor:
        """바닐라 손실 + λ·CE. `loss_dict` 에 `stage_ce`/`stage_acc` 를 채운다.

        `loss` 가 샘플별(B,)이어도(RA-BC 의 `reduction="none"`) 배치 평균 CE 를 더한다 —
        샘플마다 같은 값이 붙으므로 평균은 스칼라일 때와 같다.
        """
        cfg = self.config
        if SUBTASK_KEY not in batch:
            # 조용히 건너뛰면 "stage 헤드로 학습했는데 stage 가 늘 6" 같은 사고가 된다.
            raise KeyError(
                f"배치에 {SUBTASK_KEY!r} 가 없다 — {self.name} 은 `python -m lerobot_policy_piper.bake` 로 "
                f"만든 `_stage` 데이터셋(meta/subtasks.parquet + subtask_index)이 필요하다 "
                f"(feature/act-s.md §4). 바닐라로 학습하려면 policy.type={self.vanilla_type} 을 쓴다.")

        tgt = self._targets(batch[SUBTASK_KEY])
        valid = tgt >= 0                                    # -1 = 라벨 없음 (세그먼트 밖 / reviewed-only 제외)
        logits = self.stage_head(pooled.float())

        if self.training:
            self._n_forward += 1
            if valid.any():
                self.stage_counts += torch.bincount(
                    tgt[valid], minlength=cfg.n_stages).to(self.stage_counts.dtype)

        if valid.any():
            # ⚠ smoothing 항은 **가중 없이** 직접 더한다. PyTorch 는 `weight` 를 smoothing 항에도
            # 곱해서, 무거운 클래스(드물거나 한 번도 안 나와 clamp 된 것)가 있으면 손실이 모든
            # 샘플에서 그 클래스를 고르라고 민다 — 정답을 다 아는 헤드도 정확도 0 이 된다
            # (2026-10-30 dualarm_sort_apple_stage: PARKING 0프레임 → stage_acc 0, CE 3.1→4.3 상승).
            # 가중이 없으면 F.cross_entropy(label_smoothing=ε) 와 수치가 같다.
            eps = cfg.label_smoothing
            ce = F.cross_entropy(logits, tgt, weight=self._class_weight(), ignore_index=-1)
            if eps > 0:
                uniform = -logits[valid].log_softmax(-1).mean(-1).mean()
                ce = (1 - eps) * ce + eps * uniform
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
            logger.info("%s stage_ce:%.4f stage_acc:%.3f", self.name, ce.item(), acc.item())
        return loss

    # ── 추론 ────────────────────────────────────────────────────────────────

    def _aux_from(self, pooled: Tensor) -> dict:
        """§3.4 — softmax 확률 + 그보다 나은 값들. 인코더 1회 위라 비용이 안 는다."""
        cfg = self.config
        pooled = pooled.float()
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
