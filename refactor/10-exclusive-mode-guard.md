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

## 해결안

`backend/app/core/exclusivity.py`에 **"무엇이 무엇을 막는가"를 선언 하나로**:

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

**위 `BLOCKED_BY` 값은 현재 코드에서 추론한 것이 아니라 의도를 새로 정한 것이다.**
착수 전에 실제 의도와 맞는지 확인해야 한다 — 특히 녹화 중 학습 금지가 맞는지
(GPU 여유가 있으면 허용하고 싶을 수도 있다).

## 주의 — 데몬 분리와의 관계

[daemon-split.md](daemon-split.md)로 가면 이 활동들이 **각각 다른 프로세스**가 된다.
`is_running`을 파이썬 객체 속성으로 읽던 것이 버스 상태 조회로 바뀌므로,
`exclusivity.py`는 그때 `piper_bus/`의 상태 키를 읽는 형태로 옮겨간다.
지금 한 곳으로 모아두면 그 이행이 한 파일 수정으로 끝난다.

E-stop의 사정도 같다 — daemon-split.md 미결정 #2("E-stop이 무엇을 죽이는가")와
이 표는 같은 사실의 양면이다. 함께 정하는 게 맞다.

## 검증

- 녹화 중 학습 시작 → 409, 녹화 중 추론 시작 → 409
- 학습 중 정책 서버 시작 → 409 (반대 방향도)
- `/inference/start-custom`이 `/inference/start`와 동일하게 막히는지
- **녹화 중 `/inference/start-custom` 호출 시 카메라를 뺏지 않는지** (현행 버그의 회귀 테스트)
- 각 409 메시지가 한 형식으로 나오는지

## 상태

☐ 미착수
