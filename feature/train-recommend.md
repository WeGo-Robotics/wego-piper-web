# 학습 파라미터 권장 — 데이터 크기에 맞춘 steps·저장 주기·스케줄

> ☐ 기획 (2026-10-30). 근거는 이 기계에 남은 학습 39건(§0). 먼저 들어간 것: `scheduler_decay_steps` 칸과
> 경고(§2-3). 곁가지로 학습 남은 시간·완료 예정과 GPU 메모리 가늠(`training/gpu_mem.py`)이 같은 날 들어갔다.

학습 폼이 고른 데이터셋의 크기(에피소드·프레임)를 보고 **steps·save_freq·스케줄 길이를
권하고**, 지금 설정이 같은 프레임을 몇 번 보게 되는지(**에폭**)를 늘 보여 준다. 에피소드
수가 정하는 손잡이(검증 분리·증강·이어 학습)는 근거가 생기는 순서대로 붙인다.

## 0. 왜 — 이 기계의 학습 39건

`~/outputs/train/*/checkpoints/last/pretrained_model/train_config.json` 과 데이터셋
`meta/info.json` 을 맞대 본 것. **에폭 = batch × steps / 프레임.**

| 실행 | 정책 | 에피소드 | 프레임 | batch × steps | 에폭 |
|---|---|---|---|---|---|
| 10-20 singlearm_apple_4point | smolvla | 32 | 6,780 | 64 × 200,000 | **1,888** |
| 10-13 sort_apple | smolvla | 50 | 9,986 | 64 × 200,000 | **1,282** |
| 10-21 singlearm_apple_4point_random | smolvla | 25 | 5,469 | 64 × 20,000 | 234 |
| 10-20 dualarm_cube_4point | smolvla | 15 | 6,437 | 64 × 20,000 | 199 |
| 10-22 singlearm_apple_random | smolvla | 100 | 21,224 | 64 × 20,000 | 60 |
| 09-21 so101_bolt | act | 26 | 4,886 | 8 × 50,000 | 82 |
| 10-28 singlearm_apple_random_v2 | act | 100 | 21,224 | 8 × 100,000 | 38 |
| 10-28 singlearm_apple_random_v2 | act_dinov3 | 100 | 21,224 | 8 × 20,000 | **7.5** |

- **에폭이 7.5 ~ 1,888 — 250배 벌어져 있다.** steps 가 정책 기본값(ACT 100k, SmolVLA 200k)이거나
  손으로 친 값이고, 어느 쪽도 데이터 크기를 안 본다. 15 에피소드에 쓴 20k 와 100 에피소드에 쓴
  20k 는 같은 숫자지만 3배 다른 학습이다.
- **화면 어디에도 에폭이 없다.** Steps 칸만 있다.
- **SmolVLA 200k 두 번은 85% 를 lr 바닥에서 돌았다.** cosine 스케줄의 `scheduler_decay_steps`
  가 30,000 으로 고정이라 30k 에서 lr 이 2.5e-6 에 닿고 나머지 170k 를 거기 머문다. LeRobot 은
  steps 가 decay 보다 **짧을 때만** 스케줄을 줄여 주고, 길 때는 늘려 주지 않는다. 10-28
  smolvla_s 80k 한 번은 누군가 decay 를 80,000 으로 손으로 맞췄다(lr 기록이 80k 까지 내려간다).
  두 번 빠뜨리고 한 번 맞춘 것 — 사람이 기억할 일이 아니다.
- **무엇이 좋았는지는 지금 모른다.** eval_log 가 9건이고 손실 곡선이 남은 학습은 5건이다.
  10-28 ACT(100 에피소드)는 loss 가 23k step(8.7 에폭)에서 0.108 → 99k 에서 0.107 — 평평하다.
  그 뒤 76k step 이 실기 성공을 바꿨는지는 기록이 없다. 그래서 1단계는 **새 지식이 아니라
  지금 습관에서 극단을 걷어 내는 것**이고, 최적값은 3단계 실험(§7)이 정한다.

## 1. 원칙

- **에피소드와 프레임은 다른 손잡이를 정한다.** 프레임 → 얼마나 오래(steps). 에피소드 →
  시연이 얼마나 다양한가(과적합 위험·검증 분리·증강·이어 학습).
- **단위는 에폭이다.** steps 는 데이터셋이 바뀌면 뜻이 바뀐다.
- **근거가 있는 정책만 권한다.** 이 기계에서 돌린 적 없는 정책(pi0·pi05·diffusion…)은 에폭
  표시와 스케줄 맞춤(§2-3, 이건 근거가 LeRobot 코드다)만 한다.
- **덮어쓰지 않는다.** 권장은 줄 하나와 [적용] 버튼이다. 프리셋·저장값이 그대로 남는다.
- **배치는 권하지 않는다** — [train-loader-workers §3](train-loader-workers.md) 와 같은 이유로
  lr·일반화를 바꾸는 손잡이다. 사용자가 고른 배치에서 steps 를 계산한다(배치를 바꾸면
  에폭이 유지되게 steps 가 따라 바뀐다). lr 도 권하지 않는다.
- 백엔드는 숫자와 `reason` 코드만 주고 문장은 프론트가 `t()` 로 만든다.

## 2. 무엇을 권하나

| 값 | 무엇이 정하나 | 규칙 | 단계 |
|---|---|---|---|
| steps | 프레임·배치 | `E × 프레임 / 배치`, 1,000 단위 반올림, [min, max] 로 자름 | 1 |
| save_freq | steps | `⌈steps / 5⌉` → 체크포인트 5개 | 1 |
| scheduler_decay_steps | steps | cosine 정책에서 steps > 기본 decay 면 `= steps` | 1 |
| log_freq | steps | 그대로. 단 `steps / log_freq < 20` 이면 곡선이 안 되니 줄인다 | 1 |
| 극단 에폭·적은 에피소드 경고 | 에폭·에피소드 | §3 | 2 |
| 검증 분리 | 에피소드 | ≥ 20 에피소드면 10%(2~10개)를 빼고 학습 | 3 |
| 이미지 증강 | 에피소드 | A/B 전에는 권하지 않는다 | 4 |
| chunk_size | fps·에피소드 길이 | **권하지 않고 초로 보여 준다**(§4) | 2 |
| num_workers | 로더 실측 | [train-loader-workers](train-loader-workers.md) 가 이미 한다 | — |
| batch · lr | — | 권하지 않는다 | — |

### 2-1. steps

```
steps_rec = clamp( round_1000( E × 프레임 / batch ), min_steps, max_steps )
```

| 정책 | E (초기값) | 근거 | min | max |
|---|---|---|---|---|
| ACT · ACT-S · ACT-DINOv3 | 50 | 이 기계 ACT 계열 19건의 에폭 중앙값 49.8 | 10,000 | 300,000 |
| SmolVLA · SmolVLA-S (base 에서) | 60 | 14건 중앙값 60.3 — 같은 설정을 되풀이한 8건이 끌어올린 값이다 | 5,000 | 100,000 |
| 그 밖 | — | 돌린 적 없음 → steps 권장 안 함 | | |

같은 규칙이 지난 학습에 무엇을 권했을지:

| 데이터셋 | 정책 | 에피소드 | 프레임 | 실제 steps (에폭) | 권장 steps (에폭) |
|---|---|---|---|---|---|
| singlearm_apple_4point | smolvla | 32 | 6,780 | 200,000 (1,888) | 6,000 (57) |
| sort_apple | smolvla | 50 | 9,986 | 200,000 (1,282) | 9,000 (58) |
| dualarm_cube_4point | smolvla | 15 | 6,437 | 20,000 (199) | 6,000 (60) |
| singlearm_apple_random_v2 | smolvla | 100 | 21,224 | 20,000 (60) | 20,000 (60) — 그대로 |
| singlearm_apple_random_v2 | act | 100 | 21,224 | 100,000 (38) | 133,000 (50) |
| so101_bolt | act | 26 | 4,886 | 50,000 (82) | 31,000 (51) |
| dualarm_cube_4point | act | 15 | 6,437 | 20,000 (25) | 40,000 (50) |

- **프레임**은 학습에 실제로 들어가는 것만 센다. 시나리오에서 제외한 에피소드가 있으면
  (`sidecar.kept_episodes`) 남는 에피소드 길이의 합이다 — `apply_scenario_exclusions` 와 같은 자리를 본다.
- **이어 학습**: 같은 작업의 체크포인트에서 이어 받으면 steps 를 권하지 않고 에폭만 보인다
  (ACT 파인튜닝은 1건뿐이라 근거가 없다). `smolvla_base` 처럼 정책의 **베이스**에서 시작하는
  것은 이어 학습이 아니라 출발점으로 보고 위 표를 쓴다. ⚠ SmolVLA 중앙값 14건에는 체크포인트에서
  이어 받은 것과 아닌 것이 섞여 있다 — 어느 쪽이든 VLM 은 사전학습 가중치로 시작하므로
  (`load_vlm_weights` override) 1단계는 나누지 않는다.

### 이 계산이 틀리는 곳

- **E 는 습관의 중앙값이지 최적값이 아니다.** 극단(1,888 · 7.5)을 걷어 낼 뿐이다. 화면에
  "이 기계 학습의 중앙값에서 출발한 초기값" 이라고 밝힌다. 3단계 실험(§7)이 바꾼다.
- **에피소드가 많을수록 적은 에폭으로 충분할 수 있다.** 1단계는 E 하나(steps ∝ 프레임)다.
  데이터가 4배면 steps 도 4배다. 실험이 "에피소드가 늘수록 최적 에폭이 준다" 고 하면
  `E × √(50 / 에피소드)` 같은 규칙으로 바꾼다(데이터 4배 → steps 2배).
- **프레임 에폭은 fps 를 안 본다.** 30fps 데이터는 같은 시연에서 프레임이 두 배라 같은 E 가
  두 배 오래 학습한다. 이 기계 데이터셋은 전부 15fps 라 지금은 드러나지 않는다.
- **loss 로는 못 고친다.** LeRobot 은 검증 손실을 안 낸다. 학습 loss 가 평평해진 뒤가 과적합인지
  여전히 나아지는 중인지 학습 loss 로는 알 수 없다(§0 의 ACT). 그래서 3단계가 검증 분리다.

### 2-2. save_freq

`⌈steps / 5⌉` (100 단위 올림) → 체크포인트 **정확히 다섯 개**. LeRobot 은 `step % save_freq == 0`
이거나 마지막 step 이면 저장하므로, 올림이어야 네 번째 주기 저장 뒤 마지막 저장이 다섯 번째가 된다
(내림이면 133,000 / 26,000 → 여섯 개). `last` 는 마지막 것을 가리키는 링크라 따로 안 센다.
지난 학습은 네 개가 가장 흔했다(19건, `steps / 4`).
[가중치 분석기](weight-analyzer.md)가 step 축으로 비교하므로 **과적합 지점을 찾을 수 있을 만큼**만 남긴다.

체크포인트 한 개 실측: ACT 591MB, SmolVLA-S 2.4GB. 다섯 개면 3GB · 12GB 다. 남는 디스크가
`PIPER_DISK_WARNING_THRESHOLD_GB`(10) 아래로 내려갈 것 같으면 `disk_low` 를 띄운다.

### 2-3. 스케줄 길이 — 이건 확실하다

cosine 스케줄 정책(smolvla·smolvla_s·pi0·pi05·pi0_fast)은 `scheduler_decay_steps` 가 30,000 으로
박혀 있다. LeRobot `CosineDecayWithWarmupSchedulerConfig.build` 는 steps < decay 일 때만 warmup·decay 를
비례로 줄인다. steps > decay 면 줄여 주지 않으니 우리가 맞춘다:

```
steps > spec.default(scheduler_decay_steps)  →  --policy.scheduler_decay_steps=<steps>
```

- ☑ 정책 YAML 에 `scheduler_decay_steps` 필드(`from_lerobot: true`, 기본 30,000). 화면은 Steps 가 더 길면 칸 아래에
  경고와 [Steps 에 맞추기] 를 띄운다 — 값을 몰래 따라 바꾸지 않는다. 1단계의 권장 [적용] 이 이 값도 함께 바꾼다.
- ACT 는 스케줄이 없고(lr 고정), diffusion 은 `num_training_steps` 를 그대로 받아 해당 없다.
- 에폭 규칙과 무관하게 맞다. 그래서 E 근거가 없는 pi0·pi05 에도 적용한다.

## 3. 에피소드 수가 정하는 것 — 경고

| 코드 | 조건 | 화면 |
|---|---|---|
| `epochs_high` | 지금 설정의 에폭 > 5E | "같은 프레임을 1,888번 봅니다 — 권장 57" |
| `epochs_low` | 지금 설정의 에폭 < E/5 | "7.5 에폭 — 한 바퀴를 몇 번 못 돕니다" |
| `few_episodes_scratch` | VLA 정책 · 베이스 없이 처음부터 · 에피소드 < 50 | "적은 시연으로 처음부터 — 베이스에서 시작하세요" (베이스 받기 버튼은 이미 있다) |
| `decay_short` | cosine 정책 · steps > decay · decay 를 안 맞춤 | "lr 이 30,000 에서 바닥 — 나머지 85%는 거의 안 배웁니다" |
| `disk_low` | §2-2 | 예상 사용량과 남는 디스크 |
| `no_basis` | 정책 YAML 에 `recommend` 없음 | 권장 줄 대신 "이 정책은 아직 권장 근거가 없습니다 — 에폭만 보여 줍니다" |

문턱 5배·1/5 는 E 에 붙인 상대값이다. E 가 실험으로 바뀌면 같이 움직인다.

## 4. chunk_size 를 초로 — 권하지 않고 보여 준다

ACT 기본 chunk 100 은 ALOHA 의 50Hz 에서 2초다. 이 기계는 15fps 라 **6.7초**이고,
singlearm_apple_random_v2 의 평균 에피소드(14.1초)의 47% 다. 에피소드 수가 아니라 fps 와 작업
길이가 정하는 값이고 `arch: true`(재시작 필요)라 권하지 않는다 — 아키텍처 필드 옆에
"= 6.7초 · 에피소드 평균의 47%" 만 붙인다. 바꿀지는 [ACT 변형](act-variants.md) 의 A/B 몫이다.

## 5. 화면

**(a) 파라미터 카드** ([TrainingPage.tsx](../frontend/src/pages/TrainingPage.tsx))

```
파라미터
 데이터  100 에피소드 · 21,224 프레임 · 평균 14.1초 (15fps)
 권장    133,000 steps (50 에폭) · 저장 26,600                    [권장값 적용]
         이 기계 ACT 학습 19건의 중앙값에서 출발한 초기값입니다
 Batch Size [8      ]   Steps [100000   ]
                        = 37.7 에폭
```

- `= N 에폭` 은 **늘** 보인다(권장 근거가 없는 정책도). 프리셋을 불러와도 보이므로 다른
  데이터셋에서 만든 프리셋의 steps 가 여기서 몇 에폭인지 바로 안다.
- [적용]은 steps·save_freq·(cosine 이면) scheduler_decay_steps 를 한 번에 바꾼다.
- 데이터셋·정책·배치·베이스가 바뀌면 다시 계산한다. 값을 바꾸지는 않는다.

**(b) 체크포인트 표·작업 목록** — 에폭 열. `train_config.json` 에 batch·steps·데이터셋이 다 있어
지난 39건에도 소급된다.

**(c) 임대 학습** — 같은 권장 + 예상 시간. 로더 프로파일(`piper:train:loader_profiles`)에 같은
데이터셋·정책의 step 시간이 있으면 `steps × (updt_s + data_s)` 로 잰다. 임대 기계는 GPU 가 달라
"이 기계 기준" 이라 밝힌다.

## 6. 백엔드

- 순수 함수 `training/recommend.py` 의 `recommend(frames, episodes, fps, batch, spec, start)` →
  `{frames, episodes, avg_len_s, epochs_now, steps, epochs, save_freq, decay_steps, basis, warnings}`.
  `start` 는 `scratch | base | checkpoint`. `tests/test_train_recommend.py` 가 §2-1 표를 그대로 검사한다.
- `GET /api/training/recommend?dataset=&policy=&batch=&steps=&pretrained=` — 지금 steps 를 받아야
  `epochs_high/low` 를 낸다.
- 정책 YAML `train.recommend: { epochs: 50, min_steps: 10000, max_steps: 300000 }` →
  `policy_spec.TrainSpec.recommend`. **없으면 권장 안 함** — 정책 추가가 여전히 파일 하나로 끝난다
  ([policy-ui-spec](policy-ui-spec.md)).
- LeRobot·러너·wrapper 무수정. CLI 에 새로 나가는 인자는 `--policy.scheduler_decay_steps` 하나뿐인데,
  그것도 이미 있는 `policy_params` 경로를 탄다.

## 7. 실험 — E 를 정하는 법 (3단계)

질문 둘: **최적 에폭은 얼마인가**, **에피소드가 늘면 그게 줄어드나**(상수 E 냐 √ 규칙이냐).

- 데이터셋 `singlearm_apple_random_v2`(100 에피소드, 15fps). **검증 10 에피소드를 고정**으로 빼고,
  남은 90 에서 25 · 50 · 90 에피소드 부분집합을 `--dataset.episodes` 로 고른다.
- ACT, 배치 8, 120 에폭까지, **10 에폭마다** 저장.
- 측정: 체크포인트마다 [가중치 분석기](weight-analyzer.md)의 open-loop 오차를 **검증 10 에피소드로** 잰다 →
  오차가 최저인 에폭. open-loop 오차는 실기 성공이 아니므로, 부분집합마다 E/2 · E · 2E 세 체크포인트를
  실기 10회씩(90회) 돌려 맞춰 본다.
- 비용: ACT step 0.053초(10-28 실측, 10k step 8.7분). 25 · 50 · 90 에피소드가 80k · 159k · 287k step →
  **GPU 약 7.7시간**(밤 한 번), 디스크 36개 × 591MB ≈ 21GB, 실기 90회.
- 판정: 최저 에폭이 세 부분집합에서 비슷하면 상수 E, 많을수록 작으면 √ 규칙. SmolVLA 는 step 이
  비싸니 ACT 결과를 보고 그다음에 한다.

검증 분리 자체는 이 실험의 부산물로 기능이 된다 — 학습 폼에 "검증용 N 에피소드 빼기" 를 두고,
`--dataset.episodes` 를 시나리오 제외와 **합친다**(둘이 같은 인자다). 정규화 통계는 전체 에피소드
기준 그대로다 — 시나리오 제외와 같은 판단(정규화만 새고 행동은 안 샌다).

## 8. 단계

0. ☐ 에폭 표시 — 폼(`= N 에폭`)·체크포인트 표·작업 목록. 계산만, 권장 없음
1. ☐ `recommend.py` + 엔드포인트 + 폼 권장 줄·[적용] (steps·save_freq·decay) + YAML `recommend` + 테스트
   (`scheduler_decay_steps` 필드·경고는 ☑ 먼저 들어갔다)
2. ☐ 경고 §3 + chunk 초 표시 §4 + 임대 예상 시간 §5(c)
3. ☐ 검증 분리 + §7 실험 → E 확정, 필요하면 √ 규칙
4. ☐ 이미지 증강 — `--dataset.image_transforms.enable`(LeRobot 기본 꺼짐, 밝기·대비·채도 0.8~1.2 등)을
   폼에 노출하고 A/B. 에피소드 몇 개 아래에서 켤지는 그 결과로 정한다. [조명 변화 감시](lighting-watch.md)·
   [회색 카드](gray-card-calibration.md)는 조명을 **고정하는** 대책이고 증강은 **견디게 하는** 대책이라,
   둘 다 필요한지도 A/B 가 답한다
5. ☐ 실측 프로파일 — 검증 분리로 돈 학습마다 검증 오차 최저 에폭을 남긴다(Redis 해시, 키
   `데이터셋|정책`, [로더 프로파일](train-loader-workers.md)과 같은 모양). 다음 학습에서 "지난 실측:
   이 데이터셋은 42 에폭에서 검증 오차 최저 [적용]" — `basis` 가 `habit` 에서 `measured` 로 바뀐다

## 열린 질문

- **학습 프리셋이 steps 대신 에폭을 담아야 하나.** 프리셋은 "다른 데이터셋에 재사용" 하려고
  `dataset_repo_id` 를 빼는데(`PRESET_EXCLUDED`), steps 는 데이터셋이 바뀌면 뜻이 바뀐다. 1단계는 에폭을
  보여 주는 것으로 버티고, 프리셋 스키마는 손대지 않는다.
- **fps 가 다른 데이터셋이 생기면** 프레임 에폭을 초 에폭(프레임/fps)으로 바꿀지.
- **같은 작업 체크포인트에서 이어 학습**할 때 권할 것이 있나 — 근거가 1건이다.
- **검증 분리를 기본으로 켤까** — 20 에피소드에서 2개를 빼면 학습 데이터 10% 를 잃는다. 3단계는
  끄는 게 기본이고, 실험이 검증 오차가 쓸모 있다고 보여 주면 다시 본다.
