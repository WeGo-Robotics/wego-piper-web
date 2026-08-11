# 6. 관절 이름·범위 프론트 3곳 (B급) — ☑ 완료

> [config/joints.ts](../frontend/src/config/joints.ts) 로 통합.
> `actionKey` 를 명시 필드로 둬서 `` `${name}.pos` `` 조립이 흩어지지 않게 했다.
> **순서가 백엔드와 어긋나면 수동 제어가 엉뚱한 관절을 움직이므로** 테스트로 고정했다.

## 문제

같은 7관절 정의가 프론트엔드 세 파일에 각각 있다.

| 위치 | 형태 |
|---|---|
| [RobotsPage.tsx:5](../frontend/src/pages/RobotsPage.tsx#L5) | `JOINT_NAMES = ['joint1'...'gripper']` |
| [TelemetryPanel.tsx:14-23](../frontend/src/components/TelemetryPanel.tsx#L14-L23) | `JOINT_NAMES` + `JOINT_RANGE` |
| [ManualControlPanel.tsx:4-12](../frontend/src/components/ManualControlPanel.tsx#L4-L12) | `JOINTS` — `.pos` 접미사 + 라벨 + min/max |

세 곳 모두 동일한 관절 7개, 동일한 범위(관절 -100~100, 그리퍼 0~100)를 쓴다.
표현만 다르다:

- `RobotsPage` / `TelemetryPanel`: `"joint1"`
- `ManualControlPanel`: `"joint1.pos"` (백엔드 action dict 키 형식,
  [params.py:52](../backend/app/routers/params.py#L52) 참조)

## 해결안

`frontend/src/config/joints.ts` — `pages.ts`와 같은 방식의 선언 리스트:

```ts
export type Joint = {
  name: string          // 'joint1'
  actionKey: string     // 'joint1.pos'
  label: string         // 'Joint 1'
  min: number
  max: number
}

export const JOINTS: Joint[] = [...]
export const JOINT_NAMES = JOINTS.map(j => j.name)
export const JOINT_RANGE = Object.fromEntries(JOINTS.map(j => [j.name, [j.min, j.max]]))
```

세 파일이 여기서 파생한다. `actionKey`를 명시적 필드로 두면
`` `${name}.pos` `` 같은 문자열 조립이 흩어지지 않는다.

## 주의

- `ManualControlPanel`은 `currentJoints: number[]`를 **인덱스 순서로** 매칭한다
  ([line 22](../frontend/src/components/ManualControlPanel.tsx#L22): `currentJoints[i]`).
  리스트 순서가 백엔드가 보내는 배열 순서와 같아야 한다 — 순서를 바꾸면 안 된다.
- 백엔드([robot_manager.py:431-436](../backend/app/services/robot_manager.py#L431-L436))도
  같은 순서로 dict를 만든다. 프론트 리스트는 그 순서를 따라야 한다.
- 이건 프론트 안에서만 통합하는 작업이다. 백엔드와의 통합은 #8(`PIPER_JOINTS`)과 함께
  판단하되, 굳이 API를 늘릴 가치가 있는지는 별개다.

## 검증

- `cd frontend && npm run build`
- 로봇 페이지 관절 표시, 텔레메트리 바, 수동 제어 슬라이더가 모두 이전과 같은지 육안 확인
- 수동 제어로 관절 하나를 움직여 실제로 그 관절이 움직이는지 (순서 뒤바뀜 검출)

## 상태

☑ 완료 (수동 제어 실기 검증 대기)
