# 12. WebSocket 메시지 타입 15종이 계약 없이 문자열 리터럴 (B급) — ☑ 완료

> 백엔드 [core/ws_messages.py](../backend/app/core/ws_messages.py) 상수 +
> 프론트 [types/ws.ts](../frontend/src/types/ws.ts) **판별 유니언**.
> 오타가 이제 컴파일 에러다 (`TS2367: no overlap`).
> 두 언어에 걸쳐 있으므로 문서의 (c) 안대로 **목록 일치 테스트**로 고정했다 —
> [test_ws_contract.py](../backend/tests/test_ws_contract.py).

## 문제

백엔드가 WS로 보내는 메시지 종류는 16가지인데, **그 목록이 어디에도 없다.**
`ws.py`의 `broadcast()` 호출에 문자열로 흩어져 있고, 프론트는 각 페이지에서 문자열을 비교한다.

### 백엔드 — [ws.py](../backend/app/routers/ws.py)

| type | 위치 |
|---|---|
| `telemetry` | [:49](../backend/app/routers/ws.py#L49), [:85](../backend/app/routers/ws.py#L85) |
| `log_saved` | [:54](../backend/app/routers/ws.py#L54) |
| `log` / `state` | [:90](../backend/app/routers/ws.py#L90), [:95](../backend/app/routers/ws.py#L95), [:179](../backend/app/routers/ws.py#L179) |
| `train_log` / `train_state` / `train_metrics` | [:104](../backend/app/routers/ws.py#L104), [:109](../backend/app/routers/ws.py#L109), [:114](../backend/app/routers/ws.py#L114), [:180](../backend/app/routers/ws.py#L180) |
| `record_log` / `record_state` / `record_status` | [:124](../backend/app/routers/ws.py#L124), [:129](../backend/app/routers/ws.py#L129), [:134](../backend/app/routers/ws.py#L134), [:181](../backend/app/routers/ws.py#L181) |
| `ps_log` / `ps_state` | [:144](../backend/app/routers/ws.py#L144), [:149](../backend/app/routers/ws.py#L149) |
| `upload_log` / `upload_state` | [:160](../backend/app/routers/ws.py#L160), [:165](../backend/app/routers/ws.py#L165) |
| `pong` | [:187](../backend/app/routers/ws.py#L187) |

### 프론트 — 5개 페이지가 각자 문자열 비교

[PolicyServerPage:30](../frontend/src/pages/PolicyServerPage.tsx#L30),
[TrainingPage:133-143](../frontend/src/pages/TrainingPage.tsx#L133-L143),
[InferencePage:127-143](../frontend/src/pages/InferencePage.tsx#L127-L143),
[DatasetsPage:40-41](../frontend/src/pages/DatasetsPage.tsx#L40-L41),
[RecordingPage:55-63](../frontend/src/pages/RecordingPage.tsx#L55-L63)

### 타입이 없다

[useWebSocket.ts:3-6](../frontend/src/hooks/useWebSocket.ts#L3-L6):

```ts
export type WsMessage = {
  type: string        // ← 아무 문자열이나 통과
  data: unknown       // ← 매번 as 캐스트
}
```

`msg.type === 'train_metrics'`를 `'train_metric'`으로 오타 내도 **빌드가 통과하고 런타임에 조용히
아무 일도 안 일어난다.** 백엔드에서 type 이름을 바꿔도 마찬가지다 — 화면이 그냥 안 갱신된다.
`data`도 매번 `as RecordStatusData` 같은 캐스트라 필드가 바뀌어도 안 잡힌다.

## 데몬 분리와의 관계 — 이게 우선순위를 올린다

[daemon-split.md](daemon-split.md)에서 프로세스를 쪼개면 **이 메시지 목록이 곧 버스 계약**이 된다.
지금은 "프론트↔백" 두 곳이지만, 분리 후에는 데몬 N개가 각자 이 이름들을 발행하게 된다.
계약 없이 쪼개면 [04-err-bits.md](04-err-bits.md)와 같은 프로세스 경계 복붙이 16배로 생긴다.

**즉 이 항목은 daemon-split의 1단계(`piper_bus/` 계약 패키지)와 같은 작업이다.**
따로 하지 말고 묶는 편이 낫다.

## 해결안

메시지 이름과 payload 타입을 한 곳에 선언하고, 양쪽이 그것만 쓴다.

```ts
// frontend/src/types/ws.ts
export type WsMessage =
  | { type: 'state';         data: ProcessState }
  | { type: 'log';           data: string }
  | { type: 'telemetry';     data: TelemetryData }
  | { type: 'train_state';   data: ProcessState }
  | { type: 'train_metrics'; data: TrainMetrics }
  | { type: 'record_status'; data: RecordStatusData }
  // …
```

판별 유니온으로 두면 `if (msg.type === 'train_metrics')` 안에서 `msg.data`가 자동으로 좁혀져
캐스트가 전부 사라지고, 오타는 컴파일 에러가 된다.

백엔드 쪽은 `ws.py`의 리터럴을 상수/enum으로 올린다. 두 언어에 걸쳐 있으므로
완전 자동 동기화는 어렵다 — 선택지는 [04-err-bits.md](04-err-bits.md)와 같은 구조다:
(a) 한쪽을 정본으로 두고 생성, (b) JSON 스키마 파일 공유, (c) 목록 일치 테스트.
**여기서는 (c)가 비용 대비 효과가 가장 좋다** — `ws.py`가 보내는 type 집합과
프론트 유니온의 키 집합이 같은지 확인하는 테스트 하나.

## 곁다리 — 로그 콜백이 전역 하나에만 걸려 있다

[ws.py:98-99](../backend/app/routers/ws.py#L98-L99)는 전역 `process_manager`에만 콜백을 걸고,
train/record/ps는 각자 [:104](../backend/app/routers/ws.py#L104)/[:124](../backend/app/routers/ws.py#L124)/[:144](../backend/app/routers/ws.py#L144)에서 따로 건다.
프로세스마다 로그 경로가 제각각이라 새 프로세스를 추가할 때마다 `ws.py`에 블록이 하나씩 는다.
데몬 분리 후에는 "데몬 → 버스 로그 스트림 → WS"로 한 경로가 되므로 이 블록들이 사라진다.
(daemon-split.md 미결정 #4)

## 검증

- `cd frontend && npm run build` — 판별 유니온 도입 후 캐스트 제거가 전부 통과하는지
- 각 페이지에서 실제로 로그/상태/메트릭이 갱신되는지 (학습 1회, 녹화 1회, 추론 1회)
- type 이름을 일부러 하나 바꿔 테스트가 실패하는지

## 상태

☑ 완료 — daemon-split 1단계에서 `piper_bus/` 로 이사할 자리다
