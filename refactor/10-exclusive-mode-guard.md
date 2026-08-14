# 10. 배타 모드 가드 8곳, 각각 다른 부분집합 (A급)

## 문제

[CLAUDE.md](../CLAUDE.md)는 *"GPU 메모리 경합: 학습과 추론 동시 실행 시 OOM 위험 → **모드를 배타적으로 관리**"* 를
안전 주의사항으로 못 박고 있다. 그런데 그 "배타"가 **엔드포인트마다 직접 손으로 적혀 있고, 전부 다른 부분집합**이다.

| 위치 | 추론 | 학습 | 녹화 | policy_server |
|---|:--:|:--:|:--:|:--:|
| [models.py:261](../backend/app/routers/models.py#L261) `/inference/start` | 자신 | ✅ | ❌ | ❌ |
| [models.py:327](../backend/app/routers/models.py#L327) `/inference/start-custom` | ❌ | ❌ | ❌ | ❌ |
| [training.py:113](../backend/app/routers/training.py#L113) `/training/start` | ✅ | 자신 | ❌ | ❌ |
| [training.py:147](../backend/app/routers/training.py#L147) `/training/start-custom` | ✅ | 자신 | ❌ | ❌ |
| [recording.py:64](../backend/app/routers/recording.py#L64) `/recording/start` | ✅ | ✅ | 자신 | ❌ |
| [encoder.py:49](../backend/app/routers/encoder.py#L49) `_gpu_busy()` | ✅ | ✅ | ❌ | ❌ |
| [cameras.py:24](../backend/app/routers/cameras.py#L24) `_camera_owner()` | ✅ | 해당없음 | ✅ | ❌ |
| [policy_server.py:44](../backend/app/routers/policy_server.py#L44) `/policy-server/start` | ❌ | ❌ | ❌ | 자신 |

같은 규칙("무엇과 무엇이 동시에 못 돈다")이 8곳에 흩어져 있고, **어느 두 곳도 같지 않다.**

## 이미 어긋난 것

### (1) 녹화 중에 학습·추론을 시작할 수 있다

`training.py`와 `models.py` 어디에도 `record_manager.is_running` 검사가 없다.
녹화는 카메라와 로봇 CAN을 쥐고 있고 비디오 인코딩으로 GPU도 쓴다.

### (2) `/inference/start-custom`에는 가드가 아예 없다

[models.py:326-343](../backend/app/routers/models.py#L326-L343)은 학습·녹화·추론 무엇도 확인하지 않는다.
게다가 [line 333](../backend/app/routers/models.py#L333)에서 **무조건 `_release_all_cameras()`를 호출**한다 —
녹화가 돌고 있어도 카메라를 뺏는다. `/inference/start`(가드 있음)와 같은 일을 하는 엔드포인트인데
한쪽만 보호된다.

### (3) policy_server는 어느 가드에도 없다

gRPC 정책 서버는 GPU에 정책을 올린다. 그런데 학습 시작 가드가 이걸 확인하지 않고,
반대로 정책 서버 시작도 학습 여부를 확인하지 않는다. CLAUDE.md가 경고한 바로 그 OOM 조합이다.

### (4) 사유 문자열도 제각각

같은 상황에 `"추론이 실행 중입니다."`([recording.py:65](../backend/app/routers/recording.py#L65))와
`"추론이 실행 중입니다. 추론을 먼저 중지하세요."`([training.py:114](../backend/app/routers/training.py#L114))가 섞여 있다.

## 해결 (☑ 완료)

**[`backend/app/services/exclusivity.py`](../backend/app/services/exclusivity.py)** 에 표 하나로 모았다.
(`core/` 가 아니라 `services/` 인 이유: `core/` 는 지금까지 `services/` 를 import 한 적이 없고,
매니저를 참조해야 하므로 방향을 뒤집으면 순환이 생긴다.)

### 확정된 표

| 활동 | 이것을 시작하려면 아래가 멈춰 있어야 | 근거 |
|---|---|---|
| `INFERENCE` | 학습 · 녹화 · 데이터셋 편집 | GPU + 카메라 + CAN |
| `RECORDING` | 학습 · 추론 · 데이터셋 편집 | GPU + 카메라 + CAN |
| `TRAINING` | 추론 · 녹화 · 정책 서버 | GPU |
| `POLICY_SERVER` | 학습 | GPU |
| `DATASET_EDIT` | 추론 · 녹화 | 디스크 + 프로세스 |
| `UPLOAD` | — | 네트워크만 |
| `ENCODER_PROBE` | (학습 · 추론) | **막지 않고 CPU 폴백** |
| `CAMERA_ACCESS` | (추론 · 녹화) | 장치 점유 |

상태를 가진 활동 사이에서는 **대칭**이다 (A가 B를 막으면 B도 A를 막는다).
`추론 ↔ 정책 서버`는 일부러 서로 안 막는다 — 서버 모드 추론이 정책 서버를 필요로 한다.

### 함수가 셋인 이유

`require_idle()` 하나로는 부족했다. 기존 가드 둘이 **409를 던지지 않는다**:

- `encoder.py:_gpu_busy()` → 사유를 돌려주고 `device = "cpu" if busy else "cuda"` **CPU 폴백**
- `cameras.py:_camera_owner()` → `/scan` 에서 **캐시 반환 조건**으로도 쓰인다

| 함수 | 용도 |
|---|---|
| `blocking(target)` | 막고 있는 활동 목록 (raise 없음) |
| `blocked_reason(target)` | 한글 사유. encoder · cameras 가 쓴다 |
| `require_idle(target)` | 409. 메시지·조사 처리가 여기 한 곳에만 있다 |
| `snapshot()` | `GET /api/activity` — 프론트가 소비 |

### 함께 고친 것

- **`STOPPING` 을 실행 중으로 통일** — SIGTERM 을 보낸 뒤에도 프로세스는 살아 카메라·CAN·GPU 를
  쥐고 있다. 기존 가드들이 `not in (idle, error)` 와 `in (running, starting)` 로 갈려 있었다
- **데이터셋 편집에 전용 `ProcessManager`** ([`services/dataset_jobs.py`](../backend/app/services/dataset_jobs.py)) —
  추론과 같은 전역 인스턴스를 써서 프로세스 핸들을 덮어쓰던 것을 분리
- **프론트엔드 4번째 사본 제거** — [`useActivity`](../frontend/src/hooks/useActivity.ts) 훅이
  `/api/activity` 를 읽고, 세 페이지의 `canStart` 가 그걸 쓴다. 새 WS 타입을 추가하지 않았다
  (기존 `*_state` 메시지가 오면 재조회) → [12-ws-message-contract.md](12-ws-message-contract.md) 와 충돌 없음

### 원래 초안 (참고)

`core/exclusivity.py`에 **"무엇이 무엇을 막는가"를 선언 하나로**:

```python
class Activity(str, enum.Enum):
    INFERENCE = "inference"
    TRAINING = "training"
    RECORDING = "recording"
    POLICY_SERVER = "policy_server"
    ENCODER_PROBE = "encoder_probe"
    CAMERA_ACCESS = "camera_access"

# 이 활동을 시작하려면 아래 활동들이 멈춰 있어야 한다
BLOCKED_BY = {
    Activity.TRAINING:      [INFERENCE, RECORDING, POLICY_SERVER],  # GPU
    Activity.INFERENCE:     [TRAINING, RECORDING],                  # GPU + 카메라 + CAN
    Activity.RECORDING:     [TRAINING, INFERENCE],                  # 카메라 + CAN
    Activity.POLICY_SERVER: [TRAINING],                             # GPU
    Activity.ENCODER_PROBE: [TRAINING, INFERENCE],                  # GPU
    Activity.CAMERA_ACCESS: [INFERENCE, RECORDING],                 # 장치 점유
}

def require_idle(target: Activity) -> None:
    """막는 활동이 돌고 있으면 409."""
```

각 엔드포인트는 `require_idle(Activity.TRAINING)` 한 줄이 된다.
표가 코드 안에 있으므로 "무엇이 무엇을 막는가"를 한 곳에서 읽고 고칠 수 있다.

**결정됨**: 녹화 중 학습은 **금지**. 녹화는 비디오 인코딩으로 GPU 를 쓸 수 있고,
학습이 GPU 를 먹으면 프레임 드랍·인코딩 실패로 에피소드가 망가진다. 데이터 품질을 우선한다.
E-stop 은 **로봇을 움직이는 것 전부**(추론 + 녹화)를 정지시킨다.

## 주의 — 데몬 분리와의 관계

[daemon-split.md](daemon-split.md)로 가면 이 활동들이 **각각 다른 프로세스**가 된다.
`is_running`을 파이썬 객체 속성으로 읽던 것이 버스 상태 조회로 바뀌므로,
`exclusivity.py`는 그때 `piper_bus/`의 상태 키를 읽는 형태로 옮겨간다.
지금 한 곳으로 모아두면 그 이행이 한 파일 수정으로 끝난다.

E-stop의 사정도 같다 — daemon-split.md 미결정 #2("E-stop이 무엇을 죽이는가")와
이 표는 같은 사실의 양면이다. 함께 정하는 게 맞다.

## 검증

자동 (`cd backend && pytest tests/ -q` — **30개 통과**, 이 저장소의 첫 테스트다):

- [`test_exclusivity.py`](../backend/tests/test_exclusivity.py) — 표의 불변식.
  **대칭성 검사**가 핵심이다. 표를 고칠 때 한쪽만 고치는 것이 정확히 이 문제를 만든 사고였다
- [`test_exclusivity_wiring.py`](../backend/tests/test_exclusivity_wiring.py) — 라우터가 실제로
  규칙을 부르는지. 표가 맞아도 라우터가 안 부르면 소용없다

프론트: `cd frontend && npm run build` ☑

실기 확인이 남은 것 (하드웨어 필요):

- 녹화 중 학습·추론 시작 → 409 ☑(TestClient) / 실기 미확인
- **녹화 중 `/inference/start-custom` 이 카메라를 안 뺏는지** — 409 는 확인했으나
  실제 카메라가 유지되는지는 실기 확인 필요
- 추론 중 인코더 프로브가 409 없이 **CPU 로 폴백**되는지
- **추론 중 E-stop → 정지. 녹화 중 E-stop → 정지** (새 동작)
- UI: 녹화 중 학습 페이지 시작 버튼 비활성 + "녹화 실행 중" 표시

## 남은 것

- ~~**클라우드 학습 예외**~~ — ☑ 붙었다. 다만 `BLOCKED_BY` 에 분기를 넣지 **않았다**.
  표는 "다툴 수 있다"까지만 말하게 두고, 실제로 다투는지는
  [`_contends()`](../backend/app/services/exclusivity.py) 가 판정한다 —
  학습이 낀 관계는 전부 GPU 라서 러너의 `occupies_local_gpu` 하나로 통째로 꺼진다.
  표에 러너별 항목을 넣었으면 대칭성 테스트가 깨졌을 것이고, 무엇보다
  **"무엇이 무엇을 막는가"와 "지금 그게 참인가"는 다른 질문**이다.
- **데몬 분리 후** `STATE_PROVIDERS` 가 버스 상태 조회로 바뀐다.
  한 곳에 모아뒀으므로 한 파일 수정으로 끝난다 ([daemon-split.md](daemon-split.md))

## 상태

☑ 완료 (실기 검증 대기)
