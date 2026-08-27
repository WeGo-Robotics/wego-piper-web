"""`act_aux` config — `ACTConfig` 위에 stage 헤드 필드만 얹는다.

⚠ 이름 규약 (feature/act-aux.md §2.2). 팩토리가 이 클래스 이름에서 나머지를 유도한다:
`ActAuxConfig` → `ActAuxPolicy`, `configuration_act_aux` → `modeling_act_aux` /
`processor_act_aux`, 타입 `act_aux` → `make_act_aux_pre_post_processors`.
하나라도 틀리면 "Policy type 'act_aux' is not available" 이다. `tests/test_naming.py` 가 잠근다.
"""

from dataclasses import dataclass, field

from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.act.configuration_act import ACTConfig

POOL_CHOICES = ("mean", "state")
# = piper_phase.PHASE_NAMES. 정책 패키지가 piper_phase 를 import 하지 않는 이유: 학습 머신·컨테이너에
# phase 패키지가 없어도 정책은 돌아야 한다. 대신 테스트가 두 목록을 대조한다.
DEFAULT_STAGE_NAMES = ("IDLE", "APPROACH", "ALIGN", "GRASP", "HOLD", "RELEASE",
                       "DONE", "PARKING")
SUBTASK_KEY = "subtask"              # LeRobot 이 배치에 넣어주는 프레임별 하위작업 이름 (§4.1)


@PreTrainedConfig.register_subclass("act_aux")
@dataclass
class ActAuxConfig(ACTConfig):
    """ACT 필드 전부 + 아래. 전부 기본값이 있어야 한다 (draccus)."""

    # ── stage 헤드 ──
    # 클래스 = LeRobot `subtask` 이름. 학습 배치의 `subtask`(문자열)를 이 순서의 인덱스로 바꾼다.
    # 목록에 없는 이름(굽기의 `_unlabeled`)은 -1 = 손실에서 무시. 체크포인트 config 에 저장되므로
    # 추론 쪽이 piper_phase 없이도 이름을 안다. 기본값은 piper_phase.PHASE_NAMES 와 같아야 한다
    # (backend/tests/test_act_aux_contract.py 가 대조).
    stage_names: list[str] = field(default_factory=lambda: list(DEFAULT_STAGE_NAMES))
    stage_loss_weight: float = 0.1       # λ. 0 이면 바닐라 ACT 와 손실이 수치 동일 — A/B 대조군
    label_smoothing: float = 0.1         # CE 포화 방지 — confidence 값들이 매끄러워진다 (§3.4)
    # 불균형: HOLD/APPROACH 가 대부분, GRASP/RELEASE/IDLE/DONE 은 몇 프레임.
    # True 면 학습 중 본 라벨 빈도로 √역빈도 가중을 **자동** 계산한다 (워밍업 뒤).
    # `stage_class_weights` 를 주면 그 값이 이긴다.
    stage_balance: bool = True
    stage_class_weights: list[float] | None = None
    stage_balance_warmup: int = 50       # 이만큼 배치를 본 뒤부터 가중 적용
    # 인코더 토큰 → 벡터. mean = 전 토큰 평균, state = robot_state 토큰 (§3.1)
    pool: str = "mean"

    # ── confidence (§3.4) ──
    temperature: float = 1.0             # 사후 보정 T. calibrate 도구가 채운다
    mc_samples: int = 0                  # >0 이면 헤드만 N회 dropout 재실행해 분산을 낸다
    stage_log_freq: int = 200            # 학습 중 stage_ce/acc 를 몇 forward 마다 로그에 찍나 (0=끔)

    @property
    def n_stages(self) -> int:
        return len(self.stage_names)

    def __post_init__(self):
        super().__post_init__()
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
