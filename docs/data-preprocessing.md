# 로봇 학습 데이터 전처리 파이프라인

> **대상**: LeRobot 기반 모방학습(Imitation Learning) 웹 제어 시스템 (wego-piper-web)
> **목적**: 데이터 수집·추론 시 관측(observation)과 액션(action)이 어떤 전처리를 거치는지, 그리고 각 단계를 **누가**(프로젝트 코드 / LeRobot / 모델) 담당하는지 이해한다.

---

## 0. 한눈에 보기 — 두 개의 독립 경로

전처리는 서로 완전히 분리된 **두 개의 subprocess 경로**로 나뉜다.

```
┌─────────────────────────────────────────────────────────────┐
│  FastAPI 백엔드 (backend/)  ──subprocess.Popen──▶  두 경로     │
└─────────────────────────────────────────────────────────────┘
        │                                    │
        ▼ 수집(Collection)                    ▼ 추론(Inference)
  wrapper/start_record.py            wrapper/lerobot_wrapper.py
        │                                    │
   LeRobot lerobot-record            정책(policy) 직접 import
        │                                    │
  원본 그대로 디스크 저장             전처리→모델→후처리→로봇 전송
  (리사이즈·정규화 없음)              (정규화·리사이즈·필터 전부 여기)
```

**핵심 원칙**
- **수집 = 원본 보존**: 카메라 원본 해상도의 uint8 RGB를 그대로 저장. 리사이즈·크롭·정규화 **안 함**.
- **추론/학습 = 변환**: 모든 정규화·리사이즈·크롭은 추론(또는 학습) 시점에 파이프라인·모델 내부에서 수행.
- 이렇게 하면 **데이터셋은 전처리에 독립적** → 나중에 모델·정규화 방식을 바꿔도 재수집 불필요.

> 💡 **왜 원본을 저장하나?** 전처리를 데이터에 "구워 넣으면"(bake) 되돌릴 수 없다. 정규화 통계나 입력 해상도를 바꾸려면 데이터를 다시 모아야 한다. 원본 저장 + 지연 변환(lazy transform)이 재현성과 유연성을 준다.

---

## 1. 추론 경로 전체 흐름

```
카메라/로봇 관측
   │
   ▼  ① 카메라 캡처 전처리        [LeRobot + 프로젝트]
   ▼  ② 관측 변환 (키매핑/트림/스케일)  [프로젝트]
   ▼  ③ LeRobot 프리프로세서 파이프라인  [LeRobot]
   ▼      (rename → batch → "\n" → 토큰화 → device → 정규화)
   ▼  ④ 모델 내부 전처리          [모델]
   ▼      (SmolVLA: resize-pad 512 → [-1,1] → 패딩)
 ┌────────────────┐
 │  정책 추론      │  predict_action_chunk()
 └────────────────┘
   ▼  ⑤ 액션 청크 슬라이스        [프로젝트]
   ▼  ⑥ LeRobot 포스트프로세서 (역정규화)  [LeRobot]
   ▼  ⑦ ActionFilter 체인         [프로젝트]  ★ 이 문서의 핵심
   ▼      (속도→보간→저크→저역통과)
로봇으로 send_action()
```

범례: **[프로젝트]** = 이 저장소 자체 코드 · **[LeRobot]** = LeRobot 라이브러리 · **[모델]** = 정책 신경망 내부

---

## 2. 단계별 상세 — 관측(Observation) 전처리

### ① 카메라 캡처 (추론·수집 공통)

| 전처리 | 담당 | 위치 | 설명 |
|---|---|---|---|
| **BGR→RGB** 색변환 + 회전 | LeRobot | `camera_opencv.py:388-425` | OpenCV는 BGR로 읽으므로 RGB로 변환. 학습·추론 색공간 일치 필수 |
| 캡처 해상도/FPS 설정 | LeRobot | `camera_opencv.py:184-280` | `cv2.VideoCapture` 속성. 프론트가 넘긴 카메라 dict 기준 |
| **V4L2 백엔드 강제** | 프로젝트 | `lerobot_wrapper.py:241` | `backend=200`. GStreamer 폴백 방지(불안정) — **추론만** |
| RealSense color(+depth) | LeRobot | `camera_realsense.py` | color 스트림, 옵션 depth(z16) |

> ⚠️ **주의**: 백엔드의 `realsense_manager.py`(depth colorize), `camera_manager.py`(JPEG 미리보기)는 **UI 프리뷰 전용**이다. record/inference 시작 전 release되므로 모델에 들어가는 데이터에는 영향 없음.

> 💡 **왜 색공간이 중요한가?** 모델은 학습 때 본 색 채널 순서를 그대로 기대한다. 추론에서 BGR을 넣으면 빨강↔파랑이 뒤바뀐 이미지가 되어 성능이 급락한다. "학습과 추론의 전처리는 반드시 동일" 원칙의 대표 사례.

### ② 관측 변환 — 프로젝트 코드

`_prepare_observation()` — [`lerobot_wrapper.py:413-486`](../wrapper/lerobot_wrapper.py)

1. **`build_dataset_frame()`** — raw 관측 키 → `observation.state` / `observation.images.<cam>` 로 매핑 (LeRobot 유틸, 리사이즈 없음)
2. **rename_map 적용** — 카메라 키 재매핑(`side`→`camera1` 등). 저장된 `policy_preprocessor.json`에서 추출 (`:324-337`)
3. **state 차원 트림** — 학습 시 그리퍼 dim을 뺐다면 관측 state도 모델 기대 길이로 잘라냄 (`:429-435`)
4. **이미지 텐서화** — `torch.tensor(val).permute(2,0,1).float()/255.0`
   - HWC → **CHW** (채널 우선), uint8 → float32, **[0,1] 스케일** (`:443`)
5. **task 텍스트 주입** — `result["task"] = _current_task` (`:449`)

> 💡 **왜 /255 로 나누나?** 이미지는 0~255 정수. 신경망은 작은 실수 입력에서 학습이 안정적이라 [0,1]로 정규화한다. (SmolVLA는 이후 모델 내부에서 다시 [-1,1]로 재스케일 — SigLIP 인코더 요구사항.)

> 💡 **왜 state를 트림하나?** 예: 마스터-슬레이브 팔에서 그리퍼를 학습에서 제외했다면 모델의 state 차원은 6인데 실측은 7이 온다. 차원이 안 맞으면 추론 자체가 실패하므로 앞쪽 기대 길이만큼 잘라 맞춘다.

### ③ LeRobot 프리프로세서 파이프라인 — LeRobot 코드

`make_pre_post_processors(policy_cfg, pretrained_path=...)` — [`lerobot_wrapper.py:313-319`](../wrapper/lerobot_wrapper.py)
저장된 `policy_preprocessor.json`을 그대로 로드해 재현. SmolVLA 기준 순서:

| 순서 | 스텝 | 하는 일 |
|---|---|---|
| 1 | RenameObservations | 카메라명 재매핑 (저장된 rename_map) |
| 2 | AddBatchDimension | 배치 차원 추가 `(C,H,W)→(1,C,H,W)` |
| 3 | SmolVLANewLine | task 문자열 끝에 `"\n"` 추가 |
| 4 | **Tokenizer** | task 토크나이즈 → `language.tokens` + `attention_mask` (`max_length`, truncation, right-padding) |
| 5 | Device | 텐서를 CUDA로 이동 |
| 6 | **Normalizer** | 데이터셋 통계로 입력 정규화 |

**정규화 매핑 (정책별로 다름 — 중요!)**

| 정책 | STATE | ACTION | VISUAL(이미지) |
|---|---|---|---|
| **SmolVLA** | MEAN_STD | MEAN_STD | **IDENTITY (정규화 안 함)** |
| **ACT** | MEAN_STD | MEAN_STD | **MEAN_STD (이미지도 정규화)** |
| **Diffusion** | MIN_MAX 계열 | MIN_MAX 계열 | (모델 내부 처리) |

- **MEAN_STD**: `(x − mean) / (std + 1e-8)` — 평균 0, 표준편차 1로
- **MIN_MAX**: `2·(x − min)/(max − min) − 1` — [-1, 1] 범위로

> 💡 **왜 정규화하나?** 관절 각도(수백 deg), 그리퍼(%), 카메라 픽셀은 스케일이 제각각이다. 정규화 없이 넣으면 큰 값이 손실을 지배해 학습이 편향된다. 데이터셋의 mean/std로 모든 입력을 비슷한 스케일로 맞춘다.

> ⚠️ **SmolVLA는 이미지를 파이프라인에서 정규화하지 않는다(IDENTITY).** 대신 모델 내부(④)에서 자체 처리한다. ACT/Diffusion과 헷갈리기 쉬운 부분.

**폴백 경로** — 프리프로세서가 없는 구형 모델일 때 프로젝트가 직접 수동 처리 ([`:454-486`](../wrapper/lerobot_wrapper.py)): state unsqueeze + device 이동, 이미지 permute+`/255`+batch, **수동 토크나이징**(`padding="max_length"`, `max_length=48`, task별 캐시).

### ④ 모델 내부 전처리 — 모델 코드

**SmolVLA** (`modeling_smolvla.py`)
- **`resize_with_pad` → 512×512** — 종횡비 유지 bilinear 리사이즈 + 좌/상단 **zero-pad** (`resize_imgs_with_padding=(512,512)`)
- **[0,1] → [-1,1]** — `img*2−1` (SigLIP 비전 인코더 요구)
- 없는 카메라는 **-1 패딩 이미지**로 채움 (`empty_cameras`)
- **state/action → 32차원 패딩** (`max_state_dim`/`max_action_dim=32`), 예측 후 action 언패딩
- Flow-matching 디코드 `num_steps=10`, 옵션 **RTC**(Real-Time Chunking)

**ACT** (`modeling_act.py`): 내부 리사이즈 없음(사전 리사이즈 기대), VAE 잠재 + ResNet 백본, 위치 인덱스 [0, 2π] 정규화

**Diffusion** (`modeling_diffusion.py`): **Resize** + **CenterCrop**(eval) / RandomCrop(train)

> 💡 **왜 패딩 리사이즈(resize-with-pad)인가?** 단순 리사이즈는 종횡비를 왜곡한다. 종횡비를 유지하며 512에 맞추고 남는 부분을 0으로 채우면 물체 형상이 안 찌그러진다.

> 💡 **왜 32차원 패딩?** SmolVLA는 다양한 로봇(팔 6축, 7축, 양팔 등)을 한 모델로 다루려고 state/action을 고정 크기 32로 패딩한다. 실제 로봇 차원은 그 일부만 쓰고 나머지는 0.

---

## 3. 단계별 상세 — 액션(Action) 후처리

### ⑤ 청크 슬라이스 — 프로젝트

[`lerobot_wrapper.py:551-553`](../wrapper/lerobot_wrapper.py). 모델이 한 번에 여러 스텝(청크)을 예측하면, 앞 `_actions_per_chunk` 스텝만 사용(0=전체). 모델 실제 청크 길이로 클램핑.

### ⑥ 역정규화 — LeRobot

[`:559-560`](../wrapper/lerobot_wrapper.py). 포스트프로세서가 **역정규화**(`x·std + mean`) 후 CPU 이동. 정규화된 모델 출력을 실제 관절 단위(deg 등)로 되돌린다. 이후 numpy → 모터명 dict로 변환.

> 💡 **왜 역정규화?** 모델은 정규화된 공간에서 액션을 출력한다(평균0/std1). 로봇은 실제 각도를 원하므로 정규화의 역연산으로 물리 단위를 복원해야 한다.

### ⑦ ActionFilter 체인 — 프로젝트 ★

다음 장에서 상세히 다룬다.

---

## 4. ActionFilter 심층 분석 ★

> **위치**: [`wrapper/action_filter.py`](../wrapper/action_filter.py) (전체 122줄) · 호출: [`lerobot_wrapper.py:656-661`](../wrapper/lerobot_wrapper.py)
> **성격**: **전적으로 프로젝트 자체 코드**. LeRobot에는 없다. (gRPC 경로에선 LeRobot 0.5에 filter가 없어 pass-through)
> **목적**: 모델이 뱉은 원시 액션을 **로봇이 부드럽고 안전하게** 실행하도록 다듬는 마지막 안전장치.

### 4.1 적용 위치와 순서

매 제어 스텝, `robot.send_action()` **직전**에 한 번 적용:

```python
current_state = {k: v for k, v in obs.items() if isinstance(v, (int, float))}
filtered = _action_filter.apply(action_dict, current_state)
robot.send_action(filtered)
```

- **입력**: 역정규화된 `{모터명: 값}` dict + 현재 관절 실측값(`current_state`)
- **불변성**: 원본을 수정하지 않고 새 dict 반환 (`result = dict(action_dict)`)
- **고정 체인 순서**:

```
목표 액션
  │
  ▼ ① 속도 제한   (기준: 현재 실측값 current_state)
  ▼ ② 보간        (기준: 이전 전송값 _prev_sent)
  ▼ ③ Jerk 제한   (기준: 이전 전송값 + 이전 속도)
  ▼ ④ 저역통과    (기준: 이전 전송값)
  │
  ▼ _prev_sent에 저장 → 다음 스텝의 기준
로봇 전송
```

> ⚠️ **①만 현재 실측값 기준**, ②③④는 **이전 전송값(`_prev_sent`) 기반 상태 필터**다. 그래서 첫 스텝엔 ②③④가 사실상 pass-through(이전값 없음).

### 4.2 필터별 상세

#### ① 속도 제한 (`max_velocity` / `max_gripper_velocity`)

[`action_filter.py:63-77`](../wrapper/action_filter.py)

```python
joint_max_delta = max_velocity / fps        # 한 스텝 최대 이동량
grip_max_delta  = max_gripper_velocity / fps
delta = 목표 − 현재실측
if |delta| > max_delta:
    result = 현재실측 + max_delta · sign(delta)   # 클램프
```

- 관절/그리퍼 구분: 키에 `"gripper"` 포함 여부
- **기준이 "현재 로봇 실측값"**이라는 게 핵심 → 큐가 뒤처져 목표가 멀리 튀어도 로봇은 한 스텝에 정해진 양만 이동
- `current_state` 없으면 skip

> 💡 **왜 실측값 기준?** 만약 명령값 기준으로만 제한하면, 명령이 실제 위치보다 앞서가 있을 때(추종 지연) 실제 로봇이 갑자기 큰 폭으로 따라잡으며 튈 수 있다. 실측 기준이면 "지금 위치에서 이만큼만" 이라 물리적 급발진을 직접 막는다.

#### ② 보간 (`interpolation_steps`, 0=OFF, 최대 10)

[`action_filter.py:79-95`](../wrapper/action_filter.py)

```python
t = (progress + 1) / steps
t_smooth = t·t·(3 − 2t)      # smoothstep (에르미트)
result = from + (to − from) · t_smooth
```

- 새 목표 → `_interp_from`(직전 전송) ~ `_interp_to`(새 목표) 구간 설정
- 매 스텝 **smoothstep** 곡선으로 완만히 도달 (시작·끝 기울기 0 → 부드러움)
- 보간 중이면 새 구간으로 리셋하지 않고 진행만 계속

> 💡 **왜 선형이 아니라 smoothstep?** 선형 보간은 구간 경계에서 속도가 급변(꺾임)한다. `t²(3−2t)`는 양 끝에서 속도가 0이라 가감속이 매끄럽다.

#### ③ Jerk 제한 (`max_jerk` deg/s², 0=OFF)

[`action_filter.py:97-110`](../wrapper/action_filter.py)

```python
velocity = (목표 − 이전전송) / dt
accel    = (velocity − 이전속도) / dt
if |accel| > max_jerk:
    velocity = 이전속도 + clamp(accel)·dt
    result   = 이전전송 + velocity·dt
```

- 가속도의 **변화율(=저크)** 을 제한 → 모터 덜컹임(진동) 억제
- `_prev_velocity`를 매 스텝 갱신

> 💡 **저크(jerk)란?** 가속도의 미분. 저크가 크면 힘이 급변해 기구부에 충격·진동이 생긴다. 로봇/차량 승차감 제어에서 흔히 제한하는 양.

#### ④ 저역통과 EMA (`lowpass_alpha`, 1.0=OFF)

[`action_filter.py:112-118`](../wrapper/action_filter.py)

```python
result = alpha · 목표 + (1 − alpha) · 이전전송
```

- 지수이동평균(EMA). alpha 낮을수록 강한 스무딩(느리고 부드러움)
- 하한 0.05로 클램프(완전 정지 방지), 1.0이면 그대로 통과

> 💡 **왜 저역통과?** 모델 출력에 섞인 고주파 노이즈(스텝마다 미세 떨림)를 걸러 부드럽게 만든다. 단, EMA는 **1차 지연(first-order lag)** 을 만든다 — 목표를 **점근적으로만** 따라가고(마지막 몇 %가 가장 느린 기하급수 꼬리), 움직이는 목표는 `(1−α)/α` 스텝만큼 **뒤처진다**. α가 낮을수록 지연↑. → 다음 4.4의 undershoot 주의 참고.

### 4.3 파라미터 요약표

전 파라미터는 **ZMQ로 실시간 변경 가능** ([`update_param`](../wrapper/action_filter.py), `lerobot_wrapper.py:162`). FPS는 시작 시 `args.fps`로 세팅.

| 파라미터 | 기본값 | 범위(클램프) | 0 / 1 의미 | 효과 |
|---|---|---|---|---|
| `lowpass_alpha` | 0.5 | 0.05 – 1.0 | 1.0 = OFF | 낮을수록 부드럽지만 느림 |
| `max_velocity` | 180 deg/s | 0 – 1000 | 0 = 무제한 | 관절 급발진 방지 |
| `max_gripper_velocity` | 300 %/s | 0 – 500 | 0 = 무제한 | 그리퍼 급발진 방지 |
| `max_jerk` | 0 | 0 – 5000 | 0 = OFF | 진동/덜컹임 억제 |
| `interpolation_steps` | 0 | 0 – 10 | 0 = OFF | 스텝 간 궤적 부드럽게 |
| `fps` | 20 | 1 – 60 | — | 제어 주파수(모든 환산의 분모) |

### 4.4 상호작용 & 주의점

- **순서가 결과를 바꾼다**: 속도 제한(실측 기준) → 보간·저크·저역통과(이전 전송 기준). 여러 개 동시에 켜면 효과가 **누적**되어 응답이 크게 느려질 수 있다(특히 보간+저역통과).
- **첫 스텝**: ②③④는 이전 전송값이 없어 pass-through.
- `use_chunk_size`는 `update_param`으로 받지만 `apply` 안에서는 미사용 — 청크 길이 제어는 큐 로직 담당(필터 본체와 무관).
- **추론 경로 전용** — 수집(record) 경로엔 없음.

#### ⚠️ 저역통과의 함정 — 엔드 이펙터가 목표에 못 미침(undershoot)

**속도·저크·보간** 필터는 *멈춰 있는 목표*엔 (느려도) **정확히 도달**하지만, **저역통과(EMA)** 는 **점근적으로만** 도달한다. 그래서 저역통과를 켜면 궤적은 부드러워져도 엔드 이펙터가 임무 위치까지 못 가는 경우가 생긴다.

| 필터 | 멈춘 목표에 정확히 도달? | 이유 |
|---|---|---|
| 속도 제한 | ✅ (느릴 뿐) | **실측 기준** delta 클램프 → 접근 후 정확히 도달, 누적 오프셋 없음 |
| 저크 제한 | ✅ | 가속도 변화율만 제한 |
| 보간 | ✅ (윈도 끝에서) | 단, 목표가 윈도보다 자주 갱신되면 지연 |
| **저역통과** | ⚠️ **점근적**(무한시간 극한) | 유한 시간엔 항상 조금 못 미침 → **undershoot** |

**왜 저역통과만 못 미치나** (3중 원인):
1. **정밀 구간 = 느린 꼬리**: 오차가 매 스텝 `(1−α)`배로 줄어드는 기하급수 꼬리라, 파지처럼 마지막 수 mm가 중요한 구간이 가장 느리다. 에피소드가 고정 horizon에서 끝나거나 정책이 목표 근처에서 델타를 줄이면(감속 진입) 명령이 **목표에 덜 미친 채 수렴**한다.
2. **다관절 코너 깎기(corner-cutting)**: 관절별 1차 지연이라 빠른 다관절 이동에서 경로가 안쪽으로 깎여 실제 엔드 이펙터 도달점이 목표에서 밀린다.
3. **필터 누적**: 속도/보간까지 겹치면 정착 시간이 곱으로 늘어 유한 시간 안에 더 못 미친다.

> ✅ **처방**: 정밀 도달이 중요하면 `lowpass_alpha`를 **1.0(OFF)** 쪽으로 올리고, 떨림 억제는 **속도/저크 제한으로 대체**한다. 저역통과는 "노이즈/채터가 문제이고 절대 위치 정밀도는 덜 중요할 때"에 한정해 사용.

---

## 5. 수집(데이터 녹화) 경로

거의 **stock LeRobot** (`record_loop`, `datasets/lerobot_dataset.py`).

| 단계 | 담당 | 내용 |
|---|---|---|
| observation/teleop 프로세서 | LeRobot | **Identity** — 수집 중 정규화·리사이즈 **없음** |
| `build_dataset_frame` | LeRobot | 키 매핑만. state는 float32, 이미지는 그대로 |
| 액션 로깅 | LeRobot | `max_relative_target` 클리핑 후 **실제 전송된 값**을 기록 |
| 이미지 저장 | LeRobot | float면 [0,1]→×255→uint8, **PNG 저장(원본 해상도, 리사이즈 없음)** |
| 타임스탬프 | LeRobot | `timestamp = frame_index / fps` |
| 비디오 인코딩 | LeRobot | 기본 `libsvtav1` (h264/hevc/nvenc 선택 가능) |

**프로젝트가 추가한 수집 코드** ([`start_record.py`](../wrapper/start_record.py)) — 전부 **프리뷰/제어 전용, 저장 안 됨**:
- **프리뷰 탭**: long side **320px 다운스케일**, RGB→BGR, **JPEG 품질 70**, ~10fps ZMQ 전송 (record 루프 부하 최소화)
- **헤드리스 제어 탭**: 키보드 리스너를 ZMQ 명령(skip/rerecord/stop)으로 교체

> ⚠️ 프리뷰의 320px·JPEG70은 **화면 표시용**일 뿐, 학습 데이터셋에는 원본 그대로 들어간다. 헷갈리지 말 것.

---

## 6. "누가 만들었나" 정리 (핵심 슬라이드)

### 🟦 프로젝트(이 저장소)가 직접 넣은 전처리
- 카메라 **V4L2 백엔드 강제**
- 관측 **state 차원 트림** / rename_map 추출·적용
- **수동 토크나이징 폴백**
- **ActionFilter 전체** (속도·보간·저크·저역통과) ★
- **RTC·청크 큐 관리**, ParkingController(종료 시 홈 복귀)
- 수집 **프리뷰 다운스케일 탭**

### 🟩 LeRobot 라이브러리가 하는 전처리
- BGR→RGB·회전, 캡처 해상도
- 키 매핑, 토크나이즈, task에 `"\n"` 추가
- **정규화 / 역정규화** (MEAN_STD·MIN_MAX, dataset 통계)
- **PNG 저장·비디오 인코딩·타임스탬프**

### 🟨 모델(정책) 내부 전처리
- SmolVLA: **resize-pad 512×512**, **[-1,1] SigLIP 스케일**, state/action **32 패딩**, flow-matching, RTC
- ACT: VAE, 위치 인덱스 정규화
- Diffusion: resize + center/random crop

---

## 7. 반드시 기억할 3가지

1. **수집은 원본, 변환은 추론/학습 시점** — 데이터셋은 native 해상도 uint8 RGB. 리사이즈·정규화는 전부 나중에.
2. **정규화 방식은 정책마다 다르다** — SmolVLA는 이미지를 파이프라인에서 정규화 안 함(모델 내부 [-1,1]). ACT/Diffusion은 dataset mean/std로 이미지 정규화.
3. **학습 전처리 = 추론 전처리** — 색공간·해상도·정규화 통계가 어긋나면 성능이 급락한다. LeRobot이 `policy_preprocessor.json`으로 이 일치를 보장한다.

---

## 8. 고급 — 제약 스무딩 (must-hit 포인트를 지키는 스무딩)

> **동기**: 4.4에서 봤듯 저역통과는 궤적을 부드럽게 하지만 엔드 이펙터가 목표에 **못 미친다(undershoot)**. "꼭 도달해야 하는 지점(파지·놓기·정렬)은 정확히 통과시키면서 나머지만 부드럽게" 하는 방법이 **제약 스무딩(constrained / shape-preserving smoothing)** 이다.

문제를 둘로 나눈다: **(1) 어떤 점이 must-hit인지 인지 → (2) 그 점을 하드 제약으로 두고 스무딩.**

### 8.1 must-hit 포인트 인지 (재학습 불필요)

모델은 "여기가 키포인트"라는 메타데이터를 주지 않지만, **청크 안에서** 추론할 수 있다.

| 신호 | 의미 | 강도 |
|---|---|---|
| **그리퍼 open↔close 전이** | 그 순간의 팔 자세 = **파지/놓기 지점**. 반드시 도달 | ★★★ pick-place 최고 신호 |
| **속도 최소점 / dwell** | 청크 내 `|Δq|` 국소 최소 = 접촉·정밀 정렬 지점(감속해서 멈추는 곳) | ★★ |
| **청크 경계** | 재추론 직전 마지막 액션을 앵커로 | ★ |

### 8.2 제약을 걸고 스무딩하는 방법

| 방법 | must-hit 보장 | 설명 | 적합도 |
|---|---|---|---|
| **A. 앵커 스냅 / 필터 리셋** | ✅ | 키포인트에선 EMA를 걸지 않고 값 그대로 통과(또는 필터 상태 리셋로 지연 누적 차단). 오차에 따라 α→1로 키우는 **error-adaptive α**도 동류 | ⭐⭐ `ActionFilter` 최소 변경 |
| **B. 웨이포인트 통과 보간** | ✅ | **cubic spline**(넉을 정확히 통과, C² 연속) 또는 **minimum-jerk 5차 다항식**(끝점 위치·속도·가속 지정 → 깔끔히 도달). 균일 EMA를 이걸로 교체 | ⭐⭐⭐ 정석 |
| **C. ★ 청크 배치 비인과 스무딩** | ✅ (핀 고정 시) | §8.3 참고 | ⭐⭐⭐⭐ 이 구조에 최적 |
| **D. Savitzky-Golay** | 근사(피크 보존) | 국소 다항식 적합 → EMA와 달리 극값의 높이·위치를 잘 보존. 창(window) 필요 → 약간 지연 | ⭐⭐ |
| **E. Ruckig / TOPP-RA (OTG)** | ✅ | 저크 제한 실시간 궤적 생성으로 목표 상태에 속도·가속 제약 지키며 도달. 가장 "정석"이나 통합 비용 큼 | ⭐ 무겁다 |

### 8.3 ★ 핵심 통찰 — 청크가 곧 미래(lookahead)다

현재 `ActionFilter`는 **인과(causal) 필터**다. 즉 "미래를 모른다"고 가정하고 과거값만으로 lag를 건다. **그런데 SmolVLA/ACT는 청크 단위로 미래 액션을 통째로 뱉고**, 그게 `_action_queue`에 이미 다 들어있다. 미래를 아는데도 causal lag 필터를 쓰니 불필요한 지연·undershoot가 생기는 것이다.

→ **청크가 큐에 들어오는 순간, 낱개 스텝 EMA 대신 청크 전체를 배치로 스무딩**하면 된다:
- **Zero-phase 필터**(filtfilt, 전방–후방 2패스) → 위상 지연 0, 지연성 undershoot **구조적 소거**
- 또는 **청크에 스플라인 적합** 후 재샘플
- 여기에 **키포인트 인덱스를 핀(pin)으로 고정** → 그 점은 정확히 통과

```
청크 도착 (a_0 … a_T)
  1. must-hit 인덱스 검출  (그리퍼 전이, 속도 최소점, 청크 끝)
  2. 팔 관절: 키포인트를 넉으로 두고 스플라인/zero-phase 스무딩 (넉은 정확 통과)
  3. 그리퍼: 스무딩 제외 — step/snap  (§8.4 참고)
→ 스무딩된 청크를 큐에 저장 → 이후는 낱개 EMA 없이 그대로 재생
```

> 💡 **왜 이게 undershoot를 없애나?** 인과 EMA의 undershoot는 "미래를 몰라 항상 뒤늦게 반응"하기 때문이다. 청크 전체를 알고 비인과로 스무딩하면 위상 지연이 사라지고, 키포인트를 넉으로 고정하면 그 점은 정의상 정확히 통과한다.

### 8.4 놓치기 쉬운 실무 포인트 — 그리퍼

**그리퍼는 저역통과에서 빼야 한다.** 그리퍼는 사실상 이진(open/close)이라 EMA를 걸면 닫힘이 느려져 **약한/빗나간 파지**가 된다. "그리퍼 닫힘 상태" 자체가 대표적 must-reach이므로 스무딩 대상에서 제외하고 스냅하는 게 맞다.

> ⚠️ 현재 코드([`action_filter.py`](../wrapper/action_filter.py))는 **속도 제한에서만** 그리퍼를 분리하고(`"gripper" in key`), **보간·저역통과에는 함께** 들어간다. 정밀 파지가 목표라면 이 두 필터에서도 그리퍼를 제외하는 것이 우선 개선점.

### 8.5 이 시스템 권장 조합

- **가볍게**: **A만** — (1) 그리퍼를 보간·저역통과에서 제외, (2) 검출된 dwell/그리퍼-전이에서 필터 상태 리셋 또는 error-adaptive α. `ActionFilter`만 손대는 targeted 변경.
- **제대로**: **C + A** — `_action_queue` 채우는 지점(청크 생성부)에서 배치 비인과 스무딩 + 키포인트 핀 + 그리퍼 스냅. 낱개 EMA를 대체하며 undershoot를 원천 제거하고 청크 형상은 유지.

> 📌 **트레이드오프 요약 (스무딩 계열 전체)**: 스무딩의 목표는 "노이즈 억제"인데 대가는 "지연·정밀도 손실"이다. **무엇을 지킬지(reach) 아느냐**가 관건 — must-hit을 인지하면 스무딩과 정밀 도달을 동시에 얻을 수 있고, 모르면 둘 중 하나를 포기해야 한다.

---

## 부록 A. 용어집

| 용어 | 뜻 |
|---|---|
| **관측(observation)** | 모델 입력. 카메라 이미지 + 관절 상태(state) + task 텍스트 |
| **액션(action)** | 모델 출력. 다음 스텝 관절/그리퍼 목표값 |
| **청크(chunk)** | 모델이 한 번에 예측하는 여러 스텝의 액션 묶음 |
| **정규화(normalize)** | 입력을 평균0/std1 또는 [-1,1]로 스케일 조정 |
| **MEAN_STD / MIN_MAX** | 정규화 방식 두 종류 (표준화 vs 최소-최대) |
| **CHW / HWC** | 텐서 축 순서. Channel-Height-Width vs Height-Width-Channel |
| **토크나이즈(tokenize)** | task 문장을 토큰 ID 시퀀스로 변환 (언어 인코더용) |
| **저크(jerk)** | 가속도의 시간 미분. 클수록 충격·진동 |
| **EMA / 저역통과** | 지수이동평균. 고주파 노이즈 억제 스무딩 |
| **RTC** | Real-Time Chunking. flow-matching 정책의 실시간 청크 스무딩 |
| **ZMQ** | 실행 중 프로세스에 실시간 파라미터를 보내는 IPC |
| **undershoot** | 엔드 이펙터가 목표 위치에 못 미쳐 멈추는 현상 |
| **인과/비인과(causal/non-causal)** | 과거값만 쓰는가 vs 미래값(청크)도 쓰는가. 비인과는 위상 지연 없음 |
| **must-hit / 웨이포인트** | 반드시 정확히 통과해야 하는 지점(파지·놓기·정렬) |
| **제약 스무딩** | must-hit을 하드 제약(넉)으로 두고 나머지만 부드럽게 하는 스무딩 |
| **cubic spline** | 넉을 정확히 통과하는 C² 연속 보간 곡선 |
| **minimum-jerk** | 끝점 위치·속도·가속을 지정하는 5차 다항식 궤적(저크 최소) |
| **zero-phase / filtfilt** | 전방–후방 2패스 필터. 위상 지연 0 (비인과) |
| **Savitzky-Golay** | 국소 다항식 적합 스무딩. 극값(피크) 보존에 강함 |
| **OTG (Ruckig/TOPP-RA)** | 온라인 궤적 생성. 저크·속도 제한 지키며 목표에 도달 |
| **dwell** | 궤적이 감속해 잠시 멈추는 지점(정밀 정렬·접촉 구간) |

## 부록 B. 파일 지도

| 파일 | 역할 |
|---|---|
| [`wrapper/lerobot_wrapper.py`](../wrapper/lerobot_wrapper.py) | 추론 경로. 관측 전처리·모델 호출·액션 후처리 |
| [`wrapper/action_filter.py`](../wrapper/action_filter.py) | ActionFilter 체인 (속도·보간·저크·저역통과) |
| [`wrapper/start_record.py`](../wrapper/start_record.py) | 수집 경로. LeRobot record 래핑 + 프리뷰/제어 탭 |
| [`backend/app/core/cli_mapping.py`](../backend/app/core/cli_mapping.py) | CLI 인자 매핑 (subprocess 실행) |
| [`backend/app/services/zmq_bridge.py`](../backend/app/services/zmq_bridge.py) | ZMQ 실시간 파라미터 브리지 (SAFE_PARAMS) |
