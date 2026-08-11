# 9. `robot_type: 'piper_follower'` 5곳 하드코딩 (C급)

## 문제

프론트엔드가 요청 본문에 로봇 타입 문자열을 직접 박아 보낸다.

- [InferencePage.tsx:210](../frontend/src/pages/InferencePage.tsx#L210)
- [InferencePage.tsx:240](../frontend/src/pages/InferencePage.tsx#L240)
- [InferencePage.tsx:265](../frontend/src/pages/InferencePage.tsx#L265)
- [RecordingPage.tsx:131](../frontend/src/pages/RecordingPage.tsx#L131)
- [RecordingPage.tsx:148](../frontend/src/pages/RecordingPage.tsx#L148) (+ `teleop_type: 'piper_leader'`)

백엔드에는 이미 기본값이 있다:

- [recording.py:20,23,42,45](../backend/app/routers/recording.py#L20) — `robot_type: str = "piper_follower"`, `teleop_type: str = "piper_leader"`
- [models.py:207](../backend/app/routers/models.py#L207) — `body.robot_type or robot_manager.selected_type or "piper_follower"`

즉 **프론트가 굳이 안 보내도 되는 값을 보내고 있다.** 실제 정본은 `robot_manager.selected_type`이다.

## 왜 C급인가

값이 전부 일치하고, 백엔드 기본값이 같은 문자열이라 어긋나도 결과가 같다.
로봇 기종이 하나뿐인 동안은 문제가 드러나지 않는다.

## 해결안 (택1)

### (a) 프론트에서 제거 — 가장 단순

프론트가 `robot_type`을 아예 안 보내고 백엔드 기본값 / `robot_manager.selected_type`에 맡긴다.
5곳이 지워지고 정본이 한 곳(`robot_manager`)으로 모인다.

**확인 필요**: `models.py:207`은 `body.robot_type or selected_type or 기본값` 순서라
프론트가 안 보내면 `selected_type`이 이긴다. 지금은 프론트 값이 이기고 있다.
두 값이 다를 수 있는 상황(로봇 페이지에서 다른 기종 선택)이 있는지 확인해야 한다.

### (b) 프론트 상수 하나로

`config/robot.ts`에 `DEFAULT_ROBOT_TYPE`을 두고 5곳이 참조. 변경은 작지만
"프론트가 로봇 기종을 안다"는 구조는 그대로 남는다.

## 판단

(a)가 옳은 방향이지만 `selected_type`과의 우선순위 변경이 동작에 영향을 줄 수 있다.
**#2(정책 레지스트리)를 하면서 같이 보는 편이 낫다** — 둘 다 "프론트가 알 필요 없는 것을
알고 있다"는 같은 문제다.

## 검증

- 로봇 미선택 상태 / 선택 상태 각각에서 추론·레코딩 시작이 되는지
- 생성된 CLI 인자에 `--robot.type=piper_follower`가 그대로 들어가는지 (UI의 CLI 미리보기로 확인)

## 상태

☐ 미착수 (#2와 함께 검토)
