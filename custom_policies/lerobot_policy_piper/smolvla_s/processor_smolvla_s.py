"""전처리/후처리는 SmolVLA 것 그대로 — 입력·출력 feature 가 같다.

`subtask` 는 화이트리스트로 배치까지 살아온다 (feature/act-s.md §4.1). SmolVLA 의 토크나이저
단계는 `subtask` 도 토큰으로 바꿔 관측에 얹지만(`observation.language.subtask_tokens`) SmolVLA 는
그 키를 읽지 않는다 — 문자열은 그대로 남아 CE 타깃이 된다. 함수 이름은 규약이다.
"""

from lerobot.policies.smolvla.processor_smolvla import make_smolvla_pre_post_processors


def make_smolvla_s_pre_post_processors(config, dataset_stats=None, **kwargs):
    return make_smolvla_pre_post_processors(config, dataset_stats=dataset_stats, **kwargs)
