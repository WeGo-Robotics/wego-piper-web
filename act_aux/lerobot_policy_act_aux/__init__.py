"""lerobot_policy_act_aux — 행동 + 작업 단계(stage)를 함께 내는 ACT 변형.

기존 ACT(`lerobot.policies.act`)는 한 줄도 건드리지 않는다. `ACTConfig`/`ACTPolicy`
를 **상속**하고 LeRobot 의 서드파티 정책 플러그인 규약으로 꽂힌다
(feature/act-aux.md §1~2).

## 이 파일이 하는 일 하나

config 를 import 한다. 그것이 곧 `@PreTrainedConfig.register_subclass("act_aux")`
실행이고, 그 뒤로 `--policy.type=act_aux` 가 풀린다. 모델 모듈은 일부러 여기서
import 하지 않는다 — 팩토리가 이름 규약으로 늦게 찾고(§2.2), 백엔드 테스트가
torch 없이 config 기본값만 읽을 수 있어야 한다.
"""

from .configuration_act_aux import ActAuxConfig

__all__ = ["ActAuxConfig"]
