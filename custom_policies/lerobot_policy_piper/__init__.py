"""lerobot_policy_piper — Piper 커스텀 정책 모음.

| 타입 | 화면 이름 | 바탕 | 문서 |
|---|---|---|---|
| `act_s` | ACT-S | ACT 상속 + stage 헤드 | feature/act-s.md |
| `smolvla_s` | SmolVLA-S | SmolVLA 상속 + stage 헤드 | feature/smolvla-s.md |

상류 정책(`lerobot.policies.*`)은 한 줄도 건드리지 않는다. 전부 **상속**이고 LeRobot 의
서드파티 정책 플러그인 규약으로 꽂힌다 (feature/act-s.md §1~2). 두 정책이 같이 쓰는
stage 헤드는 `stage.py`, 학습 데이터 굽기는 `bake.py` 다.

## 이 파일이 하는 일 하나

config 를 import 한다. 그것이 곧 `@PreTrainedConfig.register_subclass(...)` 실행이고,
그 뒤로 `--policy.type=act_s` / `smolvla_s` 가 풀린다. 모델 모듈은 일부러 여기서 import
하지 않는다 — 팩토리가 이름 규약으로 늦게 찾고(§2.2), 백엔드 테스트가 torch 없이 config
기본값만 읽을 수 있어야 한다.
"""

from .act_s.configuration_act_s import ActSConfig
from .smolvla_s.configuration_smolvla_s import SmolVLASConfig

__all__ = ["ActSConfig", "SmolVLASConfig"]
