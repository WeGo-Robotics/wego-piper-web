"""stage 헤드 config 필드 — ACT-S · SmolVLA-S 공용 (feature/act-s.md §3.2).

`ActSConfig(StageConfig, ACTConfig)` 처럼 상류 config 와 **같이 상속**한다. 필드는 전부
기본값이 있어야 한다 (draccus). 검증은 `__post_init__` 이 아니라 `validate_stage()` 다 —
상류 `__post_init__` 사슬이 이 믹스인까지 내려온다는 보장이 없으므로 각 정책이 직접 부른다.

torch 를 import 하지 않는다 — 백엔드 테스트가 기본값만 읽는다.
"""

from dataclasses import dataclass, field

POOL_CHOICES = ("mean", "state")
# = piper_phase.PHASE_NAMES. 정책 패키지가 piper_phase 를 import 하지 않는 이유: 학습 머신·컨테이너에
# phase 패키지가 없어도 정책은 돌아야 한다. 대신 테스트가 두 목록을 대조한다.
DEFAULT_STAGE_NAMES = ("IDLE", "APPROACH", "ALIGN", "GRASP", "HOLD", "RELEASE",
                       "DONE", "PARKING")
SUBTASK_KEY = "subtask"              # LeRobot 이 배치에 넣어주는 프레임별 하위작업 이름 (§4.1)


@dataclass
class StageConfig:
    """stage 헤드 필드. 상류 config 앞에 섞는다 — 그래야 저장된 config.json 에서 뒤쪽에 온다."""

    # ── stage 헤드 ──
    # 클래스 = LeRobot `subtask` 이름. 학습 배치의 `subtask`(문자열)를 이 순서의 인덱스로 바꾼다.
    # 목록에 없는 이름(굽기의 `_unlabeled`)은 -1 = 손실에서 무시. 체크포인트 config 에 저장되므로
    # 추론 쪽이 piper_phase 없이도 이름을 안다. 기본값은 piper_phase.PHASE_NAMES 와 같아야 한다
    # (backend/tests/test_custom_policies_contract.py 가 대조).
    stage_names: list[str] = field(default_factory=lambda: list(DEFAULT_STAGE_NAMES))
    stage_loss_weight: float = 0.1       # λ. 0 이면 바닐라와 손실이 수치 동일 — A/B 대조군
    label_smoothing: float = 0.1         # CE 포화 방지 — confidence 값들이 매끄러워진다 (§3.4)
    # 불균형: HOLD/APPROACH 가 대부분, GRASP/RELEASE/IDLE/DONE 은 몇 프레임.
    # True 면 학습 중 본 라벨 빈도로 √역빈도 가중을 **자동** 계산한다 (워밍업 뒤).
    # `stage_class_weights` 를 주면 그 값이 이긴다.
    stage_balance: bool = True
    stage_class_weights: list[float] | None = None
    stage_balance_warmup: int = 50       # 이만큼 배치를 본 뒤부터 가중 적용
    # 토큰 → 벡터. mean = 전 토큰 평균, state = 로봇 state 토큰 하나 (§3.1).
    # 정책마다 기본값이 다르다 — 어느 토큰이 무엇을 보는지가 다르다 (smolvla-s.md §2).
    pool: str = "mean"

    # ── confidence (§3.4) ──
    temperature: float = 1.0             # 사후 보정 T. calibrate 도구가 채운다
    mc_samples: int = 0                  # >0 이면 헤드만 N회 dropout 재실행해 분산을 낸다
    stage_log_freq: int = 200            # 학습 중 stage_ce/acc 를 몇 forward 마다 로그에 찍나 (0=끔)

    @property
    def n_stages(self) -> int:
        return len(self.stage_names)

    def validate_stage(self) -> None:
        if len(self.stage_names) < 2:
            raise ValueError(f"stage_names 는 2개 이상이어야 합니다: {self.stage_names}")
        if len(set(self.stage_names)) != len(self.stage_names):
            raise ValueError(f"stage_names 에 중복이 있습니다: {self.stage_names}")
        if self.pool not in POOL_CHOICES:
            raise ValueError(f"pool 은 {POOL_CHOICES} 중 하나여야 합니다: {self.pool!r}")
        if self.stage_class_weights is not None and len(self.stage_class_weights) != self.n_stages:
            raise ValueError(
                f"stage_class_weights 길이 {len(self.stage_class_weights)} ≠ n_stages {self.n_stages}")
        if self.stage_loss_weight < 0:
            raise ValueError("stage_loss_weight 는 음수일 수 없습니다")
        if not (0.0 <= self.label_smoothing < 1.0):
            raise ValueError("label_smoothing 은 [0, 1) 이어야 합니다")
        if self.temperature <= 0:
            raise ValueError("temperature 는 양수여야 합니다")
        if self.mc_samples < 0:
            raise ValueError("mc_samples 는 음수일 수 없습니다")
