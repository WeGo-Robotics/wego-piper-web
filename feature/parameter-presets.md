# 파라미터 프리셋 — 추론·학습 설정을 이름 붙여 저장

추론 슬라이더 값과 학습 하이퍼파라미터를 **이름 붙인 프리셋**으로 저장하고 불러온다.
로봇 프리셋([robot_manager.py:790-838](../backend/app/services/robot_manager.py#L790-L838))이 이미 하는 일을
파라미터 도메인으로 넓히는 것이다.

---

## 왜 필요한가

### 지금은 "이름 없는 프리셋 1개"가 브라우저에 있다

| 저장 대상 | 어디에 | 이름 | 문제 |
|---|---|---|---|
| 로봇 CAN 구성 | `config_dir/presets/*.json` | **있음** | — (선례) |
| 카메라 세션 | `config_dir/camera_session.json` | 없음 | 값이 아니라 등록 상태만 |
| **추론 파라미터** | `localStorage['piper_inference_params']` | 없음 | 아래 |
| **학습 설정** | `localStorage['piper_train_settings']` | 없음 | 아래 |
| 녹화 설정 | `localStorage['piper_record_settings']` | 없음 | 아래 |
| 정책 서버 주소 | `localStorage['ps_mode' / 'ps_remote_addr']` | 없음 | 아래 |

localStorage 저장의 실제 문제:

- **브라우저를 바꾸면 사라진다.** 다른 PC나 태블릿에서 접속하면 튜닝이 없다
- **로봇마다 인스턴스**인데 설정이 브라우저에 있으면 로봇↔설정 대응이 깨진다.
  [ROADMAP](../ROADMAP.md)의 *"기기별 상태는 `config_dir` 하나로"* 결정과 정면으로 어긋난다
- 백업·이관 단위 밖이다
- 팀에서 공유할 수 없다 ("현장 조명에서 쓰는 값" 같은 것을 넘겨줄 방법이 없다)

### 더 큰 이유 — 재현성

[eval_log](../backend/app/routers/eval_log.py#L19-L23)가 기록하는 것은
`success` / `checkpoint` / `memo` 뿐이다. **어떤 파라미터로 돌린 추론인지가 안 남는다.**

> "이 체크포인트 성공률 70%" → 그 70%가 어느 속도·필터·청크 설정에서 나온 것인지 알 수 없다.

프리셋에 ID가 생기면 평가 기록에 `preset_id` 를 같이 남길 수 있고,
**"어느 설정이 잘 됐나"** 를 비교할 수 있다. 이게 단순 편의보다 큰 값어치다.

학습 쪽도 같다 — 어떤 하이퍼파라미터로 학습한 체크포인트인지가 지금은 `output_dir` 이름에만 있다.

---

## 설계

### 하나의 스토어, 도메인별 스키마

지금 프리셋 성격의 저장소가 여섯 군데인데 CRUD·UI 모양이 전부 같다.
[camera-profiles.md](camera-profiles.md)도 **또 하나를 독립 구현하려 하고 있다.**
따로 만들면 일곱 번째 사본이 된다.

```
backend/app/services/presets.py       공통 스토어 (CRUD · 검증 · 마이그레이션)
config_dir/presets/
  robot/<name>.json                   ← 기존 것을 여기로 이동
  inference/<name>.json
  training/<name>.json
  camera/<name>.json                  ← camera-profiles.md 가 여기로
```

```python
@dataclass
class Preset:
    domain: str          # "inference" | "training" | "camera" | "robot"
    name: str
    scope: str           # "device" | "shared"   (아래)
    version: int         # 스키마 버전 (마이그레이션용)
    updated_at: str
    note: str            # "야간 조명, 큐브 태스크" 같은 메모
    values: dict         # 도메인 스키마가 정의하는 값들
```

도메인마다 다른 것은 **`values` 의 스키마와 검증**뿐이다. 나머지는 공유한다.

### ⚠ 무엇을 담고 무엇을 안 담는가 — 여기가 핵심

프리셋이 재사용되려면 **"튜닝 값"과 "실행 대상"을 반드시 분리**해야 한다.
체크포인트 경로나 CAN 인터페이스 이름을 담으면 다른 장비·다른 모델에서 못 쓴다.

| 도메인 | 담는다 (튜닝) | 담지 않는다 (실행 대상) |
|---|---|---|
| **추론** | fps, max_velocity, max_gripper_velocity, lowpass_alpha, max_jerk, interpolation_steps, use_chunk_size, refill_threshold_pct, gripper_bypass_filter, RTC/ACT 파라미터, smoothing 계열 | `checkpoint_path`, `camera_mapping`, `robot_port`, `server_address` |
| **학습** | batch_size, steps, optimizer_type, learning_rate, save_freq, num_workers, seed, amp, `policy_params` 전체 | `dataset_repo_id`, `output_dir`, `pretrained_path`, `policy_repo_id` |

`task` 텍스트는 애매하다 — 태스크별로 파라미터를 다르게 쓰는 경우가 많으므로
**선택적으로 포함**(체크박스)하는 게 실용적이다.

> 지금 `piper_train_settings` 는 `dataset_repo_id` 까지 통째로 저장한다.
> 프리셋으로 옮기면서 이 경계를 그어야 한다.

### `policy_type` 은 프리셋의 일부다

학습 파라미터는 [`POLICY_TRAIN_SCHEMAS`](../frontend/src/pages/TrainingPage.tsx#L32)가
정책마다 다르다 (`act` 의 `use_vae` 는 `diffusion` 에 없다).
추론도 RTC 파라미터가 flow-matching 정책 전용이다.

→ 프리셋에 `policy_type` 을 기록하고, **다른 정책에 로드하면 공통 항목만 적용하고
나머지는 무시했다고 표시**한다. 조용히 버리면 안 된다.

### scope — 기기별인가 공용인가

[ROADMAP](../ROADMAP.md)에서 **로봇마다 별도 인스턴스**로 정해졌다. 그러면 프리셋도 갈린다.

| | scope | 이유 |
|---|---|---|
| 추론 속도·필터 | **device** | 팔마다 캘리브레이션·기계적 특성이 다르다. 한 대에서 맞춘 `max_velocity` 를 다른 대에 그대로 쓰면 안 된다 |
| 추론 청크·RTC | shared | 정책 특성이라 기기와 무관 |
| 학습 전부 | **shared** | 기기와 무관. 오히려 로봇 간 공유돼야 한다 |
| 카메라 프로파일 | **device** | 조명·렌즈가 현장마다 다르다 |

`shared` 프리셋은 나중에 Hub나 사내 서버로 동기화할 여지를 남긴다.
`device` 프리셋은 그 기기의 `config_dir` 에만 있고 백업 단위에 포함된다.

---

## ⚠ 선행 의존 — PARAM_SPEC 없이 만들면 안 된다

[01-inference-params.md](../refactor/01-inference-params.md) 단계 2의 `PARAM_SPEC`
(파라미터 이름·범위·기본값·타입의 단일 소스)이 **이 기능의 전제조건이다.**

없으면 프리셋 저장/로드 코드가 파라미터 목록을 자체로 알아야 하고,
그게 **다섯 번째 사본**이 된다. 지금 고친 문제를 그대로 재생산한다.

`PARAM_SPEC` 이 있으면 공짜로 따라오는 것:

- **검증** — 범위 밖 값이 든 프리셋을 로드할 때 클램프 + 경고
- **마이그레이션** — 프리셋에 없는 새 파라미터는 기본값으로 채우고,
  없어진 파라미터는 무시했다고 표시
- **UI 자동 생성** — 프리셋 편집 화면을 스펙에서 렌더

단계 2를 안 기다리고 시작하려면 **추론은 미루고 학습부터** 하는 방법이 있다 —
학습 파라미터는 이미 [`POLICY_TRAIN_SCHEMAS`](../frontend/src/pages/TrainingPage.tsx#L32)라는
단일 소스가 프론트에 있다. (다만 그것도 백엔드 `TrainStartRequest` 와 이중이다.)

---

## API

로봇 프리셋([robots.py:452-486](../backend/app/routers/robots.py#L452-L486))과 같은 모양을 도메인 파라미터로 일반화한다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/presets/{domain}` | 목록 (이름·scope·policy_type·updated_at·note) |
| `GET` | `/api/presets/{domain}/{name}` | 값 전체 |
| `POST` | `/api/presets/{domain}` | 저장 `{name, scope, values, note, policy_type?}` |
| `PUT` | `/api/presets/{domain}/{name}` | 덮어쓰기 |
| `DELETE` | `/api/presets/{domain}/{name}` | 삭제 |
| `POST` | `/api/presets/{domain}/{name}/apply` | 검증 후 적용 결과 반환 (클램프·누락·무시된 키) |

`apply` 가 값만 돌려주지 않고 **적용 리포트**를 함께 주는 게 중요하다:

```jsonc
{ "values": { "max_velocity": 250, ... },
  "clamped":  [{ "key": "max_velocity", "saved": 800, "applied": 500 }],
  "missing":  ["새로_추가된_파라미터"],      // 기본값으로 채움
  "ignored":  ["use_vae"],                  // 다른 policy_type 전용
  "policy_mismatch": { "saved": "act", "current": "smolvla" } }
```

조용히 버리면 "저장한 값이 왜 다르지"가 된다. 지금 고친 유실 버그와 같은 형태다.

---

## UI

- **추론 페이지**: 파라미터 카드 상단에 `프리셋 [야간-큐브 ▾] [저장] [다른 이름으로] [삭제]`.
  현재 값이 프리셋과 다르면 `● 수정됨` 배지
- **학습 페이지**: 같은 모양. 정책 타입을 바꾸면 호환 프리셋만 목록에 표시
- 목록 UI는 로봇 프리셋([RobotsPage.tsx:465-486](../frontend/src/pages/RobotsPage.tsx#L465-L486))
  패턴을 그대로 재사용
- 적용 리포트에 `clamped`/`ignored` 가 있으면 토스트로 표시 (실패가 아니라 정보)

**localStorage 는 유지한다** — "마지막으로 쓰던 값"의 자동 복원은 프리셋과 다른 기능이다.
프리셋을 로드하면 localStorage 도 갱신된다. 다만 `dataset_repo_id` 처럼
실행 대상에 해당하는 것은 프리셋으로 옮기지 않는다.

---

## 작업 분해

| # | 작업 | 선행 |
|---|---|---|
| 1 | ☑ [services/presets.py](../backend/app/services/presets.py) + `/api/presets/{domain}` CRUD | — |
| 2 | ☑ 로봇 프리셋 이관 (기동 시 1회, 재실행 안전) | 1 |
| 3 | ☑ 학습 프리셋 — 키를 `TrainStartRequest` 에서 파생 + [PresetBar](../frontend/src/components/PresetBar.tsx) | 1 |
| 4 | ☑ 추론 프리셋 — 키·범위 모두 `PARAM_SPEC` 파생, 범위 밖 값 클램프 | ☑ #1 단계 2 완료 |
| 5 | ☑ `eval_log` 에 `preset`·`params`·`robot_id` + 프리셋/체크포인트별 성공률 | 4 |
| 6 | 카메라 프로파일을 이 스토어로 흡수 | 1, [camera-profiles](camera-profiles.md) |

2번을 초반에 하는 이유: **기존 것을 흡수하지 않으면 프리셋 시스템이 두 개가 된다.**
새 도메인만 새 스토어에 넣고 로봇은 그대로 두면 정확히 그 상태가 된다.

---

## 검증

- `cd frontend && npm run build`
- 프리셋 저장 → 서버 재시작 → 목록·값이 유지되는가
- **다른 브라우저에서 접속** → 같은 프리셋이 보이는가 (localStorage 대비 핵심 이득)
- 범위 밖 값이 든 프리셋을 손으로 만들어 로드 → 클램프되고 리포트에 뜨는가
- `act` 프리셋을 `smolvla` 에서 로드 → 공통 항목만 적용되고 `ignored` 에 표시되는가
- 파라미터를 하나 추가한 뒤 옛 프리셋 로드 → 기본값으로 채워지고 `missing` 에 뜨는가
- 로봇 프리셋 이관 후 기존 프리셋이 그대로 보이는가 (마이그레이션 1회)
- 프리셋 적용 후 실제 추론을 돌려 wrapper 로그의 값이 프리셋과 일치하는가

---

## 먼저 정해야 할 것

| 항목 | 선택지 | 권장 |
|---|---|---|
| 추론 프리셋 착수 시점 | `PARAM_SPEC` 후 / 지금 자체 목록으로 | **PARAM_SPEC 후** — 아니면 다섯 번째 사본 |
| `task` 텍스트 포함 | 항상 / 선택 / 제외 | **선택** (체크박스) |
| 기본 scope | device / shared | 추론=**device**, 학습=**shared** |
| 프리셋 간 상속 | 지원 / 미지원 | **미지원** — "기본값 + 델타"는 복잡도 대비 이득이 작다 |
| 로봇 프리셋 이관 | 지금 / 나중 | **지금** (작업 2) — 미루면 시스템이 둘로 갈린다 |

---

## 상태

◐ 1~5단계 완료 — 6(카메라 프로파일 흡수)만 남음 (camera-profiles 착수 시)
