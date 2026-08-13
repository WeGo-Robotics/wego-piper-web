# 로봇팔 수동 조작 — 웹 조그 + MIT 중력 보상 드래그

두 기능을 한 문서로 다룬다. 겉보기엔 다르지만 같은 곳(robotd)에 살고,
마지막 단계(손으로 끌며 녹화)에서 하나로 합쳐진다:

- **A. 웹 조그** — 추론을 안 띄우고 웹에서 관절을 움직인다.
  이미 깔린 shm 명령 경로에 **소비자 하나를 붙이는 일**이다
- **B. MIT 중력 보상** — 팔이 자기 무게를 스스로 들게 해서(토크 ON 유지) 손으로 끌 수 있게 한다.
  SDK MIT 모드(`JointMitCtrl`)에 중력 토크 G(q)를 피드포워드로 넣는다
- **C. (파생) 키네스테틱 교시** — B 상태에서 녹화 = **리더 팔 없는 단팔 데이터 수집**

---

## 1. 현재 상태 — 무엇이 있고 무엇이 없나

| 있는 것 | 위치 | 한계 |
|---|---|---|
| 수동 조작 슬라이더 | [ManualControlPanel.tsx:24](../frontend/src/components/ManualControlPanel.tsx#L24) → `/params/manual-action` ([params.py:75](../backend/app/routers/params.py#L75)) → 버스 → [lerobot_wrapper.py:648](../wrapper/lerobot_wrapper.py#L648) | **추론이 떠 있고 일시정지 중일 때만.** 팔만 움직이고 싶어도 정책 로드 + 카메라 + 프로세스 기동이 전제다 |
| 토크 ON/OFF · 파킹 | [robots.py:503-525](../backend/app/routers/robots.py#L503-L525) → robotd RPC | 단발 동작뿐, 임의 자세로 못 보낸다 |
| 마스터 모드 드래그 | [arm.py:146](../robot/piper_robot/arm.py#L146) `MasterSlaveConfig(0xFA)` | 토크가 꺼져 **팔이 처진다.** 피드백(0x2Ax) 송신도 멈춘다 |
| 명령 깔때기 | shm action → [safety.filter_goal](../robot/piper_robot/safety.py#L112) → CAN ([publish.py:198-236](../robot/piper_robot/publish.py#L198-L236)) | "웹 수동 제어"는 넷 중 하나로 **설계에 이름만 있고**([robotd.py:6](../daemons/robotd.py#L6), [safety.py:27](../robot/piper_robot/safety.py#L27)) 소비자 구현이 없다 |
| MIT 모드 | piper_sdk 0.6.1 `JointMitCtrl` / `MotionCtrl_2(…, 0xAD)` + 공식 데모 동봉 | 코드베이스 어디서도 안 쓴다 |

요컨대 **A는 길이 다 깔려 있고 소비자만 없다. B는 SDK가 지원하고 우리가 안 썼다.**

---

## 2. A. 웹 조그 — 깔린 길에 소비자 하나

### 설계

```
[RobotsPage 조그 패널] ─REST→ [게이트웨이 JogSession] ─shm ActionWriter→ [robotd ArmBridge]
                                                          → filter_goal → JointCtrl
```

1. `POST /api/robots/jog/start {iface}` — 배타 가드 통과 후 게이트웨이가
   `ActionWriter(iface, deadman_ms=…)` 를 연다.
   ⚠ **라이터는 이중 열기를 막아주지 않는다** — `O_CREAT` 라 기존 세그먼트를 조용히
   덮는다 ([arm.py:160](../shm/piper_shm/arm.py#L160)). "세그먼트 존재 = 조종 중"은
   관례지 강제가 아니므로, **열기 전에 세그먼트 존재를 확인하고 있으면 거절**한다
   (추론 프록시가 조종 중이라는 뜻이다)
2. `POST /api/robots/jog/goal {iface, values}` — **전체 관절의 절대 목표**(정규화 좌표)를
   세션에 반영한다. 기존 패널의 50ms 디바운스 절대 목표 방식 그대로
   (`ActionWriter.publish` 는 전 관절을 요구한다 — 부분 목표는 세션이 직전 값과 병합)
3. 세션은 **마지막 목표를 ~10Hz 로 재발행**한다. 목표 한 번에 팔이 1초쯤 움직이는데,
   재발행 없이는 데드맨(기본 300ms)이 중간에 팔을 세워 슬라이더 UX가 깨진다.
   재발행 = "의도가 살아있다"는 신호. 브라우저가 죽어도 팔은 **마지막 목표까지 가서 선다**
   (위치 모드라 유한한 동작) — 게이트웨이가 죽으면 그때 데드맨이 잡는다.
   목표 갱신이 N분 없으면 세션 자동 종료
4. `POST /api/robots/jog/stop` — writer 닫기 + 세그먼트 unlink → 브리지가 "소비자 종료" 처리

### 안전은 전부 공짜다

| 위험 | 막는 것 (이미 있음) |
|---|---|
| 슬라이더를 반대 끝으로 홱 던짐 | `max_step` 20/스텝 램프 ([safety.py:78](../robot/piper_robot/safety.py#L78)) |
| 게이트웨이 사망·hang | 데드맨 → 현재 자세 유지 ([publish.py:181-196](../robot/piper_robot/publish.py#L181-L196)) |
| 범위 밖 목표 | `clamp_range` ([safety.py:98](../robot/piper_robot/safety.py#L98)) |
| 추론과 동시 조종 | 배타 가드 + 세그먼트 점유 확인(위 1번) |

**robotd 변경 0줄, 새 안전 코드 0줄.** "필터를 CAN 쥔 쪽에 둔다"([robotd-safety](../refactor/robotd-safety.md))의 배당금이다.

### 필요한 것

| 층 | 일 |
|---|---|
| 게이트웨이 | `JogSession` 서비스(ActionWriter 수명 + 병합 + 재발행), 라우터 3개, `Activity.MANUAL_JOG` ([exclusivity.py:30](../backend/app/services/exclusivity.py#L30)). backend 는 이미 `piper_shm` 을 쓴다 ([shm_publisher.py](../backend/app/services/shm_publisher.py)) |
| 프론트 | [ManualControlPanel](../frontend/src/components/ManualControlPanel.tsx) 을 목적지 주입형으로 일반화해 RobotsPage 에도. 관절 min/max 는 이미 공유 ([config/joints.ts](../frontend/src/config/joints.ts)) |
| robotd | **없음** |

그리퍼 포함(JOINT_ORDER 그대로). 움직임 속도의 상한은 safety 의 `max_step` 램프가
결정한다 — 더 느린 조그가 필요하면 필터 설정을 만지지 말고 프론트에서 목표를 보간한다
(필터 설정은 방어선이라 하나로 유지).

---

## 3. B. MIT 중력 보상 — 설계

### SDK 사실 (piper_sdk 0.6.1 실측 + 동봉 데모)

| 항목 | 내용 |
|---|---|
| 모드 진입 | `MotionCtrl_2(0x01, 0x04, 0, 0xAD)` — CAN 제어 + MOVE M + MIT. 공식 데모는 **매 사이클 보낸다** (`demo/V2/V2_piper_ctrl_joint_mit.py`) |
| 관절 명령 | `JointMitCtrl(n, pos_ref, vel_ref, kp, kd, t_ref)` — 0x15A~F. pos **rad** ±12.5 · vel ±45 · kp 0~500(참조 10) · kd ±5(참조 0.8) · **t_ref ±18** |
| 펌웨어 | MOVE M 은 **V1.5-2 이상** — 연결 시 읽는 `arm.firmware` 로 확인 ([arm.py:76-81](../robot/piper_robot/arm.py#L76-L81)) |
| 미확정 | 다른 데모(`piper_set_mit.py`)는 `(0x01, 0x01, 0, 0xAD)` — MOVE J+MIT 조합을 쓴다. 어느 쪽이 맞는지 실기로 확정 (§8) |

중력 보상 = **kp=0, kd=소량(감쇠), t_ref=G(q)**. 위치 오차 항이 없으니 팔엔 목표가 없고,
중력 토크만 상쇄돼 손으로 밀면 밀리는 대로 간다. 단위는 SI(rad·Nm)다 —
정규화(-100..100)는 여기 안 낀다. raw(0.001°) ↔ rad 변환만
[joints.py](../robot/piper_robot/joints.py) 에 추가한다.

### 루프는 robotd 안에 산다

이유 둘: ① CAN 소유자가 robotd 다(3b-5) — 다른 프로세스가 하려면 소유권을 나누거나 토크
채널을 새로 파야 한다. ② 100~200Hz 재계산 루프라 프로세스 경계(RPC·컨테이너)를 매 사이클
넘으면 지연·지터를 사서 들인다.

```
robotd
 ├ publish 루프 (100Hz 상태 발행)   ← 그대로 돈다 — 드래그 중에도 웹에 관절이 산다
 ├ command 루프 (shm 소비)          ← mit_active 면 CAN 송신을 멈춘다 (모드 가드, 아래)
 └ gravity 루프 (신규 100~200Hz)    q 읽기(CAN 캐시, 왕복 없음) → τ=G(q) → 클램프 → JointMitCtrl ×6
```

버스 RPC 두 개 추가: `gravity_start(iface, params)` / `gravity_stop(iface)`
([robotd.py:47](../daemons/robotd.py#L47) `_METHODS`).

### G(q) — 무거운 건 오프라인에, robotd 는 numpy 만

[robotd-safety](../refactor/robotd-safety.md)가 못 박은 원칙: robotd 는 호스트 배포 + 경량
의존, pinocchio 같은 무거운 것 대신 numpy. 중력 보상은 FK가 아니라 **동역학**(질량·질심·관성)이
필요하지만 원칙은 지킬 수 있다:

1. **URDF(관성 파라미터 포함) 확보 — 트랙 E와 같은 선결 조건이다.**
   이제 URDF 하나가 두 기능(기구학 안전 필터·중력 보상)을 먹인다
2. 오프라인에서 pinocchio 로 G(q) = RNEA(q, 0, 0) 를 **코드 생성** → 순수 numpy 함수
   (`kinematics/gravity.py`, 생성 스크립트와 입력 URDF 해시를 주석으로 박제)
3. 말단 페이로드(그리퍼 교체·카메라 마운트)는 질량·질심 파라미터로 —
   **기기별 설정**이다 (ROADMAP "기기별 설정 분리", `settings.config_dir`)

### 안전 — 기존 위치 필터가 한 개도 안 먹힌다

`filter_goal` 은 **위치 목표**의 필터다. MIT 는 토크를 보낸다 — 기존 안전층이 통째로
비켜간다. 같은 철학(순수 함수, 하드웨어 없이 시험, 리플레이)으로 토크용을 새로 둔다:

| 방어선 | 내용 |
|---|---|
| 토크 클램프 | 관절별 \|τ\| ≤ max\|G(q)\|·여유율. 상한은 **리플레이로 정한다** — 기존 데이터셋의 q 궤적 전체에 G 를 적용해 분포부터 본다 ([safety.py](../robot/piper_robot/safety.py) 도입부와 같은 방법). SDK 한계 ±18 Nm 를 그대로 쓰지 않는다 |
| 폭주 가드 | \|dq/dt\| 임계 초과(휘두름·모델 발산) → 즉시 이탈 |
| 리밋 가드 | 관절이 캘리브레이션 범위 끝 N% 안에 들면 → 이탈. 가상 벽 스프링은 2차(에너지 주입이라 신중히) |
| E-stop | estopd 가 죽일 **프로세스가 아니라 robotd 내부 모드다.** gravity 루프가 버스의 E-stop 신호를 직접 구독하고, 걸리면 이탈 시퀀스로 나온다 |
| 무인 방치 | N분 무움직임 → 자동 이탈 (기본 5분) |
| 이탈 시퀀스 | q 읽기 → 위치 모드 복귀(`ModeCtrl(0x01,0x01,30,0x00)` + `JointCtrl(q)`) → 그 자리 유지. "정지 = 그 자리에 서기" ([publish.py:187](../robot/piper_robot/publish.py#L187)) 그대로 |

**정직한 미지수**: robotd 가 MIT 모드 중 죽으면 펌웨어가 마지막 토크를 유지하는지 0으로
떨어뜨리는지(=낙하) 모른다. 실기 확인 1순위다(§8) — 유지라면 팔이 서서히 표류한다.
어느 쪽이든 이 기능은 **사람이 팔 옆에 있는 것이 전제**라는 운영 수칙이 최종 방어선이다.

### 모드 가드 — C 를 공짜로 만드는 규칙

`mit_active` 동안 command 루프는 shm 명령을 읽되 **CAN 으로 보내지 않는다.** 이 가드가:

1. 위치 명령과 MIT 토크가 같은 팔에서 싸우는 것을 막고 (안전)
2. **키네스테틱 녹화를 공짜로 만든다** — LeRobot 레코더는 여느 때처럼 action 을 shm 에
   쓰고(데이터셋에는 기록됨) 팔로는 안 나간다. 팔은 사람 손이 움직인다

### 브링업 — 스케일을 올려가며

`τ = s·G(q)`, **s=0.3 부터.** 덜 보상되면 팔이 천천히 처진다 — 잡을 수 있다.
s 를 0.1 씩 올려 1.0 에서 **홀드 테스트**: 임의 자세에서 놓았을 때 머물러야 한다.
표류하면 필터가 아니라 모델(질량·질심·페이로드)이 틀린 것이다. **s>1 금지** — 에너지를
주입해 스스로 가속한다. 드래그가 뻑뻑하면(Piper 관절 마찰) 우선 kd 를 낮춰본다
(시작 0.3~0.8) — 마찰 보상 항은 방향 추정이 필요한 2차 과제로 미룬다.

---

## 4. C. 키네스테틱 교시 녹화 (파생 — B 뒤)

리더 팔 없이 한 팔로 데이터 수집: 중력 보상 켜고, 손으로 끌고, 녹화한다.
양팔 세트가 없어도 팔 하나로 시연 데이터를 만들 수 있다.

- **팔쪽은 §3 모드 가드로 끝** — robotd 추가 변경 없음
- LeRobot 쪽: "자기 상태를 액션으로 되돌려주는" Teleoperator 하나 —
  state shm 세그먼트를 읽어 그대로 action 으로 반환.
  [vendor/lerobot_robot_pipershm](../vendor/lerobot_robot_pipershm) 의 state 읽기 재사용.
  데이터셋의 action = 실측 관절 (리더-팔로워 녹화와 형식 동일)
- **남는 결정은 그리퍼 하나** — 손으로 팔을 끌면 그리퍼 여닫기는 누가 하나.
  후보: 조그 패널의 그리퍼 슬라이더만 모드 가드 예외로 살리기 / 풋 페달
  (evdev 키 입력 주입 인프라가 이미 있다)

---

## 5. 기존 인프라와의 배선

| 배선 | 내용 |
|---|---|
| robotd RPC | `_METHODS` 에 `gravity_start`/`gravity_stop` ([robotd.py:47](../daemons/robotd.py#L47)). 조그는 RPC 불필요(shm) |
| 배타 가드 | `Activity.MANUAL_JOG`·`GRAVITY_COMP` 등록 — INFERENCE·RECORDING 과 상충. 단 **GRAVITY_COMP + RECORDING 조합은 C 의 정상 상태**라 예외 규칙이 필요하다 ([exclusivity.py:60](../backend/app/services/exclusivity.py#L60)) |
| WS 계약 | `robot_mode` 이벤트(mit 진입/이탈/이탈 사유) — [ws_messages.py](../backend/app/core/ws_messages.py) + [types/ws.ts](../frontend/src/types/ws.ts) 판별 유니언에 타입으로 합류 |
| 텔레메트리 | gravity 루프의 τ·클램프 발동·이탈 사유를 브리지 진단 카운터([publish.py:68-73](../robot/piper_robot/publish.py#L68-L73))처럼 노출 |
| 프론트 | RobotsPage: 조그 패널(재사용) + 중력 보상 토글. 마스터/슬레이브 토글 옆이 자연스럽다 — 같은 "팔 상태" 의미론 |
| E-stop | gravity 루프가 버스 신호 구독(§3). estopd 자체는 손대지 않는다 |

---

## 6. 순서

| 단계 | 내용 | 전제 | 비고 |
|---|---|---|---|
| 1 | **웹 조그** — JogSession + 패널 재배치 | robotd(3b-5) ☑ · #10 ☑ → **없음** | **지금 가능** |
| 2 | **MIT 스파이크** — 공식 데모 그대로 관절 6번 하나, 웹 무관. §8 앞 4개 확인 | 팔 1대 + 사람 | URDF 없이 **지금 가능**, 1과 병렬 |
| 3 | G(q) 자산 — URDF 확보 → 오프라인 코드젠 → 홀드 테스트 하네스 | **트랙 E** | URDF 는 외부 자산, 리드타임 있음 |
| 4 | robotd gravity 루프 + 토크 안전층 + 웹 토글 | 2·3 | |
| 5 | 키네스테틱 녹화 — 에코 Teleoperator + 그리퍼 입력 결정 | 4 | |

1·2가 서로 독립이라 병렬 가능. **트랙 E(URDF)의 수혜자가 둘이 됐다** —
ROADMAP 의 "URDF 확보는 지금 바로 시작한다"가 더 강해진다.

---

## 7. 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| **마스터 모드로 드래그 교시** (이미 있음) | 토크가 꺼져 팔이 자기 무게로 처진다 — 팔을 끄는 게 아니라 드는 일이 된다. 피드백(0x2Ax)도 멈춰 상태 읽기가 0x15x 경로로 바뀌고, 전원 재투입에 풀리며 역할 판별과 얽힌다 ([arm.py:88-97](../robot/piper_robot/arm.py#L88-L97)) |
| **RPC 단발 조그** (`move_joints` RPC 추가) | robotd 에 두 번째 명령 입구가 생긴다. "세그먼트 존재 = 조종 중" 점유 신호가 안 걸리고 데드맨도 없다. 깔때기는 하나여야 한다 ([publish.py:17-19](../robot/piper_robot/publish.py#L17-L19)) |
| **게이트웨이에서 gravity 루프** | 100~200Hz 재계산이 컨테이너·버스 경계를 매 사이클 넘는다. CAN 소유권 원칙(3b-5) 위반 |
| **pinocchio 를 robotd 런타임 의존으로** | robotd 는 호스트 배포 + 경량 의존 ([robotd-safety](../refactor/robotd-safety.md)). 오프라인 코드젠이면 런타임엔 numpy 뿐 |
| **카테시안 조그(`EndPoseCtrl`) 1단계 포함** | 펌웨어 IK 를 믿어야 하고, 안전층이 관절 공간이라 데카르트 목표는 스텝 램프를 우회한다. 관절 조그가 선 뒤에 |
| **B 없이 C** (토크 끄고 끌며 녹화) | 마스터 모드의 문제 그대로 — 팔이 처져 자세를 못 잡고, 안전층·상태 발행 경로 밖이다 |

---

## 8. 실기 확인 체크리스트

- [ ] 펌웨어 ≥ V1.5-2 (두 팔 다 — `arm.firmware`)
- [ ] 모드 조합: `MotionCtrl_2(0x01,0x04,0,0xAD)` vs `(0x01,0x01,0,0xAD)` 어느 쪽이 실제로 먹나
- [ ] MIT 진입 순간 팔이 튀는가 (직전 위치 목표 잔재)
- [ ] **MIT 중 `kill -9` robotd — 팔을 잡고.** 잔류 토크 유지인가, 낙하인가
- [ ] 이탈 시퀀스: `JointCtrl(현재 q)` 복귀가 점프 없이 되나, 재-`EnablePiper` 가 필요한가
- [ ] `GetArmHighSpdInfoMsgs` 전류로 정지 자세의 G(q) 부호·크기 감 잡기 (모델 방향 검증)
- [ ] 조그 데드맨: goal 재발행 중 게이트웨이 kill → 팔이 서나
