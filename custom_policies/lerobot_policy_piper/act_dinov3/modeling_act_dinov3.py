"""`ActDinov3Policy` — ACT 그대로, 비전 백본만 DINOv3 (feature/act-dinov3.md).

## ACT 코드를 복사하지 않는다

`ACT.forward` 가 이미지에서 쓰는 것은 두 모듈뿐이다 — `backbone(img)["feature_map"]` 과
그 채널을 `dim_model` 로 줄이는 1×1 conv `encoder_img_feat_input_proj`. 둘을 **같은 모양으로
갈아 끼우면** forward 는 한 줄도 안 건드린다. 2D 사인 위치 임베딩·트랜스포머·VAE 는 특징맵
크기만 맞으면 그대로 돈다. ACT-S 가 hook 으로 버티는 것과 같은 이유다 — 상류를 올릴 때
따라 고칠 곳이 없다.

## 가중치는 timm 에서

Meta 원본(`facebook/dinov3-*`)은 HF 승인제라 학습 기계·계정마다 신청해야 한다. timm 사본
(`timm/*dinov3*`)은 같은 가중치(DINOv3 라이선스)이고 승인 없이 받힌다. 그리고 timm 은
**아키텍처를 코드로** 갖고 있어 이름만으로 빈 모델을 만들 수 있다 — 체크포인트를 열 때
(`from_pretrained`) 네트워크가 필요 없다. 추론 기계가 오프라인이어도 열린다.
"""

from contextvars import ContextVar

import torch.nn.functional as F
from torch import Tensor, nn

from lerobot.policies.act.modeling_act import ACTPolicy

from .configuration_act_dinov3 import ActDinov3Config

# `from_pretrained` 안이면 True — 곧 체크포인트가 백본까지 덮으므로 사전학습 가중치를 받지 않는다.
# 생성자 인자로 못 넘긴다: LeRobot 이 `cls(config, **kwargs)` 로 부르고 kwargs 는 config 로더와 같이 쓴다.
_FROM_CHECKPOINT: ContextVar[bool] = ContextVar("act_dinov3_from_checkpoint", default=False)


class DinoBackbone(nn.Module):
    """ACT 의 `model.backbone` 자리 — ResNet 과 같이 `{"feature_map": (B, C, h, w)}` 를 낸다."""

    def __init__(self, name: str, pretrained: bool, feature_stride: int):
        super().__init__()
        try:
            import timm
        except ImportError as e:      # 설치 목록에서 빠진 기계 — 원인을 바로 말한다
            raise ImportError("act_dinov3 는 timm 이 필요합니다 (pip install timm, feature/act-dinov3.md)") from e
        # ViT 만 임의 크기를 받게 켠다 (RoPE 라 위치 보간이 필요 없다). ConvNeXt 는 이 인자가 없다.
        kw = {"dynamic_img_size": True} if name.startswith("vit_") else {}
        self.net = timm.create_model(name, pretrained=pretrained, num_classes=0, **kw)
        self.channels: int = self.net.num_features
        self.reduction: int = self.net.feature_info[-1]["reduction"]   # ViT/16 = 16, ConvNeXt = 32
        if feature_stride < self.reduction:
            raise ValueError(f"dino_feature_stride({feature_stride})가 {name} 의 보폭({self.reduction})보다 "
                             "작습니다 — 이미지를 키워 넣게 됩니다")
        self.feature_stride = feature_stride

    def forward(self, img: Tensor) -> dict[str, Tensor]:
        H, W = img.shape[-2:]
        gh, gw = max(1, round(H / self.feature_stride)), max(1, round(W / self.feature_stride))
        size = (gh * self.reduction, gw * self.reduction)
        if (H, W) != size:
            # 정규화 뒤라도 쌍선형은 선형이라 순서가 상관없다. 줄일 때 앨리어싱을 막는다.
            img = F.interpolate(img, size=size, mode="bilinear", antialias=True, align_corners=False)
        feat = self.net.forward_features(img)
        if feat.ndim == 3:
            # ViT: (B, cls + 레지스터 + gh·gw, C) → 패치 토큰만 (B, C, gh, gw)
            feat = feat[:, self.net.num_prefix_tokens:].transpose(1, 2).reshape(feat.shape[0], -1, gh, gw)
        return {"feature_map": feat}


class ActDinov3Policy(ACTPolicy):
    config_class = ActDinov3Config
    name = "act_dinov3"

    def __init__(self, config: ActDinov3Config, **kwargs):
        super().__init__(config, **kwargs)            # ACT 전부 그대로 — 백본은 아래에서 바꾼다
        if not config.image_features:
            raise ValueError("act_dinov3 는 카메라 입력이 있어야 합니다 — 이미지 없이 학습하려면 act 를 쓴다")
        backbone = DinoBackbone(config.dino_model, pretrained=not _FROM_CHECKPOINT.get(),
                                feature_stride=config.dino_feature_stride)
        # 이름이 `model.backbone` 이어서 ACT 의 `get_optim_params` 가 `optimizer_lr_backbone` 을 그대로 건다
        self.model.backbone = backbone
        self.model.encoder_img_feat_input_proj = nn.Conv2d(backbone.channels, config.dim_model, kernel_size=1)
        if config.freeze_backbone:
            backbone.requires_grad_(False)

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        token = _FROM_CHECKPOINT.set(True)
        try:
            return super().from_pretrained(*args, **kwargs)
        finally:
            _FROM_CHECKPOINT.reset(token)
