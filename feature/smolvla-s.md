# SmolVLA-S — 행동 + 작업 단계(stage)를 함께 내는 SmolVLA 변형

> **◐ 1단계 완료** (패키지·yaml·테스트). 남은 것: smolvla 체크포인트에서 출발(워밍스타트, §6), 실기 학습·A/B.
>
> [ACT-S](act-s.md) 의 stage 헤드를 SmolVLA 에 붙인 것이다. 헤드·손실·confidence·굽기·wrapper 배선은
> **ACT-S 와 같은 코드**이고(act-s.md §3~§6), 이 문서는 다른 것만 적는다 — 특징을 **어디서** 꺼내나.
> 상류 SmolVLA 는 한 줄도 건드리지 않는다.

---

## 0. 요약

| 구성 | 내용 |
|---|---|
| 정책 타입 | `smolvla_s` — `SmolVLAConfig`/`SmolVLAPolicy` **상속** + `StageConfig`/`StageHeadMixin` |
| 위치 | `custom_policies/lerobot_policy_piper/smolvla_s/` — ACT-S 와 같은 배포(`lerobot_policy_piper`) |
| 특징 | VLM **prefix 출력**(이미지·언어·state 토큰) → 패딩 뺀 풀링 → stage 헤드 (§1·§2) |
| 비용 | 0 — 학습 손실 forward 와 추론의 KV 캐시 채우기가 이미 prefix 출력을 낸다 |
| 데이터 | ACT-S 와 같은 구운 `_stage` 데이터셋 (`subtask_index`) — 굽기는 정책을 모른다 |
| 추론 | wrapper 무수정 — `last_aux` 모양이 ACT-S 와 같다. stage 는 청크마다 한 번 |
| 학습 시작점 | **처음부터만** — VLM 은 SmolVLM2 가중치, 행동 전문가는 무작위 (§6) |

---

## 1. 어디서 특징을 꺼내나 — VLM prefix 출력

SmolVLA 는 VLM 이 `[이미지 토큰…, 언어 토큰…, state 토큰]`(prefix)을 돌리고, 행동 전문가가 그 KV 를 보며
flow matching 으로 청크를 낸다. 후보는 둘이었다:

| 자리 | 판정 |
|---|---|
| **VLM prefix 출력** | ✅ 관측만의 함수다. 학습 손실 forward(`[prefix, suffix]` 한 번에)와 추론의 KV 채우기(`[prefix, None]`)가 **둘 다 이미 돌려준다** — 추가 연산이 없다 |
| 행동 전문가(suffix) 출력 | ✗ 노이즈 액션·타임스텝이 섞인다. 추론에선 디노이즈 10스텝마다 다른 값이라 "어느 스텝 것" 을 정해야 하고, 학습 때 본 분포(무작위 t)와 맞지 않는다 |

### 1.1 hook 이 아니라 forward 감싸기

ACT-S 는 `register_forward_hook` 이다. 여기선 **안 돈다** — `VLAFlowMatching` 이
`self.vlm_with_expert.forward(...)` 를 직접 불러 `__call__` 을 안 거친다. 그래서 그 **인스턴스의**
`forward` 하나만 `types.MethodType` 으로 감싼다 (클래스·다른 인스턴스는 그대로, deepcopy·pickle 안전).
디노이즈 스텝 호출은 prefix 가 `None` 이라 건너뛴다. 읽은 prefix 는 바로 비운다 — 다음 호출이 낡은 것을 못 쓴다.

### 1.2 학습 때 본 것 = 추론 때 본 것 — 단, 패딩을 빼야

블록 마스크라 suffix 는 prefix 를 못 바꾼다. **실측**: 유효 토큰의 prefix 출력은 학습 forward 와 추론
KV 채우기에서 차이 **0.0** 이다. 다른 것은 언어 **패딩 토큰**뿐(최대 0.4) — 행 전체가 막힌 토큰의
softmax 가 보는 열 수가 둘에서 다르기 때문이다. 그래서 풀링이 패딩을 빼지 않으면 헤드가 학습과 다른 입력을
받는다. 패딩 마스크는 VLM 에 넘어가는 2D 어텐션 마스크의 **대각선**과 같다(`att & pad_i & pad_j`, 자기 자신은
늘 본다) — `embed_prefix` 를 또 감싸지 않는다. `test_training_and_inference_see_the_same_features` 가
잡는다 (패딩을 안 빼는 변이를 넣으면 실패하는 것을 확인했다).

---

## 2. 풀링 — 기본이 `state` 인 이유

| | ACT 인코더 | SmolVLA prefix |
|---|---|---|
| 어텐션 | 양방향 — 모든 토큰이 모든 토큰을 본다 | 블록 인과 — 이미지·언어 블록은 **state 를 못 본다**, state 토큰은 앞의 전부를 본다 |
| `mean` | 이미 전부를 담는다 → ACT-S 기본 | 이미지 토큰(카메라당 64)이 지배하고 state 정보는 state 토큰 하나뿐 |
| `state` | `encoder_out[1]` | **마지막 실제 토큰** — 이미지·언어·state 를 다 본 유일한 토큰 → SmolVLA-S 기본 |

둘 다 config `pool` 로 고른다. state 토큰 위치는 패딩 마스크의 마지막 참값이다 (`prefix_length` 맞춤 패딩은 끝에 붙는다).

---

## 3. 그래디언트 — 무엇이 바뀌나

`train_expert_only=True`(SmolVLA 기본)면 VLM 은 얼어 있다. CE 는 **헤드와 `state_proj`**(state 를 VLM 폭으로
투영하는 층, 학습됨)로만 흐른다 — 이미지·언어 표현은 안 바뀐다 (테스트가 잡는다). 그래서 ACT-S 의
"보조 손실이 표현을 정돈한다" 가설은 여기선 state 투영에만 걸린다. 사실상 **얼린 VLM 위의 탐침 + state 투영
미세 조정**이다. 감시 출력(stage 표시·오케스트레이터 신호)으로서의 가치는 그대로다.

λ(`stage_loss_weight`)=0 이면 바닐라 SmolVLA 와 손실이 수치 동일하다 (노이즈·시간 고정 시, 테스트).
`reduction="none"`(RA-BC)이면 배치 평균 CE 를 샘플마다 더한다 — 평균은 스칼라일 때와 같다.

---

## 4. 추론

`predict_action_chunk` 는 시그니처 그대로(RTC 인자도 통과) 액션을 내고 `last_aux` 를 채운다. `select_action` 은
큐가 빌 때만 청크를 새로 내므로 그때만 stage 가 바뀐다. wrapper·텔레메트리·화면은 ACT-S 때 이미 배선돼 있어
고친 것이 없다 (act-s.md §6). RTC 가이던스는 디노이즈 루프 쪽이라 stage 와 무관하다.

gRPC 정책 서버 경로는 ACT-S 와 같은 이유로 안 된다 — LeRobot `async_inference` 가 정책 이름 목록을 박아 두고
있다(`SUPPORTED_POLICIES`). 로컬 추론만.

---

## 5. 저장소 배선

| 파일 | 내용 |
|---|---|
| `custom_policies/lerobot_policy_piper/smolvla_s/*` | config(`StageConfig` + `SmolVLAConfig`, `pool="state"`) · 모델 · 프로세서(SmolVLA 위임) |
| `policies/smolvla_s.yaml` | `rtc: true`, `language: true`, `encoder_probe: false`, 학습 필드 = SmolVLA 것 + stage 셋 |
| `backend/app/core/cli_mapping.py` | `load_vlm_weights` 안전망을 SmolVLA-S 에도 (`_VLM_WEIGHT_POLICIES`) — 기본값 false 를 물려받는다 |
| `backend/app/core/policies.py` | 목록에서 SmolVLA 바로 뒤. `guess_from_name` 이 이름 뒤에 글자가 오면 거른다 — `smolvla_so101_pick` 이 SmolVLA-S 로 잡히지 않게 |
| `custom_policies/tests/test_smolvla_s.py` | 진짜 SmolVLM2 config(무작위 가중치, 2층)로 — HF 캐시에 없으면 건너뛴다 |

상류가 `model.vlm_with_expert` 이름을 바꾸면 `__init__` 이 명확한 오류로 죽는다.

---

## 6. 다음 — smolvla 체크포인트에서 출발 (워밍스타트)

지금은 **처음부터만** 학습된다. `lerobot/smolvla_base` 는 config `type: smolvla` 라 `--policy.path` 로 주면
type 이 체크포인트에서 오므로 **바닐라 SmolVLA 로** 학습된다. 학습 화면은 이어 학습 후보를 같은 type 으로
거르므로 그 실수는 화면에선 안 일어나지만, 대신 SmolVLA-S 는 로봇 사전학습 없이 시작한다
(`bases.policy` 를 비워 둔 이유). SmolVLA 를 바닥부터 올리는 것은 smolvla_base 파인튜닝보다 확실히 약하다.

길은 ACT-S 의 `from_act.py`(act-s.md §5.4)와 같다 — config 의 `type` 만 바꾸고 stage 필드를 채운 사본을 만들면
가중치는 그대로 실리고(`strict=False`) 헤드만 무작위로 시작한다. 이어 학습 base 를 이미 사본으로 고치는
`core/base_align.py` 가 그 자리다(로컬·원격 공용). 화면은 `smolvla` 체크포인트를 SmolVLA-S 의 이어 학습 후보로
보여줘야 하고, 이어 학습이면 `--policy.*` 를 버리는 지금 규칙 때문에 stage 필드를 사본 config 에 써 넣어야 한다.
SmolVLA vs SmolVLA-S A/B 는 둘 다 smolvla_base 에서 출발해야 공정하므로 이게 먼저다.
