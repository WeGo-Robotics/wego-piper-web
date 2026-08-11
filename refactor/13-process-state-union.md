# 13. `ProcessState` 유니온이 프론트 4곳 + 백엔드 1곳 (C급)

## 문제

같은 유니온이 프론트엔드 4개 페이지에 **한 글자도 다르지 않게** 각각 선언돼 있다.

```ts
type ProcessState = 'idle' | 'starting' | 'running' | 'stopping' | 'error'
```

- [PolicyServerPage.tsx:6](../frontend/src/pages/PolicyServerPage.tsx#L6)
- [RecordingPage.tsx:7](../frontend/src/pages/RecordingPage.tsx#L7)
- [TrainingPage.tsx:7](../frontend/src/pages/TrainingPage.tsx#L7)
- [InferencePage.tsx:66](../frontend/src/pages/InferencePage.tsx#L66)

정본은 백엔드 [process_manager.py:15-21](../backend/app/services/process_manager.py#L15-L21)의
`ProcessState(str, enum.Enum)`이다. 총 5곳.

## 실제 영향

**지금은 다섯 곳이 전부 일치한다.** 그래서 C급이다. 다만:

- 백엔드에 상태를 하나 추가하면(예: `paused`) 프론트 4곳을 다 고쳐야 하고, 빼먹은 페이지는
  **컴파일 에러 없이** 조용히 틀린다 — 값이 `as ProcessState` 캐스트로 들어오기 때문이다:
  [RecordingPage:76](../frontend/src/pages/RecordingPage.tsx#L76), [TrainingPage:154](../frontend/src/pages/TrainingPage.tsx#L154), [InferencePage:160](../frontend/src/pages/InferencePage.tsx#L160), [PolicyServerPage:41](../frontend/src/pages/PolicyServerPage.tsx#L41)
- 상태 문자열 리터럴도 페이지마다 흩어져 있다 (`'idle'`, `'running'` 직접 비교가 40곳 이상,
  [DatasetsPage](../frontend/src/pages/DatasetsPage.tsx)가 19곳으로 최다)

## 해결안

`frontend/src/types/process.ts` 하나로 옮기고 4곳에서 import 한다.

```ts
export const PROCESS_STATES = ['idle', 'starting', 'running', 'stopping', 'error'] as const
export type ProcessState = typeof PROCESS_STATES[number]

// 흩어진 비교를 대체
export const isBusy = (s: ProcessState) => s === 'starting' || s === 'running'
export const isStoppable = (s: ProcessState) => s === 'running' || s === 'starting'
```

`isBusy` 같은 헬퍼가 있으면 "실행 중"의 정의가 페이지마다 갈리는 것도 함께 막힌다
(현재 [encoder.py:49](../backend/app/routers/encoder.py#L49)는 `starting`+`running`을,
[training.py:113](../backend/app/routers/training.py#L113)은 `not in (idle, error)`를 쓴다 —
`stopping`의 취급이 다르다. [10-exclusive-mode-guard.md](10-exclusive-mode-guard.md) 참고).

백엔드 enum과의 동기화는 [12-ws-message-contract.md](12-ws-message-contract.md)와 같은 문제이므로
그쪽 결론을 따른다 — 목록 일치 테스트 하나면 충분하다.

## 순서

[12](12-ws-message-contract.md)를 먼저 하면 `ProcessState`가 WS 판별 유니온의 payload 타입으로
필요해지므로 이 항목은 그 부산물로 자연히 해결된다. **12와 묶어서 하고, 단독으로는 하지 않는다.**

## 검증

`cd frontend && npm run build` — 4곳의 로컬 선언을 지우고 import로 바꾼 뒤 통과하는지.
동작 변화는 없어야 한다 (순수 타입 이동).

## 상태

☐ 미착수 — #12에 흡수 예정
