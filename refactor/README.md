# 구조화 리팩터링 작업 목록

> **[../ROADMAP.md](../ROADMAP.md)** — 이 목록과 [feature/](../feature/) 3건을 합친 구현 순서.
> 무엇을 먼저 할지는 거기서 정한다.

트랙이 둘이다. 서로 독립적으로 진행할 수 있다.

- **[중복 제거](#진행-현황)** (#1~#13) — 한 사실이 두 곳에 적혀 있는 곳을 고치는 국소 수정
- **[구조 개편](#구조-개편-별도-트랙)** — 프로세스 경계를 다시 긋는 큰 작업

## 판단 기준

아래 번호 목록은 "코드를 예쁘게 만들기"가 아니라 **한 사실이 두 곳 이상에 적혀 있어 어긋날 수 있는 곳**만 다룬다.
어긋나도 아무 일 안 일어나는 중복은 C급으로 내리고, 이미 어긋난 것은 A급으로 올린다.
(구조 개편 트랙은 이 기준 밖이라 번호를 붙이지 않고 따로 둔다.)

## 선례

`frontend/src/config/pages.ts` (커밋 `181ace1`) — 내비게이션·라우트·대시보드 카드가 세 곳에
흩어져 있던 것을 선언 리스트 하나로 통합. 이 목록의 나머지도 같은 방식을 지향한다.

## 진행 현황

| # | 항목 | 급 | 상태 | 문서 |
|---|------|----|----|------|
| 1 | 추론 파라미터 3중 정의 (드리프트 2건) | A | ☐ | [01-inference-params.md](01-inference-params.md) |
| 2 | 정책 타입 목록 6곳 불일치 | A | ☐ | [02-policy-registry.md](02-policy-registry.md) |
| 3 | wrapper 부트스트랩 40줄 복붙 ×3 | B | ☐ | [03-wrapper-bootstrap.md](03-wrapper-bootstrap.md) |
| 4 | `_ERR_BITS` 프로세스 경계 넘어 복붙 | B | ☐ | [04-err-bits.md](04-err-bits.md) |
| 5 | 관절 캘리브레이션 `cal` dict 2회 중복 | B | ☐ | [05-joint-calibration.md](05-joint-calibration.md) |
| 6 | 관절 이름·범위 프론트 3곳 | B | ☐ | [06-joint-names-frontend.md](06-joint-names-frontend.md) |
| 7 | 라우터 등록 2중 (main.py) | C | ☐ | [07-router-registration.md](07-router-registration.md) |
| 8 | `PIPER_JOINTS = 7` 프론트/백 각각 | C | ☐ | [08-piper-joints.md](08-piper-joints.md) |
| 9 | `robot_type: 'piper_follower'` 5곳 | C | ☐ | [09-robot-type.md](09-robot-type.md) |
| 10 | 배타 모드 가드 8곳 부분집합 불일치 (드리프트 3건) | A | ☐ | [10-exclusive-mode-guard.md](10-exclusive-mode-guard.md) |
| 11 | HF 캐시 레이아웃 해석 중복 (드리프트 1건) | B | ☐ | [11-hf-cache-layout.md](11-hf-cache-layout.md) |
| 12 | WS 메시지 타입 16종 계약 없음 | B | ☐ | [12-ws-message-contract.md](12-ws-message-contract.md) |
| 13 | `ProcessState` 유니온 프론트 4곳 | C | ☐ | [13-process-state-union.md](13-process-state-union.md) |

상태: ☐ 미착수 / ◐ 진행중 / ☑ 완료

## 권장 순서

1. **#10** — 유일하게 안전에 걸린다. 녹화 중 학습·추론이 시작되고,
   `/inference/start-custom`은 녹화 중에도 카메라를 뺏는다.
2. **#1의 드리프트 2건만 핀포인트 수정** — 지금 동작에 영향이 있다 (UI 값 유실).
3. **#3~#6, #11** — 동작 변화 없는 순수 추출. 위험 낮고 각각 짧다.
4. **#1, #2, #12, #13의 구조 통합** — 프론트↔백엔드 계약을 새로 만드는 일이라 범위가 크다.
   전부 아래 구조 개편의 버스 계약과 겹치므로 **묶어서 판단한다** (#13은 #12에 흡수됨).
5. **#7~#9** — 여유 있을 때.

## 구조 개편 (별도 트랙)

| 항목 | 상태 | 문서 |
|------|----|------|
| 데몬 분리 — 방향·통신·배포 결정 | ☐ | [daemon-split.md](daemon-split.md) |
| 데몬 목록 — 무엇을 몇 개로 쪼갤 것인가 | ☐ | [daemon-inventory.md](daemon-inventory.md) |
| 카메라 전송 — shm + LeRobot 플러그인 (**설계 확정**) | ☐ | [camera-transport.md](camera-transport.md) |
| 로봇 전송 — shm 프록시 드라이버 (**설계 확정**) | ☐ | [robot-transport.md](robot-transport.md) |
| robotd 안전 층 — 기구학 충돌 방지 | ☐ | [robotd-safety.md](robotd-safety.md) |

백엔드를 **개별 프로세스와 프론트엔드를 엮는 얇은 게이트웨이**로 축소하고, 상시 실행체를
독립 데몬 프로세스로 떼어낸다. 통신은 Redis(데이터 평면) + systemd(제어 평면),
배포는 systemd를 최상위에 두고 LeRobot/CUDA 의존 데몬만 컨테이너로 남긴다.

위 #1~#13과 달리 중복 제거가 아니라 경계 재설계이므로 번호 밖에 둔다. 다만 두 트랙이
여러 곳에서 만난다 — **#1·#2·#12·#13은 프론트↔백 계약을 새로 만드는 일이라 데몬 분리의
버스 계약(`piper_bus/`)과 범위가 겹치고, #10의 배타 규칙은 분리 후 버스 상태 조회로 바뀐다.**
어느 쪽을 먼저 할지는 daemon-split.md의 미결정 #5에서 판단한다.

부수 효과로 CLAUDE.md/REF.md가 설계 원칙으로 못 박은 *"E-stop watchdog은 독립 프로세스"* 가
처음으로 실제로 지켜진다 (현재는 웹서버와 같은 이벤트 루프).

## 검증

프론트엔드 변경은 반드시 `cd frontend && npm run build`로 확인한다.
(`npx tsc --noEmit`은 루트 tsconfig가 참조 전용이라 no-op이다.)
백엔드 변경은 서버 재시작 시 import 에러가 없는지, wrapper 변경은 실제 추론/레코딩을 한 번 돌려서 확인한다.
