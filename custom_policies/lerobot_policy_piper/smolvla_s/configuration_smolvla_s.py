"""`smolvla_s` config — `SmolVLAConfig` 위에 stage 헤드 필드(`StageConfig`)만 얹는다.

⚠ 이름 규약 (feature/act-s.md §2.2). `SmolVLASConfig` → `SmolVLASPolicy`,
`configuration_smolvla_s` → `modeling_smolvla_s` / `processor_smolvla_s`,
타입 `smolvla_s` → `make_smolvla_s_pre_post_processors`. `tests/test_smolvla_s.py` 가 잠근다.
"""

from dataclasses import dataclass

from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig

from ..stage_config import StageConfig


@PreTrainedConfig.register_subclass("smolvla_s")
@dataclass
class SmolVLASConfig(StageConfig, SmolVLAConfig):
    """SmolVLA 필드 전부 + `StageConfig`. 전부 기본값이 있어야 한다 (draccus)."""

    # ACT-S 와 기본값이 다르다. SmolVLA prefix 에서 이미지·언어 토큰은 state 토큰을 못 보고
    # (블록 인과 마스크), state 토큰만 그 앞의 전부를 본다 — 평균은 state 를 거의 안 담는다.
    # ACT 인코더는 양방향이라 평균이 이미 전부를 본다 (feature/smolvla-s.md §2).
    pool: str = "state"

    def __post_init__(self):
        super().__post_init__()
        self.validate_stage()
