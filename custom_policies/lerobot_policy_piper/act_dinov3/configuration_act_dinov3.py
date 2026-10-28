"""`act_dinov3` config — `ACTConfig` 위에 DINOv3 백본 필드만 얹는다.

⚠ 이름 규약 (feature/act-s.md §2.2). `ActDinov3Config` → `ActDinov3Policy`,
`configuration_act_dinov3` → `modeling_act_dinov3` / `processor_act_dinov3`,
타입 `act_dinov3` → `make_act_dinov3_pre_post_processors`. `tests/test_act_dinov3.py` 가 잠근다.

torch·timm 을 import 하지 않는다 — 백엔드 테스트가 기본값만 읽는다.
"""

from dataclasses import dataclass

from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.act.configuration_act import ACTConfig


@PreTrainedConfig.register_subclass("act_dinov3")
@dataclass
class ActDinov3Config(ACTConfig):
    """ACT 필드 전부 + DINOv3 백본. 전부 기본값이 있어야 한다 (draccus)."""

    # timm 이름 `아키텍처.태그`. 태그는 LVD-1689M(일반 사진) 사전학습이어야 한다 — 입력이 ImageNet
    # 평균·표준편차로 정규화돼 오고(LeRobot `use_imagenet_stats`), LVD 가 그 통계로 학습됐다.
    # ViT(`vit_*_patch16_dinov3`)와 ConvNeXt(`convnext_*.dinov3_*`) 둘 다 된다.
    dino_model: str = "vit_small_patch16_dinov3.lvd1689m"
    # 카메라 픽셀 몇 개(한 변)가 인코더 토큰 하나가 되나. ResNet18 ACT 가 32 다 — 그래서 기본값이
    # 32 면 토큰 수가 바닐라 ACT 와 같다(480×640 → 카메라당 15×20). ViT/16 은 그만큼 이미지를
    # 줄여서 넣고, ConvNeXt(보폭 32)는 그대로 넣는다. 16 이면 ViT 를 원본 크기로 돌려 토큰이 4배다.
    dino_feature_stride: int = 32
    # True 면 DINOv3 를 고정하고 나머지만 학습한다. False 면 ACT 가 ResNet 을 다루듯
    # `optimizer_lr_backbone` 으로 같이 학습한다 — 바닐라 ACT 와 A/B 할 때 다른 것이 백본뿐이게.
    freeze_backbone: bool = False

    def __post_init__(self):
        super().__post_init__()
        # ACT.__init__ 이 ResNet18 을 만든 뒤 우리가 바로 버린다 — ImageNet 가중치까지 받을 이유가 없다.
        # `vision_backbone` 은 상류 검증(resnet 만 허용) 때문에 그대로 둔다. 이 정책에선 안 쓰인다.
        self.pretrained_backbone_weights = None
        if "dinov3" not in self.dino_model:
            raise ValueError(f"dino_model 은 timm 의 DINOv3 모델이어야 합니다: {self.dino_model}")
        if self.dino_feature_stride < 1:
            raise ValueError(f"dino_feature_stride 는 1 이상이어야 합니다: {self.dino_feature_stride}")
