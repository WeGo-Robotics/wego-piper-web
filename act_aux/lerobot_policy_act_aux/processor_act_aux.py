"""전처리/후처리는 ACT 것 그대로 — 입력·출력 feature 가 같다.

`task_stage` 는 `input_features`/`output_features` 어디에도 없으므로 정규화기가
건드리지 않고 배치에 원값으로 실린다 (feature/act-aux.md §4.1). 그래서 CE 타깃으로 그냥 쓴다.
함수 이름은 규약이다 (`make_{type}_pre_post_processors`).
"""

from lerobot.policies.act.processor_act import make_act_pre_post_processors


def make_act_aux_pre_post_processors(config, dataset_stats=None, **kwargs):
    return make_act_pre_post_processors(config, dataset_stats=dataset_stats, **kwargs)
