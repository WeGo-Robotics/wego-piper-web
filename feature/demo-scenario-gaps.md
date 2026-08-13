# 데모 시나리오 격차 분석 — 시나리오 문서 vs 현재 코드

> 대상: [../PiPER_AI_데모_시나리오_정리.md](../PiPER_AI_데모_시나리오_정리.md) (2026-08-11)
> 방법: 뎁스/카메라 · 언어 조건/평가 · 양팔/프로세스 구조 세 갈래로 코드베이스 전수 확인.
> 결론: **언어 조건 픽앤플레이스는 거의 지금도 되고, 나머지는 공통 격차 4개(G1~G4)에 걸린다.**

---

## 0. 이미 갖춰져 있는 것

시나리오 문서가 요구하는 것 중 **코드 수정 없이 되는 것**부터. 새로 만들 목록에서 빼야 한다.

| 요구 | 현재 상태 | 근거 |
|---|---|---|
| 지시문 실시간 변경 (3.1.1 런타임 규칙 변경의 주입 채널) | **동작함.** `POST /api/params`로 `task` 전송 → 클램핑 없이 wrapper 도달 → 다음 추론부터 observation에 반영. task 문자열 변경 시 재토큰화도 처리됨 | [zmq_bridge.py:66-68](../backend/app/services/zmq_bridge.py#L66-L68), [lerobot_wrapper.py:154-157](../wrapper/lerobot_wrapper.py#L154-L157), [lerobot_wrapper.py:486-499](../wrapper/lerobot_wrapper.py#L486-L499) |
| 임의 지시문 입력 UI (3.4) | **있음.** 자유 텍스트 + 실행 중 500ms 디바운스 반영 | [InferencePage.tsx:511-517](../frontend/src/pages/InferencePage.tsx#L511-L517), [InferencePage.tsx:637-648](../frontend/src/pages/InferencePage.tsx#L637-L648) |
| 리셋 → 원점 복귀 → 재개 (6장 자동 리셋의 재료) | **수동 트리거로 완성돼 있음.** `POST /api/params/reset` → 큐/필터/policy 초기화 → `ParkingController` 2단계 파킹(리프트 후 전축) → 첫 추론처럼 재개 | [lerobot_wrapper.py:608-639](../wrapper/lerobot_wrapper.py#L608-L639), [parking_controller.py](../wrapper/parking_controller.py) |
| D405 뎁스 스트림 | **이미 켜져 있음.** color 0fps 워크어라운드로 `use_depth=True` 강제 → 뎁스 프레임이 이미 흐르는데 **버려지고 있다.** Phase 2의 추가 비용이 사실상 0인 이유 | [realsense_manager.py:128-140](../backend/app/services/realsense_manager.py#L128-L140), [models.py:136-141](../backend/app/routers/models.py#L136-L141) |
| 양팔 **추론** (gRPC 모드 한정) | `robot_ports` 2개 → left/right 생성, 카메라 접두사 분배, 액션 동시 전송 | [grpc_wrapper.py:240-311](../wrapper/grpc_wrapper.py#L240-L311), [models.py:159](../backend/app/routers/models.py#L159) |
| 성공/실패 기록 + 통계 | 수동 버튼 + JSONL + 성공률 집계 | [eval_log.py](../backend/app/routers/eval_log.py), [EvalPanel.tsx](../frontend/src/components/EvalPanel.tsx) |

---

## 1. 공통 격차 4개

시나리오별로 요구를 나열하면 겹치는 몸통이 4개로 수렴한다. **시나리오 순서가 아니라 이 4개 순서로 개발해야** 두 번 짓지 않는다.

### G1. 뎁스가 데이터셋/정책에 도달하지 못함 — 팝콘 전 Phase의 전제

- 뎁스는 프리뷰 JPEG로만 나간다. 컬러라이즈는 JET·`alpha=0.03` 하드코딩, 클리핑 범위/무효픽셀 처리 없음
  ([realsense_manager.py:210-215](../backend/app/services/realsense_manager.py#L210-L215))
- 관측 조립은 `async_read()`(color)만 호출, 카메라 feature `(h,w,3)` 하드코딩
  ([piper_follower.py:138](../vendor/lerobot_robot_piper/lerobot_robot_piper/piper_follower.py#L138), [piper_follower.py:70-73](../vendor/lerobot_robot_piper/lerobot_robot_piper/piper_follower.py#L70-L73))
- **함정**: UI에서 `rs:<serial>:depth`를 카메라 매핑에 골라도 serial만 뽑고 stream을 버려 **조용히 color가 기록된다**
  ([models.py:130-131](../backend/app/routers/models.py#L130-L131), [RecordingPage.tsx:117](../frontend/src/pages/RecordingPage.tsx#L117))
- raw uint16 접근 경로 자체가 없다 — 하이트맵(Phase 1)도 여기 걸린다
- 하이트맵 파이프라인(5×5 median, 시간 평균, argmax) 전무. librealsense post-processing 필터 호출이 저장소에 없다

**지름길**: [camera-transport의 깊이맵 절](../refactor/camera-transport.md)은 camerad 위 설계지만,
`vendor/lerobot_robot_piper`는 **우리 플러그인**이라 "LeRobot 수정 금지" 원칙 밖이다.
여기서 `read_depth()`를 호출해 컬러라이즈 후 추가 카메라 키로 노출하면 **camerad 없이 Phase 2가 된다.**
단 컬러맵·클리핑 범위는 **데이터셋 계약**이므로 처음부터 설정 파일로 고정할 것
(camera-transport.md의 경고 그대로 — 수집과 추론이 다르면 분포가 어긋난다).

### G2. 에피소드 오케스트레이터 부재 — 팝콘·분리수거·자동 리셋·플래너의 공통 몸통

> 구현 설계는 [episode-orchestrator.md](episode-orchestrator.md) — 스텝 프로토콜 + YAML 시나리오 스펙.

추론은 start/stop 단일 세션이고 **에피소드 경계 개념이 없다**
([process_manager.py](../backend/app/services/process_manager.py) — 상태 5종뿐, [lerobot_wrapper.py:604](../wrapper/lerobot_wrapper.py#L604) — `while running:` 단일 루프).
리셋은 사람이 버튼을 눌러야 한다 ([InferencePage.tsx:286](../frontend/src/pages/InferencePage.tsx#L286)).

필요한 것은 백엔드에 얹는 상태기계 하나:

```
스냅샷 → 판단(argmax / YOLO→LLM) → 지시문 확정(task 변경) → 실행 → 종료 판정 → 리셋 → 반복
```

구성 요소가 전부 REST/ZMQ로 이미 노출돼 있어(task·pause·reset) **wrapper 대수술이 아니다.**
이 루프가 6장 "30분 무인 구동"의 실체이고, 5장 플래너는 이 루프의 "판단" 칸을 LLM 계획으로 바꾼 것이다.

주의: `require_idle`이 STOPPING도 실행 중으로 보므로 ([exclusivity.py:86](../backend/app/services/exclusivity.py#L86))
스킬 전환 시 완전 종료 대기가 강제된다 — 오케스트레이터는 같은 프로세스 안에서 reset으로 회차를 돌려야지,
프로세스 재시작으로 돌면 회차마다 파킹+CAN+카메라 재연결 수 초를 문다.

### G3. 스냅샷 · 외부 센서/이벤트 입력 경로 부재

**스냅샷** ("팔이 빠진 시점" 1회 캡처):
- 현재 가장 가까운 것은 wrapper가 20스텝마다 덮어쓰는 `/dev/shm/piper_cam_*.jpg`
  ([grpc_wrapper.py:815-820](../wrapper/grpc_wrapper.py#L815-L820)) — 손실 JPEG, 타이밍 제어 불가
- 트리거 조건 개념 자체가 없다. 오케스트레이터(G2)가 **리셋 직후 = 팔이 확실히 빠진 시점**에
  캡처를 요청하는 API가 자연스러운 답 (시나리오 4.2 "팔이 빠진 시점 스냅샷"과 정확히 일치)
- 뎁스 스냅샷은 G1의 raw 접근이 선행

**외부 이벤트 → 성공 판정** (시나리오 5장 "성공 판정 인터페이스 통일"과 동일 문제):
- 센서 입력 경로가 코드베이스에 전혀 없다 (loadcell/sensor 관련 코드 0건).
  관측 소스는 관절 상태 + 카메라뿐 ([lerobot_wrapper.py:430-499](../wrapper/lerobot_wrapper.py#L430-L499))
- 로드셀(팝콘 4.4)·바코드 스캐너 "삑"(3.1)은 **같은 인터페이스의 두 구현**이다:
  `외부 이벤트 리스너 → 성공/실패 판정 → eval_log 자동 기록 + G2 루프에 종료 신호`
- 바코드 스캐너는 보통 USB HID 키보드 → evdev 리스너면 충분
- **단, 붓기 확장(4.4 ②)의 "로드셀 값을 관측에 포함"은 급이 다르다** —
  vendor 플러그인에 상태 차원을 추가하는 일이라 데이터셋 계약이 바뀐다. 판정용(easy)과 관측용(hard)을 분리해 계획할 것

**eval_log 확장** (지시문별/체크포인트별 성공률):
- 현재 레코드에 task가 없고, `checkpoint`는 저장만 하고 필터로 안 쓴다
  ([eval_log.py:18-22](../backend/app/routers/eval_log.py#L18-L22))
- [parameter-presets](parameter-presets.md)의 `preset_id` 기록과 **같은 확장 지점** — 같이 설계할 것

### G4. 양팔은 실행만 되고 수집이 안 됨 — 핸드오버(우선순위 2)의 블로커

> 구현 설계는 [bimanual.md](bimanual.md) — 양팔 조립을 wrapper 에서 LeRobot bi 클래스로 내린다.

데이터가 없는데 실행 경로만 있는 상태다.

| 항목 | 상태 | 근거 |
|---|---|---|
| 녹화 요청 | `robot_port`/`teleop_port` **단수 고정** | [recording.py:20-23](../backend/app/routers/recording.py#L20-L23) |
| 녹화 CLI 매핑 | `--robot.port`/`--teleop.port` 단수만 생성 | [cli_mapping.py:305-326](../backend/app/core/cli_mapping.py#L305-L326) |
| 하드웨어 슬롯 | 2리더/2팔로워 조합 **이미 표현 가능** | [robot_manager.py:27-30](../backend/app/services/robot_manager.py#L27-L30) |
| 로컬 추론 | `robot_ports` 매핑 없음 — gRPC 전용 | [cli_mapping.py:20-30](../backend/app/core/cli_mapping.py#L20-L30) |

양팔 **실행 경로 자체의 결함** 2건도 데이터 수집 전에 고쳐야 한다:
- `lerobot_features`를 **left 기준으로만** 정책 서버에 전송 ([grpc_wrapper.py:277](../wrapper/grpc_wrapper.py#L277)) — 양팔 정책 feature 정합성 구멍
- gRPC 파킹이 2단계 리프트 없이 동시 파킹 + `sleep(5)` 고정 대기 ([grpc_wrapper.py:314-317](../wrapper/grpc_wrapper.py#L314-L317)) — 로컬 모드에서 잡은 그리퍼 바닥 긁힘 문제가 gRPC 경로엔 그대로

---

## 2. 시나리오 → 격차 매핑

시나리오 문서 8장 로드맵 순위 기준.

| 순위 | 시나리오 | 걸리는 격차 | 추가로 필요한 것 | 규모 |
|---|---|---|---|---|
| 1 | 팝콘 Phase 1 | G1(raw 뎁스+하이트맵) G2 G3(로드셀 판정) | RGB 노출 고정 — [camera-profiles](camera-profiles.md) 전체는 후순위 맞지만 **노출 고정 한 가지만 선행** | 중 |
| 1 | 팝콘 Phase 2 | G1(vendor 플러그인 뎁스 카메라화) | — | 소~중 |
| 1.5 | 팝콘 붓기 | G3의 hard 쪽 (로드셀 → 관측 feature) | 데이터셋 계약 변경 | **대** |
| 2 | 핸드오버 재파지 | G4 전체 | — | 중 |
| 3 | 바코드 제시 | G2(재시도 루프) G3(스캐너 리스너) | — | 소~중 |
| 4 | 트레이 키팅 | 없음 | 데이터 수집 문제일 뿐 | — |
| 5 | 언어 픽앤플레이스 | 거의 없음 | 녹화가 데이터셋당 `single_task` 하나 ([cli_mapping.py:314](../backend/app/core/cli_mapping.py#L314)) — 에피소드별 지시문은 사후 편집뿐. eval에 task 미기록(G3) | 소 |
| 6 | 분리수거 YOLO→LLM→VLA | G2 G3(스냅샷) | LLM 규칙 스토어 + 템플릿 슬롯 조립 + 파이프라인 뷰 UI. **YOLO 라이브 스트림만은 camerad 이후** (아래 §3) | 중 |
| 장기 | 스킬 라이브러리+플래너 | G2 | 세션 내 정책 교체: `SendPolicyInstructions`가 부팅 1회만 호출됨 ([grpc_wrapper.py:329-338](../wrapper/grpc_wrapper.py#L329-L338)) → ZMQ 명령으로 재호출하게 하면 서버의 `same_model` 캐시([start_policy_server.py:103-118](../wrapper/start_policy_server.py#L103-L118))를 그대로 써서 재시작 없이 전환. **스킬 체이닝은 gRPC 모드가 유일한 현실적 토대** | 장기 |
| 상시 | 자동 리셋 | G2 G3(종료 판정) | 리셋 재료는 §0 참고 — 루프만 없다 | G2에 포함 |

---

## 3. ROADMAP과의 정합성

시나리오 문서는 [../ROADMAP.md](../ROADMAP.md) 순서와 대체로 충돌하지 않는다.
"에피소드 단위 스냅샷"으로 설계한 덕에 **camerad(3b-5)를 기다리지 않아도 된다.**

**당길 것 2개:**
- G1 지름길 — camera-transport 깊이맵 절 중 **vendor 플러그인 부분만** 전술 선행 (camerad 불필요).
  나중에 camerad가 서면 컬러라이즈 소유권만 이관
- **RGB 노출 고정** — camera-profiles 전체는 후순위 유지, 노출 고정 한 가지만 팝콘에 선행

**미룰 것 1개:**
- **YOLO 라이브 프레임 접근.** 진짜 벽은 exclusivity 표가 아니라 **카메라 독점**이다 —
  표는 `BLOCKED_BY[새활동] = []` 한 줄로 열리지만 ([exclusivity.py:58-76](../backend/app/services/exclusivity.py#L58-L76)),
  추론 subprocess가 카메라를 독점하고 백엔드가 같은 D405를 열면 uvcvideo D-state로 죽는다
  ([exclusivity.py:71-75](../backend/app/services/exclusivity.py#L71-L75) 주석, [models.py:284](../backend/app/routers/models.py#L284)의 `_release_all_cameras()`).
  에피소드 단위 스냅샷(G3)이면 회피 가능 — **실시간 바운딩박스 뷰만 camerad 이후로**

---

## 4. 조사 중 발견한 부수 결함 (데모와 별개로 수정 가치)

| 결함 | 위치 |
|---|---|
| `rs:<serial>:depth` 매핑 시 조용히 color 기록 (G1의 함정) | [models.py:130-131](../backend/app/routers/models.py#L130-L131) |
| `build_train_args`가 인자 빌더인데 체크포인트 `config.json`을 디스크에 덮어씀 — 미리보기 경로와 공유 시 위험 | [cli_mapping.py:275-298](../backend/app/core/cli_mapping.py#L275-L298) |
| eval stats `last_n`이 total/성공률에만 적용, `recent`는 항상 전체 기준 | [eval_log.py:60](../backend/app/routers/eval_log.py#L60) |
| gRPC 종료 파킹 `sleep(5)` 고정 + 단계 분리 없음 (G4) | [grpc_wrapper.py:314-317](../wrapper/grpc_wrapper.py#L314-L317) |
| `RECORD_CMD`/`TRAIN_CMD` 상수는 선언만 되고 미사용 (죽은 코드) | [cli_mapping.py:14](../backend/app/core/cli_mapping.py#L14), [cli_mapping.py:177](../backend/app/core/cli_mapping.py#L177) |
