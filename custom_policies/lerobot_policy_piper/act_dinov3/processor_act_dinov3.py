"""전처리/후처리는 ACT 것 그대로 — 입력·출력 feature 가 같다.

이미지 정규화도 ACT 와 같다(데이터셋 통계, 기본은 ImageNet 평균·표준편차). DINOv3 LVD 가
그 통계로 학습됐으므로 따로 손대지 않는다. 함수 이름은 규약이다 (`make_{type}_pre_post_processors`).
"""

from lerobot.policies.act.processor_act import make_act_pre_post_processors


def make_act_dinov3_pre_post_processors(config, dataset_stats=None, **kwargs):
    return make_act_pre_post_processors(config, dataset_stats=dataset_stats, **kwargs)
