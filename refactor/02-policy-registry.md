# 2. 정책 타입 목록 6곳 불일치 (A급)

## 문제

"지원하는 정책 타입"이 6곳에 따로 적혀 있고 **전부 다른 집합**이다.

| 위치 | 집합 |
|---|---|
| [TrainingPage.tsx:12](../frontend/src/pages/TrainingPage.tsx#L12) `POLICY_TYPES` | act, diffusion, smolvla, pi0, pi05, vqbet, tdmpc, **sac** |
| [InferencePage.tsx:455-462](../frontend/src/pages/InferencePage.tsx#L455-L462) `<option>` | smolvla, act, diffusion, pi0, pi05, **pi0_fast**, vqbet, tdmpc |
| [InferencePage.tsx:73](../frontend/src/pages/InferencePage.tsx#L73) `RTC_POLICIES` | smolvla, pi0, pi05 |
| [hub_client.py:39](../backend/app/services/hub_client.py#L39) | smolvla, act, diffusion, pi0, pi05, vqbet, tdmpc, sac, **rtc** |
| [lerobot_wrapper.py:108-125](../wrapper/lerobot_wrapper.py#L108-L125) `POLICY_IMPORTS` | smolvla, act, diffusion, pi0, pi05, **pi0_fast**, tdmpc, vqbet |
| [encoder.py:42](../backend/app/routers/encoder.py#L42) `SUPPORTED` | smolvla, act |

## 실제 영향

- **`sac`**: 학습 화면에서 고를 수 있지만 `POLICY_IMPORTS`에 없다 →
  추론 시작 시 [lerobot_wrapper.py:268-269](../wrapper/lerobot_wrapper.py#L268-L269)에서
  `ValueError: Unsupported policy type: 'sac'`로 죽는다.
- **`pi0_fast`**: 추론에서 고를 수 있지만 학습 목록에 없다.
- **`"rtc"`**: `hub_client`의 정책 목록에 들어 있으나 정책 타입이 아니라 추론 스무딩 기법이다.
  Hub 모델 이름에 "rtc"가 들어가면 `policy_type = "rtc"`로 잘못 태깅된다.

## 곁다리 — 루프 안에서 dict 재생성

[hub_client.py:59-73](../backend/app/services/hub_client.py#L59-L73)의 `_KNOWN_POLICY_BASES`와
`_KNOWN_VLM_BASES`가 [line 30의 `for model in models:`](../backend/app/services/hub_client.py#L30)
**안**에 있어 모델마다 재생성된다. 모듈 상수로 올려야 한다. (이것도 정책별 메타데이터이므로
아래 레지스트리로 흡수된다.)

## 해결안

`backend/app/core/policies.py`에 정책 레지스트리 하나:

```python
POLICIES = {
    "smolvla": {
        "label": "SmolVLA",
        "train": True, "infer": True, "rtc": True, "encoder_probe": True,
        "policy_base": "lerobot/smolvla_base",
        "vlm_base": "HuggingFaceTB/SmolVLM2-500M-Video-Instruct",
    },
    "act": {"label": "ACT", "train": True, "infer": True, "rtc": False, "encoder_probe": True},
    ...
}
```

파생:
- `hub_client`의 정책 태그 추론 목록 + `_KNOWN_*_BASES` → 레지스트리에서
- `encoder.py`의 `SUPPORTED` → `encoder_probe: True`인 것
- `GET /api/policies`로 프론트 공급 → `TrainingPage.POLICY_TYPES`, `InferencePage`의
  `<option>` 목록과 `RTC_POLICIES` 모두 파생

`wrapper/lerobot_wrapper.py`의 `POLICY_IMPORTS`는 wrapper가 백엔드를 import하지 않는
구조(크래시 격리)라 그대로 두되, **레지스트리에 `infer: True`인 정책은 반드시
`POLICY_IMPORTS`에 있어야 한다**는 것을 테스트나 기동 시 점검으로 잡는 편이 낫다.

## 먼저 정해야 할 것

- `sac`를 지원할 것인가? (지원 → `POLICY_IMPORTS`에 추가 / 미지원 → 학습 목록에서 제거)
- `pi0_fast`를 학습에서도 지원할 것인가?
- `hub_client`의 `"rtc"`는 제거해도 되는가? (Hub 모델 태깅에만 쓰임)

## 검증

- 각 정책 타입으로 학습 인자 빌드가 되는지: `build_train_args({"policy_type": ...})`
- `infer: True`인 정책 전부에 대해 `POLICY_IMPORTS`에 키가 있는지 확인
- `cd frontend && npm run build`

## 상태

☐ 미착수
