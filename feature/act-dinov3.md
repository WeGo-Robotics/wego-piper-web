# ACT-DINOv3 — ACT 의 비전 백본을 DINOv3 로

상태: **◐ 1단계 ☑** (2026-10-27) — 정책·화면·학습·추론 배선, 실제 데이터셋으로 학습 60스텝 + 오프라인 재로드 확인.
남은 것: 바닐라 ACT 와의 A/B 실기 (§5).

## 1. 무엇이 다른가

ACT 는 카메라 영상을 **ResNet18(ImageNet)** 으로 특징맵을 만든 뒤 트랜스포머에 넣는다. `act_dinov3` 는 그 자리에
**DINOv3** 를 넣고 **나머지는 한 줄도 안 바꾼다** — 트랜스포머·VAE·액션 헤드·전처리·추론 파라미터(시간 앙상블 등)가
ACT 그대로다. 그래서 바닐라 ACT 와 A/B 하면 다른 것이 백본뿐이다.

ACT 는 LeRobot 에서 백본을 ResNet 계열만 받는다(`ACTConfig.__post_init__`). 그래서 설정으로는 안 되고 새 정책 타입이다
— ACT-S 와 같은 패키지(`custom_policies/`, 배포명 `lerobot_policy_piper`)에 있다.

## 2. 어떻게 꽂았나

`ACT.forward` 가 이미지에서 쓰는 것은 두 모듈뿐이다: `backbone(img)["feature_map"]` 과 그 채널을 `dim_model` 로
줄이는 1×1 conv. `ActDinov3Policy` 는 ACT 를 그대로 만든 뒤 **이 둘만 같은 모양으로 갈아 끼운다**
([modeling_act_dinov3.py](../custom_policies/lerobot_policy_piper/act_dinov3/modeling_act_dinov3.py)). forward 를
복사하지 않으므로 LeRobot 을 올릴 때 따라 고칠 곳이 없다.

- **토큰 수는 ResNet18 ACT 와 같다.** `dino_feature_stride`(기본 32)는 "카메라 픽셀 몇 개가 토큰 하나인가"다.
  ResNet18 이 32 라 480×640 → 카메라당 15×20 토큰. ViT/16 은 이미지를 절반으로 줄여(240×320) 넣고 ConvNeXt(보폭 32)는
  그대로 넣는다. 16 으로 내리면 ViT 를 원본 크기로 돌린다 — 토큰 4배, 인코더 주의 비용 16배
- **정규화는 손대지 않는다.** LeRobot 이 카메라를 ImageNet 평균·표준편차로 정규화하고(`use_imagenet_stats` 기본값)
  DINOv3 LVD-1689M 이 그 통계로 학습됐다. 위성용(`sat493m`) 가중치는 통계가 달라 맞지 않는다
- **백본 학습**: 기본은 ACT 가 ResNet 을 다루듯 `optimizer_lr_backbone`(1e-5)으로 같이 학습한다. `freeze_backbone` 을
  켜면 DINOv3 는 고정, 투영 conv 부터 학습한다

## 3. 가중치 — timm

Meta 원본(`facebook/dinov3-*`)은 HF **승인제**다. 이 기계의 계정(`wego-mink`)도 403 이었다 — 학습 기계·계정마다
신청해야 한다. timm 사본(`timm/vit_small_patch16_dinov3.lvd1689m` 등)은 **같은 가중치**이고 승인 없이 받힌다.
라이선스는 그대로 DINOv3 License 다.

timm 은 아키텍처를 코드로 갖고 있어 이름만으로 빈 모델을 만든다. 그래서:

- **처음부터 학습** — 사전학습 가중치를 받는다(ViT-S/16 약 90MB, HF 캐시)
- **체크포인트 열기**(`from_pretrained` — 추론·정책 서버·오프라인 평가·이어 학습) — 받지 않는다. 체크포인트가 백본까지
  덮기 때문이다. **추론 기계가 오프라인이어도 열린다** (네트워크를 막고 확인했다)

설치: `custom_policies` 의존성(`timm>=1.0.20`). 이미지 둘은 그 패키지를 `--no-deps` 로 깔아서 따로 핀이 있다 —
[backend/Dockerfile](../backend/Dockerfile) · [install-stack.sh](../deploy/train/install-stack.sh) (`timm==1.0.30`,
`test_train_image.py` 가 둘을 대조).

## 4. 쓰기

학습 화면에서 정책 **ACT-DINOv3** 를 고른다. 데이터셋 조건은 ACT 와 같다(굽기 불필요). 기본은 ViT-S/16 이다 —
다른 크기는 CLI 를 직접 고쳐 `--policy.dino_model=<timm 이름>` 을 붙인다(화면 필드는 숫자·스위치만 있다):

| `dino_model` | 파라미터 | 보폭 | 비고 |
|---|---|---|---|
| `vit_small_patch16_dinov3.lvd1689m` | 22M | 16 | 기본 — ResNet18(11M) 다음 크기 |
| `vit_small_plus_patch16_dinov3.lvd1689m` | 29M | 16 | |
| `vit_base_patch16_dinov3.lvd1689m` | 86M | 16 | |
| `convnext_tiny.dinov3_lvd1689m` | 28M | 32 | ResNet 처럼 합성곱 — 원본 크기 그대로 |

## 5. 실측 (RTX 5090, `wego-mink/pet_1_torque` 25에피소드·카메라 2대 480×640, 배치 8, 60스텝)

| | ACT (ResNet18) | ACT-DINOv3 (ViT-S/16) |
|---|---|---|
| 학습 파라미터 | 52M | 62M |
| 스텝 시간(updt_s) | 0.049 s | 0.052 s |
| 손실 10 → 60스텝 | 36.7 → 5.14 | 38.4 → 5.13 |
| 추론 한 청크 | — | 8.1 ms |

비용은 거의 같다. **성능은 아직 모른다** — 60스텝은 배선 확인일 뿐이다. 남은 일: 같은 데이터셋·같은 스텝으로 둘을
학습해 실기 성공률을 비교한다(조명·배경이 바뀐 조건을 포함 — DINOv3 를 고른 이유가 그쪽 강건성이다).

## 6. 안 한 것

- 엔코더 프로브 tap — 프로브는 ACT 의 ResNet 특징맵을 직접 다시 만든다(`wrapper/encoder_probe.py`). 스펙에서 꺼 두었다
- ACT-S(stage 헤드)와의 조합 — 필요하면 `ActSPolicy` 에 같은 백본 교체를 얹으면 된다
- 화면에서 DINOv3 크기 고르기 — 스펙 필드에 선택형(kind)이 없다. 지금은 CLI 직접 수정(로컬 학습만)
- DINOv3 밖의 백본(DINOv2·SigLIP 등) — 이름 제한과 정규화 변환만 풀면 된다. 후보와 고를 때 걸리는 것은
  [act-variants.md §4](act-variants.md)
