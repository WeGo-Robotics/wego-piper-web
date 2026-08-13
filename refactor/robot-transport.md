# 로봇 전송 — robotd가 CAN을 잡고 프록시 드라이버가 통신

[daemon-inventory.md](daemon-inventory.md)의 부속 문서. [camera-transport.md](camera-transport.md)와 같은 구조를 로봇에 적용한다.

## 결정

LeRobot이 CAN을 직접 여는 대신, **`piper-robotd`가 CAN을 영구 소유**하고
LeRobot 쪽은 `/dev/shm`으로 통신하는 **프록시 드라이버**를 쓴다.
카메라와 동일하게 **LeRobot 수정 0, wrapper 수정 0** — 같은 서드파티 플러그인 경로를 탄다.

---

## 자를 자리는 `Robot`이 아니라 `MotorsBusBase`

`PiperFollower`(175줄)는 이미 얇다. 실제 CAN 접근은 전부
`self.bus`(`PiperMotorsBus`, 227줄 → `C_PiperInterface_V2`)에 있다.

| 자를 층 | 재구현할 것 | 위험 |
|---|---|---|
| `Robot` | feature dict 구성, 카메라 병렬 읽기, 안전 클램핑까지 전부 | 계약 드리프트 |
| **`MotorsBusBase`** | **버스 메서드 ~18개(전부 얇음)** | **낮음** ← 채택 |

`MotorsBus` 층에서 자르면 `PiperFollower`를 **그대로 재사용**한다:

- `observation_features`/`action_features`가 [`self.bus.motors`에서 파생](../vendor/lerobot_robot_piper/lerobot_robot_piper/piper_follower.py#L66)되므로
  **자동으로 일치한다** — 정책/데이터셋 계약이 어긋날 여지가 없다
- 카메라 병렬 읽기(`_camera_executor`)는 [camera-transport.md](camera-transport.md)의 shm 카메라가 그대로 들어간다
- `ensure_safe_goal_position` 클램핑 로직도 그대로

### 패키지

```
vendor/lerobot_robot_pipershm/
  pyproject.toml                       # name = "lerobot_robot_pipershm"
  lerobot_robot_pipershm/
    __init__.py                        # PiperShmFollower export
    config_pipershmfollower.py         # @RobotConfig.register_subclass("piper_follower_shm")
    pipershmfollower.py                # class PiperShmFollower(PiperFollower)  ← __init__만 오버라이드
    shm_motors_bus.py                  # class PiperShmMotorsBus(MotorsBusBase)
```

`PiperShmFollower`는 `PiperFollower`를 상속해 **`__init__`에서 버스만 바꿔 끼운다.** 30줄이면 된다.

메커니즘은 카메라와 동일하다 —
[`register_third_party_plugins()`](../wrapper/lerobot_wrapper.py#L225)가 `lerobot_robot_` 접두사 배포판을
자동 import하고, `make_robot_from_config`의 마지막 `else`가
`make_device_from_device_class(config)`로 빠진다. `vendor/lerobot_robot_piper/`가 이미 이 경로를 쓰고 있다.

바뀌는 곳은 [`robot_type`을 정하는 지점](../backend/app/routers/models.py#L262) 하나다
(→ [09-robot-type.md](09-robot-type.md)가 5곳에 흩어져 있다고 지적한 바로 그 값이므로 **먼저 정리하는 게 좋다**).

---

## 왜 이게 성립하는가 — 블로킹이 없다

`PiperMotorsBus`의 메서드를 확인한 결과, **CAN을 기다리는 호출이 하나도 없다.**

| 메서드 | 실제 동작 |
|---|---|
| `get_action()` | `piper.GetArmJointMsgs()` — piper_sdk 백그라운드 스레드가 채운 **캐시 읽기** |
| `get_control()` | `GetArmJointCtrl()` — 캐시 읽기 |
| `set_action()` | `ModeCtrl`/`JointCtrl`/`GripperCtrl` — **fire-and-forget 송신** |
| `sync_read()` | 캐시 읽기 |

즉 제어 루프 한 사이클 안에 **왕복 대기가 없다.** 그래서 shm으로 그대로 옮길 수 있다:

- **상태**: robotd가 자기 주기로 CAN 캐시를 읽어 shm에 발행 → 소비자는 최신값 복사만 (왕복 없음)
- **명령**: 소비자가 shm 슬롯에 쓰고 seq 증가 → robotd가 폴링해 CAN 송신

> ⚠ **명령 경로는 [robotd-safety.md](robotd-safety.md)에서 수정된다.**
> robotd에 기구학 충돌 필터가 들어가면 명령이 변형되므로 **적용값이 돌아와야 한다**
> (`applied` 세그먼트 + ack 대기). 순수 fire-and-forget이 아니게 된다.

관절 상태는 7 float + 에러 플래그로 **56바이트 수준**이다. 카메라 프레임(6MB)과 달리
복사 비용이 나노초라 전송 방식 자체가 성능 문제가 되지 않는다.

> ⚠ 이건 **현재 piper_sdk가 캐시 방식**이라는 데 기댄 판단이다.
> SDK 업그레이드 시 `GetArmJointMsgs()`가 블로킹으로 바뀌면 전제가 깨진다. 이행 시 재확인할 것.

---

## 세그먼트 설계

카메라와 같은 seqlock 패턴을 쓰되, **방향이 둘**이다.

```
/dev/shm/piper.arm.<iface>.state     robotd → 소비자   (발행)
┌──────────────────────────────────────────┐
│ 헤더: magic│version│n_slots│write_seq│... │
│ slot[0..2]: joint1..6, gripper (float32) │
│             err_code u16, ctrl_mode u8   │
│             can_wall_ns u64  ← CAN 수신 시각 │
└──────────────────────────────────────────┘

/dev/shm/piper.arm.<iface>.action    소비자 → robotd   (명령)
┌──────────────────────────────────────────┐
│ 헤더: magic│version│write_seq│deadman_ms  │
│ slot[0..2]: goal joint1..6, gripper       │
│             issued_wall_ns u64            │
└──────────────────────────────────────────┘
```

`can_wall_ns`를 실어야 소비자가 **상태의 신선도**를 판단할 수 있다.
지금은 캐시를 읽으면서 그게 얼마나 오래된 값인지 알 방법이 없다 — 이 설계가 그걸 개선한다.

### 역할 분담

| | 담당 |
|---|---|
| **shm** | 핫패스만 — 관절 상태 발행, 목표 위치 명령 |
| **Redis** | 나머지 전부 — connect/disconnect, 캘리브레이션, master/slave 전환, 파킹, 에러 클리어, 프리셋 |

Redis 쪽은 초당 수 회 수준이고 요청/응답 의미론이 필요해서 버스가 맞다.

---

## 얻는 것

### 1. CAN quiesce 문제가 사라진다 — 마지막 장치 중재 이슈

[daemon-inventory.md](daemon-inventory.md)에 남아 있던 유일한 난제였다. 지금은
[`_clear_arm_errors`](../backend/app/routers/models.py#L77) 주석이 규칙을 이렇게 적고 있다:

> *"팔과의 CAN 통신은 백엔드 robot_manager가 직접 보유하므로(추론 subprocess와 별개),
> 시작은 subprocess 기동 전에, 종료는 subprocess 정지 후에 호출하여 버스 경합을 피한다."*

**robotd가 CAN을 영구 독점하면 경합 자체가 없다.** 호출 순서로 표현되던 암묵적 프로토콜이
사라지고, `daemon-split.md` 4단계에서 quiesce 상태를 설계할 필요도 없어진다.

### 2. 컨테이너가 완전히 깨끗해진다

카메라(shm) + 로봇(shm)이면 소비자 컨테이너에 하드웨어가 **하나도** 안 남는다.

| | 지금 | 카메라 shm 후 | + 로봇 shm 후 |
|---|---|---|---|
| `privileged: true` | 필요 | 불필요 | 불필요 |
| `/dev:/dev` | 필요 | 불필요 | 불필요 |
| `/run/udev`, `/lib/modules` | 필요 | 불필요 | 불필요 |
| `network_mode: host` (CAN) | 필요 | 필요 | **불필요** |
| `ipc: host` | — | 필요 | 필요 |
| GPU | 필요 | 필요 | 필요 |

**GPU + `ipc: host`만 남는다.** Redis는 유닉스 소켓을 볼륨으로 마운트하면 네트워크도 필요 없다.

### 3. robotd가 최저층 데드맨이 된다 — 안전상 가장 큰 수확

명령이 fire-and-forget이므로 **robotd는 소비자가 살아있는지를 seq 증가로 안다.**
`deadman_ms` 동안 action seq가 안 늘면 정지시킨다.

이건 지금 없는 방어선이다. 현재 E-stop watchdog은
[웹서버와 같은 이벤트 루프](../backend/app/services/estop_watchdog.py#L43)라 루프가 막히면 같이 멈춘다
(D405 UVC hang 때 실제로 그랬다). robotd의 데드맨은 **웹서버·게이트웨이·버스와 무관하게**
CAN을 쥔 프로세스 안에서 돈다. 추론 프로세스가 hang·크래시·OOM 어느 쪽으로 죽어도 팔이 선다.

### 4. 텔레오퍼레이션·양팔이 한 곳에 모인다

leader 팔 읽기(`piper_leader.py`), master/slave 전환(`set_master`/`set_slave`),
에러 클리어가 전부 robotd 안으로 들어간다.
지금은 백엔드 `robot_manager`와 LeRobot 드라이버가 같은 팔을 각자의 경로로 만진다.

---

## 위험 — 정직하게

### 1. 명령 경로에 폴링 지연이 붙는다

소비자가 shm에 쓰고 robotd가 폴링해 송신하므로 **폴링 주기만큼 지연이 추가**된다.
1kHz 폴링이면 최대 +1ms — 30fps 제어(33ms)의 3%다. 허용 범위지만 **실측이 필요하다.**
문제가 되면 eventfd/futex로 µs 수준까지 내릴 수 있다.

### 2. 캘리브레이션 소유자를 하나로 정해야 한다

`_normalize`/`_unnormalize`가 `self.calibration`을 쓴다. robotd와 프록시가 각자 캘리브레이션을
들면 [05-joint-calibration.md](05-joint-calibration.md)가 지적한 중복이 **2곳에서 3곳으로 는다.**

→ **robotd를 캘리브레이션 단일 소유자로 두고, shm에는 정규화된 값만 흐르게 한다.**
프록시 버스의 `_normalize`는 항등 함수가 되거나 아예 없어진다.

### 3. 안전 클램프가 소비자 쪽에 남는다

`max_relative_target` 클램핑([`ensure_safe_goal_position`](../vendor/lerobot_robot_piper/lerobot_robot_piper/piper_follower.py#L148))이
프록시 안, 즉 컨테이너 안에 있다. 그 프로세스가 오작동하면 무방비다.

→ **robotd에도 하드 리밋을 둔다** (관절 범위, 최대 변화량). 이중 방어이고, 어차피 데드맨을
넣는 김에 같은 자리다. 소비자 쪽 클램프는 그대로 두되 신뢰하지 않는다.
기구학 기반 충돌 방지까지 여기서 다룬다 → [robotd-safety.md](robotd-safety.md)

### 4. 프로세스가 하나 더 늘어난다

CAN 경로에 홉이 하나 추가된다. 그만큼 실패 지점도 하나 는다.
다만 robotd는 **지금 백엔드 `robot_manager`가 이미 하고 있는 일**이라 새 코드가 아니라 이사다.

---

## 착수 순서

1. ☑ **[09-robot-type.md](09-robot-type.md) 먼저 정리** — `robot_type` 값이 5곳에 흩어져 있어서
   새 타입을 추가하면 5곳을 고쳐야 한다
2. ☑ 세그먼트 포맷 확정 ([shm/piper_shm/arm.py](../shm/piper_shm/arm.py)).
   상태 40B / 명령 36B, seqlock 슬롯 3개, `deadman_ms` 는 **소비자가 선언**한다
3. ☑ [`PiperShmMotorsBus`](../vendor/lerobot_robot_pipershm/) + 임시 발행
   ([arm_bridge.py](../backend/app/services/arm_bridge.py)). `settings.robot_transport`
   스위치로 넣었다 — 되돌리기가 값 하나다. **실기 확인**: 상태가 직접 읽기와 9e-7 차이,
   `set_action` 지연 중앙 0.058ms, 프록시 60발행 → CAN 60송신, seqlock 재시도 0
4. robotd 분리 (CAN 독점 + 상태 발행 + 명령 소비 + **데드맨** + 하드 리밋).
   `arm_bridge.py` 의 내용이 그대로 이사한다
5. `_clear_arm_errors` 타이밍 춤 제거, `robot_type`을 프록시로 전환
6. 컨테이너에서 `network_mode: host` 제거

3단계가 핵심이다 — 카메라와 같은 이유로 **데몬 분리와 전송 변경을 동시에 하지 않는다.**

## 실기에서 걸린 것

- **`read_new` 를 `StateReader` 에만 달았다.** 명령을 소비하는 쪽이 `AttributeError` 로
  매번 재접속했는데 상태 경로만 테스트해서 못 잡았다 → 방향이 둘인 세그먼트는
  **양쪽 표면이 같아야 한다** (테스트로 강제).
- **소비자 접속 폴링이 0.2초였다.** 30fps 기준 첫 4개 명령이 통째로 날아갔다.
  `/dev/shm` stat 한 번이라 촘촘해도 공짜다 → 20ms.
- `vendor/lerobot_robot_piper/` 는 **설치되지 않는 사본**이다. 상류는 별도 저장소
  (`WeGo-Robotics/lerobot_robot_piper`)라 상수를 공유할 수 없어, 모터·캘리브레이션은
  프록시 쪽에 복사하고 **테스트가 상류 소스와 대조**한다.

## 검증

- **정규화 값이 기존과 동일한지** — 같은 자세에서 직접 드라이버와 프록시의 `get_observation()` 비교.
  여기가 어긋나면 정책이 조용히 틀린 관측을 받는다
- `observation_features`/`action_features` dict가 기존과 완전히 같은지 (키 이름·순서·타입)
- 추론 1회 / 녹화 1 에피소드 / 텔레오퍼레이션 정상 동작
- **제어 루프 지연 실측** — 기존 대비 사이클 시간, 특히 `send_action` 경로
- **데드맨**: 추론 프로세스를 `SIGKILL` → 팔이 정지하는지, 몇 ms 안에 서는지 (4단계)
- **하드 리밋**: 프록시를 우회해 범위 밖 목표를 shm에 직접 써 넣고 robotd가 거부하는지
- robotd 재시작 후 소비자가 재접속하는지 / 소비자 재시작 후 robotd가 살아있는지
- 세그먼트 누수 없는지 (`ls /dev/shm/piper.arm.*`)
- 양팔 구성에서 두 팔이 독립적으로 동작하는지

## 상태

☐ 미착수 — 설계 확정, 구현 대기
