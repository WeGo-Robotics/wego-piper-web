"""`act_s` config — `ACTConfig` 위에 stage 헤드 필드(`StageConfig`)만 얹는다.

⚠ 이름 규약 (feature/act-s.md §2.2). 팩토리가 이 클래스 이름에서 나머지를 유도한다:
`ActSConfig` → `ActSPolicy`, `configuration_act_s` → `modeling_act_s` /
`processor_act_s`, 타입 `act_s` → `make_act_s_pre_post_processors`.
하나라도 틀리면 "Policy type 'act_s' is not available" 이다. `tests/test_act_s.py` 가 잠근다.
"""

from dataclasses import dataclass

from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.act.configuration_act import ACTConfig

from ..stage_config import StageConfig

# 이름을 바꾸기 전(ACT-Aux)의 타입. 그때 저장한 체크포인트의 config.json 이 `"type": "act_aux"`
# 라 이 이름으로도 열려야 한다. **`act_s` 다음에** 등록한다 — draccus 는 클래스의 이름을
# 등록 순서로 찾으므로(`get_choice_name`) 다시 저장하면 `act_s` 가 된다.
LEGACY_TYPES = ("act_aux",)


@PreTrainedConfig.register_subclass("act_s")
@dataclass
class ActSConfig(StageConfig, ACTConfig):
    """ACT 필드 전부 + `StageConfig`. 전부 기본값이 있어야 한다 (draccus)."""

    def __post_init__(self):
        super().__post_init__()
        self.validate_stage()


for _legacy in LEGACY_TYPES:
    # 옛 패키지(lerobot_policy_act_aux)가 아직 깔린 기계에선 그쪽이 이 이름을 먼저 가져간다 —
    # 여기서 다시 등록하면 ValueError 로 이 패키지 전체가 import 에 실패한다.
    if _legacy not in PreTrainedConfig.get_known_choices():
        PreTrainedConfig.register_subclass(_legacy, ActSConfig)
