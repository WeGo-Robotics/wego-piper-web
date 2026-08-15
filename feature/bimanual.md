# 양팔(bimanual) — "실행만 되는" 상태에서 수집→학습→실행 전체 경로로

> **◐ 소프트웨어 전 구간 구현됨 (§5 의 1~3단계) — 실기 검증만 하드웨어 대기.**
>
> - **bi 클래스 4개**: `bi_piper_follower`/`bi_piper_leader` (WeGo repo,
>   [config_bi_piper.py](../vendor/lerobot_robot_piper/lerobot_robot_piper/config_bi_piper.py)) +
>   `bi_piper_follower_shm`/`bi_piper_leader_shm`
>   ([bipipershm.py](../vendor/lerobot_robot_pipershm/lerobot_robot_pipershm/bipipershm.py)).
>   상류 관용구 그대로 — 서브클래스는 `arm_class`/`arm_config_class` 만 갈아끼운다.
>   **함정 하나를 밟았다**: 중첩 필드에 등록형 설정을 쓰면 draccus 인자 등록이
>   무한 재귀한다. 상류가 `SOFollowerConfig`(평면)/`SOFollowerRobotConfig`(등록형)를
>   나눈 이유가 그것이라, 우리도 `PiperArmConfig`(평면)를 분리했다.
> - **녹화**: recording.py 가 `robot_ports`/`teleop_ports` 를 받아 중첩 인자를
>   조립한다. 웹이 만드는 CLI 를 실제 `RecordConfig` 로 파싱해 로봇·텔레옵
>   팩토리까지 통과 확인 — action 14축, 관측 키 `left_top`/`left_hand`/`right_hand`.
> - **추론**: 로컬도 열렸다 (`--robot-ports` → [robot_factory](../wrapper/robot_factory.py)).
>   grpc_wrapper 의 즉석 조립(236-311)은 **삭제** — 결함 ①(left 기준 features)②(파킹)
>   가 예고대로 소멸했다. ParkingController 는 양팔 병렬 2단계.
> - **좌/우 박제**: `ArmInfo.side` — 슬롯 페어 번호가 기본(1=왼, 2=오른),
>   `/api/robots/side` 로 스왑, 세션에 저장. 녹화·추론 화면이 side 로 프리필한다.
> - **관절 수 가드**: `/inference/validate` 가 팔 수 × 7 로 검증 — 14축 정책이
>   7 에 막혀 시작 불가였던 유일한 하드 거부가 풀렸다. phase 분석기의 조용한
>   7 하드코딩(14축을 왼팔로만 읽던 것)도 수정.
> - **남은 것**: §7 체크리스트 전부 — 팔 4대 + udev 4이름 확장(사람), 실기 fps,
>   SIGKILL/E-stop 양팔 동시 정지, 실제 양팔 에피소드 1개.
핸드오버+재파지(시나리오 우선순위 2)·병뚜껑·수건 접기 — 양팔 데모 전부의 블로커가
"데이터가 없는데 실행 경로만 있는" 상태다.

> 설계 원칙: **양팔 조립을 wrapper 코드에서 LeRobot Robot 클래스로 내린다.**
> 그러면 녹화·로컬 추론·gRPC 추론·파킹이 한 구현을 공유하고,
> G4가 지적한 실행 결함 2건은 고치는 게 아니라 **구조적으로 소멸**한다.

---

## 1. 현재 상태 — 무엇이 되고, 어떻게 되고 있나

| 항목 | 상태 | 근거 |
|---|---|---|
| 양팔 추론 | **gRPC 모드 한정.** wrapper 가 left/right 로봇 2개를 **즉석 조립** — 카메라 `left_`/`right_` 접두사 분배, 액션 동시 전송 | [grpc_wrapper.py:236-264](../wrapper/grpc_wrapper.py#L236-L264) |
| 실행 결함 ① | `lerobot_features` 를 **left 기준으로만** 정책 서버에 전송 — 양팔 정책 feature 정합성 구멍 | [grpc_wrapper.py:255-256](../wrapper/grpc_wrapper.py#L255-L256) |
| 실행 결함 ② | 파킹이 2단계 리프트 없이 동시 파킹 + `sleep(5)` 고정 — 로컬 모드에서 잡은 그리퍼 바닥 긁힘이 이 경로엔 그대로 | [grpc_wrapper.py:288-301](../wrapper/grpc_wrapper.py#L288-L301) |
| 녹화 | `robot_port`/`teleop_port` **단수 고정** — 요청 스키마부터 CLI 매핑까지 | [recording.py:20-23](../backend/app/routers/recording.py#L20-L23), [cli_mapping.py:390-411](../backend/app/core/cli_mapping.py#L390-L411) |
| 로컬 추론 | `robot_ports` 매핑 없음 — 양팔은 gRPC 전용 | [cli_mapping.py:40-45](../backend/app/core/cli_mapping.py#L40-L45) |
| 하드웨어 슬롯 | 2리더/2팔로워 **이미 표현 가능** (`leader_1`·`follower_1`·`leader_2`·`follower_2`) | [robot_manager.py:322-330](../backend/app/services/robot_manager.py#L322-L330) |
| 추론 UI | 양팔 라디오 + left/right 선택 **이미 있음** (gRPC 시작 전용) | [InferencePage.tsx:81](../frontend/src/pages/InferencePage.tsx#L81), [:265](../frontend/src/pages/InferencePage.tsx#L265) |
| robotd/shm | **팔 단위라 이미 양팔 네이티브** — `arms` dict, 브리지·데드맨·세그먼트가 iface 별. [robot-transport §얻는 것 4](../refactor/robot-transport.md)가 "텔레오퍼레이션·양팔이 한 곳에 모인다"고 예견한 그대로 | [hub.py:33](../robot/piper_robot/hub.py#L33), [publish.py:239-251](../robot/piper_robot/publish.py#L239-L251) |

**전송·데몬 계층은 이미 준비돼 있다.** 없는 것은 LeRobot 쪽 양팔 로봇 클래스와
백엔드의 복수화 배선뿐이다.

---

## 2. 핵심 설계 — 조립을 Robot 클래스로 내린다

### 상류 선례가 정확히 이 모양이다

LeRobot 본체에 `bi_so_follower`/`bi_openarm_follower`(로봇), `bi_so_leader`(텔레옵)가 있다:

```python
@RobotConfig.register_subclass("bi_so_follower")
@dataclass
class BiSOFollowerConfig(RobotConfig):
    left_arm_config: SOFollowerConfig      # 중첩 설정 — CLI 는
    right_arm_config: SOFollowerConfig     # --robot.left_arm_config.port=... 로 온다

class BiSOFollower(Robot):
    # 내부에 single 둘. 키는 left_/right_ 접두사로 병합:
    #   observation/action = {f"left_{k}", f"right_{k}"}
```

**접두사 규약이 [grpc_wrapper.py:249-252](../wrapper/grpc_wrapper.py#L249-L252)가 즉석 조립로
이미 쓰는 것과 동일하다** — 즉 지금까지 양팔 추론이 만들던 키 이름과 데이터셋 호환이 깨지지 않는다.

### 우리 것 세 개

| 클래스 | 감싸는 것 | 등록 이름 |
|---|---|---|
| `BiPiperFollower` | `PiperFollower` ×2 | `bi_piper_follower` |
| `BiPiperLeader` | `PiperLeader` ×2 | `bi_piper_leader` |
| `BiPiperShmFollower` | `PiperShmFollower` ×2 (세그먼트 2쌍) | `bi_piper_follower_shm` |

플러그인 자동 등록(`register_third_party_plugins()` 가 `lerobot_robot_*` 배포판을 import,
[config_pipershmfollower.py](../vendor/lerobot_robot_pipershm/lerobot_robot_pipershm/config_pipershmfollower.py) 도입부)이라
**LeRobot 수정 0.** 각각 ~100줄 조립 클래스다.

⚠ **vendor 는 스냅샷이다** — [joints.py](../robot/piper_robot/joints.py) 도입부 경고 그대로,
로컬 vendor 만 고치면 다음 갱신에 덮인다. bi 클래스는 **WeGo repo**
(`lerobot_robot_piper`·`lerobot_robot_pipershm`)에 올리고 vendor 로 다시 받는다.

### 한 클래스가 갚아주는 것

| 경로 | 지금 | bi 클래스 이후 |
|---|---|---|
| 녹화 | 불가 | `lerobot-record --robot.type=bi_piper_follower_shm --teleop.type=bi_piper_leader` — **lerobot-record 본체 수정 없이** 양팔 에피소드 |
| 로컬 추론 | 불가 (gRPC 전용) | `--robot-type=bi_piper_follower_shm` 로 단팔과 같은 경로 |
| gRPC 추론 | 즉석 조립 236-311 | 조립 삭제 — **코드가 줄어드는 기능이다** |
| 결함 ① features | left 기준만 | `map_robot_keys_to_lerobot_features(bi_robot)` 가 14축 전체를 돌려준다 — 소멸 |
| 결함 ② 파킹 | 동시 + `sleep(5)` | `BiPiperFollower.parking()` 하나에 구현 — 두 팔 리프트 → 전축 (로컬 [parking_controller](../wrapper/parking_controller.py) 2단계 로직 이식) |

### 데이터셋 계약

- `observation.state`/`action` = **14축**: `left_joint1.pos … left_gripper.pos, right_…`
- 카메라 키 = `left_wrist`·`right_wrist`·(공용은 left 소속 — grpc 조립이 확립한 규약,
  [grpc_wrapper.py:240-242](../wrapper/grpc_wrapper.py#L240-L242)). top 카메라가 `left_top` 이
  되는 것은 미관 문제일 뿐 무해하다 — **수집과 추론이 같은 클래스를 지나므로 자동으로 일치한다**
- **단팔 데이터셋과 호환되지 않는다** (7축 vs 14축). 핸드오버 정책은 양팔 데이터를 새로 모아야
  하고, 기존 단팔 데이터셋·모델은 그대로 단팔용이다. 정책(ACT·pi0·smolvla)은 상태 차원에
  무관하므로 학습 쪽 변경은 없다

---

## 3. 좌/우는 누가 정하나 — 게이트웨이가, 등록에 박제

지금은 추론 시작할 때마다 드롭다운으로 고른다([InferencePage.tsx:265](../frontend/src/pages/InferencePage.tsx#L265)).
녹화가 생기면 이대로는 위험하다 — **세션 사이에 좌/우가 뒤바뀌면 데이터셋이 거울상으로
오염되고, 학습이 조용히 망가진다.** 캘리브레이션 어긋남과 같은 부류의, 로그에 안 남는 사고다.

원칙은 이미 서 있다: *"`is_master` 는 사실이고 leader/follower 는 해석이다"*
([robot-transport](../refactor/robot-transport.md) 4단계). **좌/우도 해석이다** — 팔에 물어볼 수
없고 사람이 정한다. 따라서 게이트웨이 등록 소유:

- 페어 규약: `leader_N ↔ follower_N` 페어링(모션 감지 슬롯 배정이 이미 이 단위다)에
  **side 를 부여** — 기본 1=왼팔, 2=오른팔, 설정에서 스왑 가능
- 저장 위치: 로봇 세션/설정 (`settings.config_dir` — 기기별 상태의 단일 경계)
- UI: 시작 화면들은 드롭다운 대신 등록된 side 로 **프리필 + 검증**. 바꾸려면 등록을 바꾼다

---

## 4. 기존 인프라와의 배선

| 배선 | 내용 |
|---|---|
| recording.py | `RecordStartRequest` 에 양팔 필드 (left/right port·카메라 접두사 매핑). 단수 필드는 단팔용으로 유지 |
| cli_mapping | `RECORD_ARGS_MAP` 이 평면 매핑이라 중첩 인자(`--robot.left_arm_config.port=…`)를 조건부로 emit 하는 분기 추가. `SHM_ROBOT_TYPES` 에 `bi_piper_follower → bi_piper_follower_shm` 한 줄 ([cli_mapping.py:24](../backend/app/core/cli_mapping.py#L24)) — `resolve_robot_type` 은 그대로 |
| models.py | `robot_ports` 2개 → bi 중첩 인자로 변환. **로컬 추론도 이 시점에 양팔 개방** |
| grpc_wrapper | 즉석 조립(236-311)·`_get_observation`/`_send_action` 분기 삭제 — bi 클래스가 대체 |
| 프론트 | RecordingPage 에 양팔 모드 (InferencePage 라디오 패턴 재사용) + 카메라를 왼팔/오른팔/공용으로 지정하는 매핑 UI |
| phase-annotation | 페이즈 슬롯 설계(7→8)가 단팔 전제 — 양팔은 **14→15.** [#8](../refactor/08-piper-joints.md)의 `robot_joints` 응답이 일반화 지점이다: bi 타입이 14키를 보고하면 프론트·분석기가 따라온다. 하드코딩 "7" 잔재가 있으면 여기서 걸린다 |
| robotd·estopd·안전층 | **변화 0.** 브리지·데드맨·안전 필터가 iface 별로 이미 독립이라, 소비자가 세그먼트를 하나 물든 둘 물든 다를 게 없다 |
| exclusivity·GPU | **변화 0.** 같은 RECORDING/INFERENCE 활동이다 |
| manual-control | 독립 — 조그·중력 보상은 팔(iface) 단위라 양팔과 직교. 양팔 키네스테틱 교시는 두 기능이 다 선 뒤의 조합 |

---

## 5. 순서

| 단계 | 내용 | 전제 | 산출 |
|---|---|---|---|
| 1 | **bi 클래스 3개** — WeGo repo 에 작성, vendor 갱신. `lerobot-record` 를 **웹 없이 손으로** 돌려 양팔 에피소드 1개 검증 | 팔 4대 연결 + udev 규칙 적용(사람, [ROADMAP "사람이 해야 하는 것"](../ROADMAP.md)) | 양팔 데이터셋 포맷 확정 |
| 2 | **좌/우 등록 박제** + recording.py·cli_mapping 복수화 + RecordingPage 양팔 모드 | 1 | **웹에서 양팔 텔레옵 녹화** |
| 3 | **추론 통합** — grpc_wrapper 즉석 조립 삭제, models.py bi 인자, 로컬 추론 개방, `BiPiperFollower.parking()` 2단계 | 1 | 결함 ①② 소멸, 실행 경로 단일화 |
| 4 | 핸드오버 데이터 수집 → 학습 → 평가 | 2·3 | 시나리오 우선순위 2 가동 |

구조 개편과 안 겹친다(robotd 변경 0). [manual-control](manual-control.md)과도 독립 —
병렬 트랙으로 굴려도 안전하다. 1단계의 실질 리스크는 코드가 아니라 **하드웨어 전제**다
(아래 체크리스트 앞 두 줄).

---

## 6. 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| **녹화도 wrapper 즉석 조립** (grpc_wrapper 방식을 record 로 확장) | `lerobot-record` 를 재구현하게 된다 — 에피소드 완결성(브라우저가 죽어도 에피소드를 닫는다)은 LeRobot CLI 가 소유한 가치이고, CLI 래핑이 이 저장소의 1원칙이다. 추론 쪽 즉석 조립도 이번에 **지우는** 마당이다 |
| **robotd 에 양팔 개념** (14축 가상 팔 세그먼트) | robotd 경계는 "팔에 물어봐야 아는 것"([hub.py](../robot/piper_robot/hub.py) 도입부) — 좌/우는 사람의 해석이라 게이트웨이 몫이다. 팔 단위 브리지·데드맨·안전층이 양팔에서 그대로 성립하는데 결합만 늘린다 |
| **iface 이름으로 좌/우 고정** (`can0`=왼팔) | CAN 이름은 포트를 바꿔 꽂으면 뒤바뀐다 — **실제로 겪었고**, 등록이 살아 있었으면 leader 팔에 follower 명령이 갔을 사고다 (ROADMAP). 이름이 아니라 등록(슬롯 페어)에 side 를 묶는다 |
| **두 팔 따로 녹화 후 병합** | 프레임 동기화·타임스탬프 정합을 사후에 만들 수 없다. LeRobot 포맷에 병합 도구도 없다 |
| **접두사 없는 단일 네임스페이스** (`joint1..12`) | 상류(bi_so_follower)·기존 gRPC 조립·이미 존재할 수 있는 양팔 gRPC 산출물 전부와 어긋난다 |

---

## 7. 검증 체크리스트

- [ ] **CAN 어댑터 4개 동시** — 같은 xHCI 컨트롤러에 몰리면 위험하다 (컨트롤러 통째 사망
      이력 → [hub.py `recover_usb`](../robot/piper_robot/hub.py#L192)). 카메라와 토폴로지 분산 확인
- [ ] udev 규칙을 4개 이름으로 확장, 재부팅·재연결에도 slot↔iface 안정
- [ ] 텔레옵 fps: 리더 2(직접 CAN 읽기) + 팔로워 2(shm 경유) 30fps 유지 — shm 지연 실측은
      단팔 기준 0.089ms ([robot-transport "실기"](../refactor/robot-transport.md)), 양팔 재측정
- [ ] 스트리밍 인코딩: 카메라 3~4 스트림 동시 녹화 부하 (USB3 로 4스트림 15fps 는 확인됨 — ROADMAP)
- [ ] 녹화 프로세스 `SIGKILL` → **두 팔 다** 데드맨 정지 (브리지별 독립인지 실기로)
- [ ] E-stop → 양팔 동시 정지
- [ ] bi 데이터셋을 phase-annotation 분석기에 통과 — "7" 하드코딩 잔재 검출
- [ ] 추론 통합(3단계) 후 gRPC 양팔 기존 시나리오 회귀 — 키 이름·카메라 접두사가 즉석 조립
      시절 산출물과 동일한지
