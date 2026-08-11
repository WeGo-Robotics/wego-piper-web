# 8. `PIPER_JOINTS = 7` 프론트/백 각각 (C급) — ☑ 완료

> **새 엔드포인트 없이 해결.** 문서가 보류를 권한 이유(API 호출 추가 + 로딩 상태)가
> 사라졌다 — 이미 있는 `/inference/validate` 응답에 `robot_joints` 를 실었다.
> 프론트 상수를 지우고 그 값을 쓴다.

## 문제

Piper 팔 1대의 관절 수가 두 곳에 하드코딩되어 있다.

- [inference.py:14](../backend/app/routers/inference.py#L14) — `PIPER_JOINTS = 7  # Piper 팔 1대의 관절 수`
- [InferencePage.tsx:71](../frontend/src/pages/InferencePage.tsx#L71) — `const PIPER_JOINTS = 7`

양쪽 모두 모델의 state/action 차원 검증(단일팔 7 vs 양팔 14)에 쓴다.

## 왜 C급인가

로봇 하드웨어가 바뀌지 않는 한 변하지 않는 값이고, 바뀌면 어차피 훨씬 많은 곳을 고쳐야 한다.
지금 어긋나 있지도 않다. **당장 고칠 이유는 없다.**

## 해결안 (하게 된다면)

`GET /api/robots/spec` 같은 엔드포인트로 백엔드가 내려주고 프론트가 받아 쓴다.
#6(관절 이름·범위)과 함께 하나의 "로봇 스펙" 응답으로 묶는 게 자연스럽다:

```json
{ "joints": 7, "joint_names": ["joint1", ..., "gripper"], "ranges": {...} }
```

다만 API 호출이 하나 늘고 프론트에 로딩 상태가 생긴다. #6을 프론트 안에서만 통합하기로 했다면
이 항목은 그대로 두는 편이 낫다.

## 판단

**보류 권장.** #6을 먼저 하고, 그때 로봇 스펙을 API로 내릴 가치가 있는지 다시 본다.

## 상태

☑ 완료 (validate 응답에 실어 해결)
