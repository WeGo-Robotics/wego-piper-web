# ACT 변형 — 어디를 바꿀 수 있고, 무엇부터 할까 (조사)

상태: **조사·후보 정리** (2026-10-27). 구현이 아니라 다음 변형을 고르기 위한 지도다. 구현된 변형은 셋 —
[ACT-S](act-s.md)(stage 헤드) · [ACT-delta](act-delta.md)(액션 표현) · [ACT-DINOv3](act-dinov3.md)(비전 백본).
이 문서의 크기·정규화·스트라이드는 이 기계의 timm 1.0.30 과 LeRobot 소스에서 직접 확인한 값이다.

## 1. ACT 에서 바꿀 수 있는 자리

ACT 는 카메라 영상 → 백본 특징맵 → 트랜스포머(인코더·디코더) → 액션 청크(L1 + CVAE)이고, 추론 때 청크를
시간 앙상블로 잇는다. 바꿀 자리는 넷이다.

| 자리 | 지금 있는 것 |
|---|---|
| 입력 — 무엇을 보는가 | 백본 교체 (ACT-DINOv3) |
| 출력 — 무엇을 내는가 | 관절 변화량 (ACT-delta) |
| 디코더·학습 목표 — 어떻게 내는가 | 보조 stage 헤드 (ACT-S) |
| 추론 — 청크를 어떻게 잇는가 | 시간 앙상블 (`temporal_ensemble`) |

## 2. 변형 후보

| 변형 | 바꾸는 곳 | 노리는 것 | 여기서 드는 일 |
|---|---|---|---|
| **힘(토크) 입력** (Bi-ACT 계열) | 상태 입력 | 볼트 끼우기 같은 접촉 작업 | **정책은 그대로다** — ACT 는 `observation.state` 차원을 가리지 않는다. robotd 는 관절 effort 를 이미 읽지만([arm.py](../robot/piper_robot/arm.py) 진단용) shm·데이터셋엔 안 나간다(`wego-mink/pet_1_torque` 도 관절 위치 7개뿐). 수집 경로만 고치면 된다 |
| **깊이(RGB-D)** | 비전 입력 | 높이·거리 판단 | D405 깊이를 데이터셋에 녹화해야 한다(지금은 [배경 마스킹](depth-background-mask.md)에만 쓴다). 백본 첫 층 4채널화는 ACT-DINOv3 와 같은 교체 방식 |
| **다른 백본** | 비전 입력 | 백본 비교 | 가장 싸다 — §4 |
| **작업 문장 조건** (MT-ACT) | 입력 (FiLM) | 모델 하나로 여러 작업 | 중간. 다만 이 자리는 SmolVLA 가 이미 있다 |
| **검출 결과를 토큰으로** | 입력 | 물체 위치가 바뀌어도 동작 | 크다 — 추론 루프마다 검출기를 돌려야 해서 지연·고장점이 는다 |
| **끝단 자세(EE) 액션** | 출력 | 작업대 위치가 달라져도 동작, 다른 팔과 데이터 공유 | 크다 — 지령 경로가 관절 기준이라 추론 경로에 IK 가 들어간다 |
| **흐름정합(flow-matching) 헤드** | 디코더 | ACT 의 대표 약점인 "두 방법 사이 평균"(멀티모달). 그리고 RTC 가 flow-matching 전용이라 ACT 에서도 쓸 수 있게 된다 | 중간 이상 — 디코더 출력부 교체. 추론이 여러 스텝이라 지금 청크당 8ms 의 몇 배 |
| **진행도·실패 헤드** | 보조 헤드 | 에피소드가 끝났는지·실패했는지 판정([오케스트레이터](episode-orchestrator.md)가 쓸 신호) | 작다 — ACT-S 와 같은 틀. 진행도 라벨은 프레임 위치로 자동, 실패는 라벨이 따로 필요 |
| **BID** (Bidirectional Decoding) | 추론만 | 시간 앙상블의 대안. 청크를 여러 개 뽑아 직전 청크와 가장 잘 이어지는 것을 고른다 | 재학습 없음. 다만 ACT 는 추론 때 잠재 z 를 0 으로 두고, CVAE 가 잠재를 잘 안 써서 뽑힌 청크끼리 차이가 작을 수 있다 — 흐름정합 헤드와 짝이 맞다 |
| **InterACT** | 양팔 주의 구조 | 두 팔 사이 협조 | [양팔](bimanual.md) 실기 검증이 하드웨어 대기라 그 뒤 |

## 3. 증상으로 고르기

변형은 실기에서 **무엇이 실패하는지**로 고른다.

| 실기 증상 | 변형 |
|---|---|
| 두 가지 방법 사이에서 머뭇거리거나 그 중간으로 간다 | 흐름정합 헤드 |
| 접촉 작업(끼우기·밀기)에서 실패한다 | 힘 입력 — 정책 무수정, 수집만 |
| 작업대·물체 위치가 바뀌면 실패한다 | EE 액션 또는 검출 토큰 |
| 조명·배경이 바뀌면 실패한다 | DINOv3 백본, 깊이 배경 마스킹 — 둘 다 이미 있다 |
| 작업 여러 개를 모델 하나로 | 작업 문장 조건 — 단 SmolVLA 가 이미 그 자리 |
| 끝났는지·실패했는지를 사람이 봐야 한다 | 진행도·실패 헤드 |

## 4. 백본

### 4.1 계열

갈리는 축은 둘이다 — **무엇으로 사전학습했나**(분류 라벨 · 이미지 자체 · 문장 짝 · 픽셀 복원)와 **합성곱이냐 ViT 냐**.

| 계열 | 대표 (크기) | 사전학습 | 특징 |
|---|---|---|---|
| **ResNet** | resnet18 11M · resnet34 · resnet50 25M | ImageNet 분류 | ACT 기본. 가볍고 검증됐다. 질감·모양 같은 국소 특징 위주 |
| **경량 합성곱** (EfficientNet, MobileNetV4) | efficientnet_b0 4M | ImageNet 분류 | RT-1 이 EfficientNet(+FiLM). 추론 기계가 약할 때만 의미 — 5090 에선 이득 없음 |
| **ConvNeXt** (V2, DINOv3판) | convnext_tiny 28M | 분류 / DINOv3 증류 | 현대화한 합성곱. DINOv3판은 지금 `dino_model` 로 고를 수 있다 |
| **DINOv2 / DINOv3** | ViT-S 22M | 라벨 없는 자기지도 | 물체 경계·부위·위치 같은 공간 정보가 특징맵에 잘 남는다. 조명·배경 강건성에서 상위 — 저절로 생기는 분할 능력이 분포 밖 성능을 예측한다(Burns 2023). v3 가 v2 보다 조밀한 특징이 좋다. v2 는 패치 14 |
| **CLIP / SigLIP** | ViT-B 86–93M (small 없음) | 이미지–문장 대조 | "무엇인가"(의미)에 강하고 문장과 같은 공간이라 언어 조건과 짝. 정확한 위치 같은 공간 세부는 약하다. SmolVLA 백본이 SigLIP |
| **MAE 계열** (VC-1, EVA02, ConvNeXt V2 FCMAE) | 다양 | 가린 픽셀 복원 | 고정해서 쓰면 약하고, 같이 학습해야 좋아진다 |
| **로봇 전용·증류** (R3M, VC-1, Theia, RADIO) | 다양 | 1인칭 영상 / 여러 모델 증류 | timm 에 없어 로더를 따로 붙여야 한다. ImageNet 대비 일관된 우위가 없다는 결과가 있다(Dasari 2023) |
| **융합** (DINOv2 + SigLIP) | 둘의 합 | 위 둘 | OpenVLA 방식 — 공간과 의미를 함께. 비용 두 배 |

### 4.2 고를 때 걸리는 것

- **정규화.** LeRobot 은 카메라를 ImageNet 평균·표준편차로 정규화한다(`use_imagenet_stats`). ResNet·DINO·ConvNeXt·
  EfficientNet 은 이 값이 맞다. CLIP 은 값이 조금 다르고(0.481, 0.458, 0.408) SigLIP 은 0.5/0.5 라 **백본 안에서
  바꿔 넣어야** 한다 — 안 바꾸면 사전학습이 본 적 없는 분포가 들어간다
- **데이터 양.** 에피소드 25개 수준에서 큰 백본을 같이 학습하면 과적합 위험. 고정하면 MAE·CLIP 계열은 약해지고
  DINO 는 비교적 버틴다 (`freeze_backbone` 은 ACT-DINOv3 에 이미 있다)
- **크기.** SigLIP·CLIP 은 base(약 90M)가 최소 — 백본만 ResNet18 의 8배다
- **패치 크기.** ACT-DINOv3 의 백본은 토큰 격자를 먼저 정하고 그 격자 × 네트워크 보폭으로 리사이즈한다. 그래서
  패치 14(DINOv2)도 구조 변경 없이 들어간다

### 4.3 지금 되는 것

| 백본 | 방법 |
|---|---|
| ResNet-34/50 | **바닐라 ACT 설정만으로** — ACT 가 torchvision ResNet 이름을 그대로 받는다(`ACTConfig.__post_init__` 는 `resnet` 접두만 본다, 투영 conv 는 `fc.in_features` 를 따른다). 화면 필드는 숫자·스위치뿐이라 CLI 를 직접 고친다: `--policy.vision_backbone=resnet50 --policy.pretrained_backbone_weights=ResNet50_Weights.IMAGENET1K_V2` |
| DINOv3 ViT-S/S+/B, ConvNeXt | ACT-DINOv3 (`--policy.dino_model=...`, [act-dinov3.md §4](act-dinov3.md)) |
| DINOv2, ConvNeXt V2, EfficientNet, CLIP, SigLIP | **아직 안 된다** — 같은 교체 구조로 돌지만 `ActDinov3Config` 가 `dinov3` 가 든 이름만 받는다. 이 제한을 풀고 정규화 변환(§4.2)을 넣는 것이 작은 수정 |

## 5. 권하는 순서

1. **ACT ↔ ACT-DINOv3 실기 A/B 먼저.** 변형이 쌓일수록 무엇이 효과였는지 가르기 어렵다. 조명·배경이 바뀐 조건을
   넣는다 — DINOv3 를 고른 이유가 그쪽이다
2. **백본 비교는 셋이면 충분하다** — ResNet18(기본) · ResNet-50(설정만, 비용 0) · DINOv3-S. "백본을 키운 효과"와
   "사전학습 방식을 바꾼 효과"가 갈린다. SigLIP 은 작업 문장 조건을 붙일 때나 의미가 있고 지금은 비싸기만 하다
3. **구조 변형 하나를 고른다면 흐름정합 헤드.** ACT 의 가장 알려진 약점을 다루고, 지금 SmolVLA 만 쓰는 RTC 를
   ACT 무리로 넓힌다. 백본과 직교라 나중에 DINOv3 와 합칠 수 있다
4. **접촉 작업이 문제면 힘 입력이 가장 싸다** — 정책 무수정, 수집 경로만

## 6. 참고

- Bi-ACT — Bilateral Control-Based Imitation Learning via Action Chunking with Transformer (2024)
- MT-ACT — RoboAgent: Generalization and Efficiency in Robot Manipulation via Semantic Augmentations and Action Chunking (2023)
- InterACT — Inter-dependency Aware Action Chunking with Hierarchical Attention Transformers for Bimanual Manipulation (2024)
- BID — Bidirectional Decoding: Improving Action Chunking via Closed-Loop Resampling (2024)
- Burns et al. — What Makes Pre-Trained Visual Representations Successful for Robust Manipulation? (2023)
- Dasari et al. — An Unbiased Look at Datasets for Visuo-Motor Pre-Training (2023)
- OpenVLA — An Open-Source Vision-Language-Action Model (2024) — DINOv2 + SigLIP 융합 백본
- RT-1 — Robotics Transformer for Real-World Control at Scale (2022) — EfficientNet + FiLM
