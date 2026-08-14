# 정책 UI 스펙 — 모델별 화면을 선언으로 굴린다

지원 모델이 늘 때마다 **여섯 곳**을 고쳐야 하고 그 중 둘은 TSX 안에 있다.
모델 하나를 파일 하나로 만들고, 화면이 그 파일을 읽어 스스로 구성하게 한다.

---

## 지금 무엇이 흩어져 있나 — 세어보면 여섯이다

정책 하나를 추가할 때 손대야 하는 곳:

| # | 위치 | 무엇 | 종류 |
|---|---|---|---|
| 1 | [core/policies.py](../backend/app/core/policies.py) `POLICIES` | 라벨·`train`/`infer`/`rtc`/`language`/`encoder_probe`·베이스 체크포인트 | 데이터 |
| 2 | [core/inference_params.py](../backend/app/core/inference_params.py) `PARAM_SPEC` | 정책별 노출 파라미터 (`"policies": ["act"]`) | 데이터 |
| 3 | [wrapper/lerobot_wrapper.py](../wrapper/lerobot_wrapper.py#L107) `POLICY_IMPORTS` | 모델·config 클래스 경로 4개 | 데이터 |
| 4 | [wrapper/encoder_probe.py](../wrapper/encoder_probe.py#L334) | `--tap` 선택지, 기본 tap 분기 | 데이터 + 분기 |
| 5 | [TrainingPage.tsx](../frontend/src/pages/TrainingPage.tsx#L53) `POLICY_TRAIN_SCHEMAS` | **학습 필드 전체** — 이름·범위·기본값·`arch` 여부 | 데이터 (TSX 안) |
| 6 | [EncoderProbePage.tsx](../frontend/src/pages/EncoderProbePage.tsx#L405) | `policyType === 'smolvla'` / `=== 'act'` **6곳** | 분기 (TSX 안) |

여기에 조건부 경고 둘이 더 붙는다
([TrainingPage:608](../frontend/src/pages/TrainingPage.tsx#L608), [777](../frontend/src/pages/TrainingPage.tsx#L777) —
`smolvla && !load_vlm_weights`).

### 이미 대가를 치렀다

- **5번이 백엔드와 갈라져 있었다.** `pi0_fast`·`tdmpc`·`vqbet` 이 백엔드 `trainable` 에는
  있는데 스키마가 없어서 **골라도 화면이 전혀 안 바뀌었다.** 주석이 그대로 적혀 있다.
- **오늘 6번과 같은 종류의 사고가 났다.** ACT 에서 입력란은 감췄는데 값은 계속 실려 가,
  입력한 적 없는 `Task: Pick up the doll...` 이 텔레메트리에 떴다. 감추는 조건과
  보내는 조건이 **다른 곳에** 적혀 있었기 때문이다.
- 1·2·3 은 이미 한 번씩 갈라졌던 것을 모아둔 결과다
  ([02-policy-registry](../refactor/02-policy-registry.md), [01-inference-params](../refactor/01-inference-params.md)).
  **같은 병이 5·6 에 남아 있다.**

> 교훈은 "한 곳에 모으자"가 아니다. 이미 세 번 모았다. 문제는 **모을 때마다
> 파이썬 dict 로 모았고, 화면 쪽은 못 따라왔다**는 것이다. 정본을 프론트도
> 읽을 수 있는 형식으로 두면 6번 같은 분기가 애초에 생길 자리가 없다.

---

## 설계 — 파일 하나 = 모델 하나

```
policies/                      ← 저장소. 배포 이미지에 들어간다
  act.yaml
  smolvla.yaml
  pi05.yaml
  _params.yaml                 ← 정책과 무관한 공통 파라미터(로봇 속도·필터)
config_dir/policies/           ← 기기별 덮어쓰기·추가 (ROADMAP "기기별 설정 분리")
  act.yaml                     ← 같은 이름이면 **깊은 병합**, 없던 이름이면 새 정책
```

`core/policies.py` 는 사라지지 않고 **로더**가 된다 — `POLICIES` dict 를 손으로 적는
대신 YAML 을 읽어 같은 모양으로 만든다. 그래서 `supported()`·`trainable()` 같은
기존 함수와 그걸 쓰는 곳은 **한 줄도 안 바뀐다.**

### 무엇을 담고 무엇을 안 담는가 — 여기가 핵심

이 기획이 실패하는 방법은 하나다: **YAML 로 UI 프레임워크를 만드는 것.**
`component:`, `col_span:`, `order:` 를 넣기 시작하면 디버깅 안 되는 JSX 가 된다.

| 담는다 (사실·필드) | 안 담는다 (표현·논리) |
|---|---|
| 이 정책이 존재하는가, 라벨은 무엇인가 | 카드를 몇 열로 배치하는가 |
| 어떤 학습 필드가 있고 범위·기본값은 얼마인가 | 어떤 React 컴포넌트를 쓰는가 |
| 어떤 추론 파라미터를 노출하는가 | 슬라이더인가 입력창인가 (`kind` 로 충분) |
| 필드가 어느 묶음(group)에 속하는가 | 묶음의 화면상 위치 |
| 언어를 받는가 / RTC 인가 / 프로브 되는가 | 그래서 무엇을 숨길지 |
| 조건부 경고 문구와 **그 조건** | 경고를 어디에 띄울지 |

**화면은 "무엇을 그릴지"를 스펙에서 받고, "어떻게 그릴지"는 자기가 정한다.**
테스트로 못 박는다 — 스펙에 표현 계층 키가 들어오면 실패시킨다.

### 스키마

```yaml
# policies/act.yaml
spec_version: 1
type: act
label: ACT
supported: true

capabilities:
  train: true
  infer: true
  rtc: false
  encoder_probe: true
  # ⚠ `vlm_base` 유무로 유추하지 않는다 — 둘은 다른 사실이고,
  # VLM 없이 언어를 받는 정책이 나오면 그 순간 유추가 거짓말이 된다.
  language: false

# wrapper 가 import 할 클래스. 지금 POLICY_IMPORTS 가 들고 있는 것.
runtime:
  model: [lerobot.policies.act.modeling_act, ACTPolicy]
  config: [lerobot.policies.act.configuration_act, ACTConfig]

train:
  defaults: { batch_size: 8, steps: 100000, optimizer_type: adam }
  fields:
    # `from_lerobot: true` 면 기본값을 **config 클래스에서 읽어** 채운다 (아래 참고)
    - { key: chunk_size,    kind: number, min: 1,  max: 200,  arch: true, from_lerobot: true }
    - { key: n_action_steps, kind: number, min: 1, max: 200,  arch: true, from_lerobot: true }
    - { key: n_obs_steps,   kind: number, min: 1,  max: 10,   arch: true, from_lerobot: true }
    - { key: dim_model,     kind: number, min: 64, max: 2048, step: 64, arch: true, from_lerobot: true }
    - { key: use_vae,       kind: bool,   arch: true, from_lerobot: true }

infer:
  # 공통 파라미터(_params.yaml)에 더해 **이 정책만** 노출하는 것
  extra_params: [temporal_ensemble_coeff]

encoder_probe:
  base_label: "베이스 ResNet-18 / ImageNet (학습 전)"
  taps:
    - { key: backbone, label: "ResNet-18 최종 특징맵", default: true }
  note: |
    ACT 의 ResNet 백본은 무작위가 아니라 ImageNet 사전학습으로 시작한다.
    그 시작점을 봐야 "학습이 엔코더를 좋게 만들었나"를 잴 기준이 생긴다.
```

```yaml
# policies/smolvla.yaml  (발췌)
train:
  fields:
    - key: load_vlm_weights
      kind: bool
      from_lerobot: true
      override:
        value: true
        # ⚠ **의도적으로 LeRobot 과 다르다.** 상류 기본값은 false 인데,
        # 처음부터 학습할 때 false 면 VLM 이 랜덤 초기화되고
        # freeze_vision_encoder=true 와 겹쳐 **학습 자체가 안 된다.**
        reason: "LeRobot 기본값 false 는 scratch 학습에서 freeze 와 충돌한다"

  warnings:
    - when: { field: load_vlm_weights, is: false }
      unless_set: pretrained_path
      level: warn
      text: "VLM 가중치를 안 불러오면 비전 인코더가 랜덤에서 시작합니다."
```

### 조건 문법은 **일부러 빈약하게** 둔다

`when` 은 `{field, is}` / `{field, lt|gt, value}` 정도만 받는다. 임의 표현식을 허용하면
YAML 안에 프로그램이 생기고, 그건 타입 검사도 테스트도 안 되는 코드다.

**표현이 안 되면 그건 진짜 로직이라는 신호다** — TSX 에 남기고 주석으로 왜 남았는지 적는다.
못 담는 것이 하나쯤 있는 편이 억지로 담아 문법을 키우는 것보다 낫다.

---

## LeRobot 을 정본으로 — 손으로 베끼면 같은 병이 한 층 위로 올라간다

지금 `POLICY_TRAIN_SCHEMAS` 의 기본값은 **LeRobot config 클래스를 사람이 보고 옮긴 것**이다
(주석에 그렇게 적혀 있다). YAML 로 옮기기만 하면 드리프트가 형식만 바뀌어 남는다.

확인해봤더니 **읽어올 수 있다**:

```python
dataclasses.fields(ACTConfig)
# chunk_size=100, n_action_steps=100, n_obs_steps=1, dim_model=512, use_vae=True
dataclasses.fields(SmolVLAConfig)
# chunk_size=50, n_action_steps=50, freeze_vision_encoder=True, load_vlm_weights=False
```

그리고 **하나가 이미 어긋나 있다**: `load_vlm_weights` 는 LeRobot 이 `False` 인데
화면은 `true` 다. 의도된 차이고 이유도 주석에 있다 — 그래서 스펙에
`override.value` + `override.reason` 을 둔다. 베끼기와 **의도적 이탈**을 구분해야 한다.

| | 방법 |
|---|---|
| 생성 | `tools/gen_policy_spec.py` 가 `from_lerobot: true` 필드의 기본값을 config 클래스에서 읽어 채운다 |
| 검증 | 테스트가 다시 읽어 비교. 다르면 실패 — **LeRobot 업그레이드에서 기본값이 바뀐 걸 알게 된다** |
| 이탈 | `override.value` 가 있으면 그 값을 쓰되, **상류가 바뀌면 알린다** ("false→true 로 바뀜, override 재검토") |

이게 이 기획에서 제일 값나가는 부분이다. LeRobot 을 안 고친다는 원칙 아래에서
**상류 변화를 조용히 놓치지 않는 유일한 방법**이다.

---

## API / 렌더링

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/policies` | **기존 그대로.** 응답에 `train`/`infer`/`ui` 절이 추가될 뿐 |
| `GET` | `/api/policies/{type}/ui` | 그 정책의 화면 스펙 (필드·묶음·경고) |

프론트는 `usePolicies()` 를 그대로 쓰고 훅 하나가 는다:

```ts
const { fields, warnings } = usePolicyUi(policyType)   // 없으면 빈 배열
```

렌더는 **공용 폼 컴포넌트 하나**가 맡는다:

```tsx
<SpecFields spec={fields} values={policyParams} onChange={setPolicyParams} />
```

`TrainingPage` 는 `POLICY_TRAIN_SCHEMAS` 400줄이 사라지고 `<SpecFields>` 한 줄이 된다.
`EncoderProbePage` 의 분기 6개는 `spec.encoder_probe.taps` 순회로 바뀐다.

### 실패해도 화면이 안 깨져야 한다

| 상황 | 동작 |
|---|---|
| YAML 파싱 실패 | **그 정책만 목록에서 빠진다** + 로그. 반쯤 그려진 폼을 띄우지 않는다 |
| `spec_version` 이 미래 값 | 같은 처리 — 모르는 스키마를 짐작해서 그리지 않는다 |
| 필드에 `min/max` 없음 | 숫자 입력창으로 (슬라이더는 범위를 알아야 한다) |
| 스펙 API 가 응답 없음 | 정책 선택은 되고 **파라미터 패널만 비운다.** 시작은 막지 않는다 |

마지막 줄이 중요하다 — 스펙은 편의 계층이지 안전 계층이 아니다.
값 검증의 정본은 백엔드 `PARAM_SPEC` 클램프와 `param_bridge` 로 남는다.

---

## 작업 분해

| # | 작업 | 선행 |
|---|---|---|
| 1 | `policies/*.yaml` + 로더 — `core/policies.py` 를 dict 에서 **로더**로. `POLICIES` 모양과 공개 함수는 그대로 | — |
| 2 | `tools/gen_policy_spec.py` + 상류 대조 테스트 (`from_lerobot`·`override`) | 1 |
| 3 | `train.fields` 를 스펙으로 — `POLICY_TRAIN_SCHEMAS` 삭제, `<SpecFields>` 신설 | 1, 2 |
| 4 | `runtime.model/config` 로 `POLICY_IMPORTS` 대체 (wrapper 가 같은 YAML 을 읽는다) | 1 |
| 5 | `encoder_probe` 절 — `EncoderProbePage` 분기 6개와 `--tap` 선택지 제거 | 1, 3 |
| 6 | `infer.extra_params` 로 `PARAM_SPEC` 의 `policies:` 역전 | 1, 3 |
| 7 | `warnings` — 조건 문법 + 렌더. 표현 안 되는 것은 TSX 에 남기고 이유를 적는다 | 3 |
| 8 | `config_dir/policies/` 덮어쓰기·추가 | 1 |

**3번까지가 이 기획의 8할이다.** 4~6 은 같은 스펙을 다른 소비자가 읽는 일이라
붙이기만 하면 되고, 7~8 은 없어도 이득이 난다.

---

## 검증

```bash
cd backend && pytest tests/test_policy_spec.py -v
cd frontend && npm run build     # 타입 검증은 반드시 build
```

테스트로 잠글 것:

1. **상류 대조** — `from_lerobot` 필드의 기본값이 지금 LeRobot 값과 같은가.
   `override` 가 있으면 상류가 바뀐 것만 알린다
2. **표현 계층 금지** — 스펙에 `component`/`layout`/`col`/`order` 키가 없는가
3. **TSX 에 정책 이름이 안 박혀 있는가** — `policyType === '...'` 문자열 비교 0건
   (오늘 사고가 이 종류였다). AST 로 판정한다 — 주석이 걸리면 안 된다
4. **깨진 YAML 은 그 정책만 빠지는가** — 목록 API 가 200 을 주는가
5. `supported()`/`trainable()`/`inferable()` 결과가 **이관 전후 동일**한가 (골든 테스트)

실기:

- ACT·SmolVLA 로 학습을 걸어 **이관 전과 같은 인자**가 나가는가 (`preview` 비교)
- `config_dir/policies/act.yaml` 로 `steps` 기본값만 덮어쓰고 화면에 반영되는가
- YAML 을 일부러 깨뜨리고 나머지 정책이 그대로 도는가

---

## 먼저 정해야 할 것

| 항목 | 선택지 | 권장 |
|---|---|---|
| 형식 | YAML / TOML / JSON | **YAML** — 주석이 달린다. 이 저장소는 "왜"를 코드 옆에 적는다 |
| 파일 단위 | 정책당 1파일 / 전체 1파일 | **정책당 1파일** — 추가가 곧 파일 추가라 diff 가 깨끗하다 |
| 파서 | `pyyaml` 추가 / JSON 으로 타협 | **`pyyaml`** (백엔드 의존성 1개) |
| 검증 | 손수 / `pydantic` | **`pydantic`** — 이미 있고, 실패 메시지가 사람이 읽을 만하다 |
| 프론트 전달 | 파일 노출 / API | **API** — 병합·검증·기본값 채우기가 이미 백엔드에 있다 |
| 미지원 정책 | 파일 삭제 / `supported: false` | **`supported: false`** — 지금 규칙 그대로 |

---

## 범위 밖

- **녹화 화면** — 정책과 무관하다 (데이터 수집은 모델을 모른다)
- **로봇·카메라 설정** — 이미 프리셋 스토어가 있다 ([parameter-presets](parameter-presets.md))
- **정책별 후처리 파이프라인** — 스무딩·RTC 는 파라미터지 UI 구조가 아니다
- **LeRobot 수정** — 여전히 안 한다. 읽기만 한다

---

## 상태

☑ **완료** (2026-08-14). 8단계 전부.

| # | 작업 | |
|---|---|---|
| 1 | `policies/*.yaml` + 로더 | ☑ [policy_spec.py](../backend/app/core/policy_spec.py), [policies.py](../backend/app/core/policies.py) |
| 2 | LeRobot 생성기 + 상류 대조 | ☑ [tools/gen_policy_spec.py](../tools/gen_policy_spec.py) |
| 3 | 학습 필드 → `<SpecFields>` | ☑ `TrainingPage` **125줄 삭제 / 45줄 추가** |
| 4 | `runtime` 으로 `POLICY_IMPORTS` 대체 | ☑ [wrapper/policy_registry.py](../wrapper/policy_registry.py) — `lerobot_bootstrap` 의 세 번째 사본도 같이 흡수 |
| 5 | `encoder_probe` 절로 프로브 분기 제거 | ☑ `EncoderProbePage` 6곳 · wrapper `--tap` 선택지·기본값 |
| 6 | `infer.extra_params` 로 `PARAM_SPEC` 역전 | ☑ 파라미터 표가 정책 이름을 더 이상 모른다 |
| 7 | 조건부 경고 | ☑ 3단계에 딸려 왔다 (`when`/`and`) |
| 8 | 기기별 덮어쓰기 | ☑ 로더가 지원, 실기 확인 |

### 생성기가 첫 실행에서 잡은 것

**옛 화면 테이블이 LeRobot 에 없는 필드 둘을 노출하고 있었다.**

| 정책 | 스키마에 있던 것 | 진짜 |
|---|---|---|
| `pi0_fast` | `freeze_vision_encoder` | **없음** (PI0FastConfig 에 그런 필드가 없다) |
| `vqbet` | `n_action_steps` | `action_chunk_size` / `n_action_pred_token` |

둘 다 지금 `supported: false` 라 아무도 안 밟았지만, 켰으면 **학습 시작에서 알 수 없는
설정 키로 죽었을 것이다.** 손으로 베꼈으면 YAML 에도 그대로 옮겨 적었을 값이다.

### 처음에 센 여섯 곳, 지금

| # | 위치 | |
|---|---|---|
| 1 | `POLICIES` | ☑ 로더 |
| 2 | `PARAM_SPEC` 의 `policies:` | ☑ `infer.extra_params` (방향을 뒤집었다) |
| 3 | `POLICY_IMPORTS` | ☑ `runtime` |
| 4 | 프로브 `--tap` | ☑ `encoder_probe.taps` |
| 5 | `POLICY_TRAIN_SCHEMAS` | ☑ `train.fields` |
| 6 | `EncoderProbePage` 분기 6개 | ☑ `encoder_probe` |

덤으로 `lerobot_bootstrap._CONFIG_IMPORTS`(세 번째 사본)와
`SCRATCH_WEIGHTS`(TSX 테이블)도 흡수했다.

### 파일 하나로 정책이 추가되는가 — 실기로 확인

`config_dir/policies/faketest.yaml` **한 장만** 놓고 게이트웨이를 올렸다.
파이썬·TSX 는 한 줄도 안 건드렸다.

| 확인 | 결과 |
|---|---|
| 정책 목록 | ☑ `FakeTest` 가 뜬다 (`rtc`·`language` 플래그 반영) |
| 학습 스펙 | ☑ `batch_size: 16, steps: 12345` + `chunk_size` 필드 |
| 추론 파라미터 게이팅 | ☑ `max_guidance_weight → [faketest, pi0, pi05, smolvla]` |
| wrapper 클래스 경로 | ☑ 같은 파일에서 읽는다 |

이게 이 작업의 목적이었다.

### 안전 계약은 안 옮겼다

파라미터의 **정의**(범위·실시간 여부)는 `core/inference_params.py` 에 남는다 —
클램프와 버스 화이트리스트가 거기서 나오는 안전 계약이기 때문이다.
정책 파일은 **"누가 그걸 쓰는가"만** 말한다. 합쳤으면 정책 YAML 이 관절 속도 상한을
정하게 됐을 것이다.

`PARAM_SPEC` 에는 `scoped: True` 만 남는다 — "전부에 해당하진 않는다"는 표시다.
정책 **이름**은 없다. 응답 모양(`policies: [...]`)은 그대로라 프론트는 안 바뀌었다.

### 두 판을 두고 결과를 대조한다

wrapper 는 게이트웨이를 import 할 수 없다 — 다른 파이썬에서 돌고 컨테이너로도
나간다. 그래서 [policy_registry.py](../wrapper/policy_registry.py) 가 **읽기만** 하는
얇은 판을 따로 갖는다. 검증·병합은 백엔드가 하고, **정본이 같은 파일이라 갈라질 수 없다** —
테스트가 두 판을 다 불러 결과를 비교한다.

이 통합이 오래된 질문 하나를 없앴다. `lerobot_bootstrap` 은 config 6개,
`lerobot_wrapper` 는 8개를 손으로 들고 있었고, **왜 다른지 아무도 몰라서**
"임의로 맞추지 않는다 — 바꾸려면 실기로 확인해야 한다"고 적혀 있었다.
둘 다 같은 표에서 나오게 하니 질문 자체가 사라졌다.

### 일부러 안 옮긴 것

`encoder_probe.py` 의 `run_smolvla` / `run_act` 갈림은 **남겼다.**
아키텍처마다 특징 뽑는 코드가 다르고 그건 데이터가 아니라 로직이다 —
이 문서가 "표현이 안 되면 그건 진짜 로직이라는 신호"라고 쓴 바로 그 경우다.

### 실기로 확인한 것

- **깨진 파일 격리** — `act.yaml` 을 망가뜨리니 목록에 SmolVLA 만 남고
  `정책 스펙 파싱 실패, 건너뜀: act.yaml` 이 찍혔다. 목록이 통째로 비지 않는다
- **기기별 덮어쓰기** — `config_dir/policies/act.yaml` 에 `steps: 777` 만 적으니
  그것만 바뀌고 `batch_size` 와 필드 5개는 그대로였다
- **학습 인자 불변** — 미리보기 명령이 이관 전과 같다
- **프로브 폴백** — ACT 에 `tap=siglip`(ACT 엔 없는 값)을 보내니 스펙의 `backbone`
  으로 떨어졌다. 예전 동작 그대로인데 이제 **하드코딩이 아니라 유도된** 값이다
- **wrapper 기동** — `lerobot_wrapper.py --help` 와 부트스트랩 import 가 깨끗하고,
  config 클래스 8개가 전부 등록된다

> 이 문서를 쓴 계기: ACT 추론에서 입력한 적 없는 task 가 뜬 사고.
> 원인은 **감추는 조건과 보내는 조건이 다른 파일에 있었던 것**이고,
> 모델이 늘수록 그런 쌍이 늘어난다. 지금 8개 정책에 6곳이면
> 정책 하나당 자리가 6개씩 생긴다는 뜻이다.
