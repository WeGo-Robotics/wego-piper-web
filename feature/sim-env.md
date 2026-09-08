# 시뮬레이션 환경 — 하드웨어 없이 등록→수집→학습→추론을 왕복한다 (기획)

상태: **기획.** 구현 전.

실측 (2026-09-09): MuJoCo 미설치(`pip install mujoco` 필요). AgileX 공식 URDF
`~/agx_arm_urdf/piper/` (MIT, 메시 22개, joint1~6 revolute — **그리퍼 관절
없음**). NVIDIA EGL 있음(RTX 5090) → `MUJOCO_GL=egl` GPU 헤드리스 렌더 가능.
저장소에 시뮬 언급 0.

## 0. 요지 — 시뮬레이터는 "또 하나의 로봇 데몬"이다

이 저장소의 모든 소비자(게이트웨이 조그·릴레이·감시, LeRobot 수집·추론
wrapper)는 장치를 모른다. **/dev/shm 세그먼트만 본다**:

- 팔: `piper.arm.<id>.state / .action` — 위치 인덱스 7-float, deadman 내장
- 카메라: `piper.cam.<id>` — BGR 프레임 (`piper_shm.frames.Publisher`)

그러니 시뮬레이션은 새 앱이 아니라 **같은 세그먼트를 발행하는 데몬 하나**
(`simd`)다. robotd·camerad·rsd·so101d 와 같은 계약
([docs/robot-daemon-contract.md](../docs/robot-daemon-contract.md))을 지키면
수집(`piper_follower_shm`)·학습·추론(wrapper)·E-stop(deadman)·조명 감시가
**한 줄도 안 바뀐 채** 시뮬 위에서 돈다. `piper_follower_shm` 의 `port` 는
CAN 이름이 아니라 세그먼트 이름이라 `sim_follower1` 이 그대로 통한다.

얻는 것: 하드웨어 없는 개발 머신에서의 회귀 테스트, 정책의 **자동 성공
판정**(물체가 통 안에 있는가를 시뮬이 안다 — [ACT-delta](act-delta.md) A/B 와
[SO-101 §5c](so101d.md) 비교의 심판), 조명·배치 무작위화, 위험 없는 안전
필터 검증.

## 1. 경계 — `simd` 하나가 세계(팔+카메라)를 든다

| 자리 | 판정 |
|---|---|
| **`daemons/simd.py` + `sim/piper_sim/`** | ✅ MuJoCo 세계 하나 = 프로세스 하나. 팔 세그먼트와 카메라 세그먼트를 **같은 물리·렌더 루프**에서 발행한다 |
| robotd/camerad 에 "sim 모드" | ❌ 실장치 데몬에 분기가 생기면 실기에서 시뮬 코드가 돈다. 격리 원칙(장치=데몬) 그대로: 시뮬은 시뮬 데몬 |
| 게이트웨이 안에서 시뮬 | ❌ 컨테이너·재시작·GIL 전부 문제. 데몬 모델을 깨지 않는다 |
| 팔·카메라를 데몬 둘로 | ❌ 한 세계의 두 절반을 두 프로세스에 두면 시각 동기가 깨진다 ("장치 하나 = 데몬 하나"의 장치가 여기선 **세계**다) |

MuJoCo 를 고른 이유: 순수 pip 설치, 헤드리스 렌더(EGL) 내장, 관절 위치
액추에이터가 Piper 의 위치 제어와 같은 추상화, Apache-2.0. 대안(Isaac
Sim/Gazebo)은 설치·GPU·라이선스가 무겁고 우리가 쓰는 건 접촉 있는 팔 하나다.

## 2. 계약 재사용 — 무엇이 안 바뀌는가

| 층 | 시뮬에서 |
|---|---|
| 팔 상태/명령 세그먼트 | 그대로. `sim_follower1` 이 발행, 소비자가 명령을 쓰면 simd 가 액추에이터 목표로 |
| 정규화 | **`piper_robot.joints.normalize_all/denormalize_all` 그대로** — 시뮬 관절각(rad)→밀리도→정규화. 캘리브레이션 표가 하나여야 실기 정책이 시뮬에서 같은 숫자를 본다 |
| 안전 필터 | **`piper_robot.safety.filter_goal` 그대로** simd 가 명령에 적용 — 순수 함수라 import 만 하면 된다. 바닥·범위·변화율·데드맨이 시뮬에서도 산다(그래서 필터 자체를 위험 없이 검증할 수 있다) |
| deadman·E-stop | 세그먼트 헤더 계약 그대로. estopd 의 PID SIGKILL 도 그대로 |
| 카메라 세그먼트 | `Publisher(name, w, h, 3)` 로 BGR 발행. 조명 감시·프로파일 적용·뷰어 전부 그대로 |
| LeRobot | `piper_follower_shm`(로봇) / `piper_leader_shm`(리더) 무수정 — 둘 다 세그먼트 이름만 받는다 |
| 기구학 | `ArmModel.load("piper")` — 시뮬 FK 와 **대조 검증**(§6)하고, 스크립트 시연(§5)의 IK 로 쓴다 |

## 3. 로봇 등록 — `SimArmInfo` 가 등록부에 들어간다

**실측: 게이트웨이 25곳(7파일)이 `robot_manager.arms[iface]` 가 `ArmInfo`
표면을 갖는다고 가정한다** — 조그·릴레이·정렬·수집·추론 시작·prepare_arms.
SO-101 은 리더라 이걸 피해 갔지만, 시뮬 팔은 **팔로워**라 정면으로 부딪친다.

두 길:

| 길 | 판정 |
|---|---|
| **`SimArmInfo(ArmInfo)`** — 같은 표면(connect/disconnect/read_joints_*/enable_torque/go_parking/set_master_slave…)을 simd RPC 로 구현해 `robot_manager.arms` 에 넣는다 | ✅ **25곳 무수정.** `transport="sim"`, capabilities 로 마스터/슬레이브·0x150·영점굽기는 없음(no-op 아닌 "없음" 선언). 세션에 남고, 스캔은 simd 가 답한다 |
| 허브 레지스트리 리팩토링([so101d §4.3](so101d.md)) 을 먼저 | ❌ 지금 필요한 건 시뮬이지 레지스트리가 아니다. 리팩토링은 팔로워 기종이 둘 이상 생긴 뒤 (시뮬 팔이 첫 사례가 되어 그때 형태를 알려준다) |

포트 패널: `sim` 카드 종류(세계 이름·씬·상태) — CAN/시리얼 카드와 같은 틀
([86e3788](../) 의 "포트는 포트, 팔은 로봇"). [연결] → 로봇 패널에 Piper
카드 틀로 선다. 리더가 필요하면 `sim_leader1`(§5)을 같은 식으로.

## 4. 카메라 등록 — `cam_type="sim"` 허브 하나

`camera_manager._hub()` 가 `cam_type` 으로 camerad/rsd 를 가른다 — 세 번째
가지 `sim` 에 `SimCameraHub`(버스 RPC 클라이언트, realsense_manager 골격).
simd 의 카메라 RPC 는 기존 시그니처 그대로: `scan / connect(cam_id, w, h,
fps, controls) / disconnect / list_controls / set_control / lost`.

- 카메라 = MJCF 의 `<camera>` (탑·손목 등). `connect` 하면 그 카메라를
  요청 해상도·fps 로 오프스크린 렌더해 발행 시작.
- **컨트롤 에뮬레이션**: exposure/gain → 밝기 배율, WB → 채널 게인. 프로파일
  적용·회색카드·조명 감시(급변·표류 알람)를 시뮬에서 **그대로 시험**할 수
  있다 — 조명을 시간에 따라 서서히 올리면 표류 알람이 우는지가 곧 테스트다.
- 깊이(D405 흉내)는 v2 — MuJoCo 가 depth 도 렌더하므로 어렵진 않다.

## 5. 수집·학습·추론 — 리더는 셋 중 고른다

수집은 LeRobot record CLI 가 돌리고 액션 소스는 `piper_leader_shm` 이 읽는
**리더 세그먼트**다. 시뮬에서 그 세그먼트를 채우는 방법:

| 리더 | 무엇 | 쓰임 |
|---|---|---|
| **스크립트 시연** (`sim_leader1`) | simd 안의 시연 드라이버: `ArmModel` IK 로 접근→파지→운반→놓기 웨이포인트를 풀고, 물체 위치·높이를 무작위화해 리더 세그먼트로 발행 | **하드웨어 0 으로 데이터셋 생산**, 회귀 테스트, 야간 대량 생성 |
| 실물 SO-101 리더 | 기존 릴레이(관절 매칭) 그대로 — 팔로워만 시뮬 | 조작감 그대로의 수집, 릴레이 자체 검증 |
| 웹 조그/말단 조그 | 기존 그대로 | 수동 확인 |

학습은 무수정(데이터셋은 그냥 LeRobot 데이터셋). 추론도 무수정 — wrapper 가
`sim_follower1` 상태를 읽고 명령을 쓰면 simd 가 움직인다.

**자동 성공 판정**: simd 가 물체 위치를 아니 "통 안/목표 높이/파지 유지"를
판정해 `/api/eval/log` 에 남긴다. 사람이 보고 누르던 성공률이 시뮬에선
숫자로 나온다 — ACT-delta·SO-101 모드 비교의 심판이 여기다.

## 6. 정합성 — 시뮬 Piper 가 실기 Piper 와 같은 숫자를 말하게

- **관절 규약 대조 테스트**: 같은 정규화 벡터를 넣었을 때 simd 의 FK(MuJoCo
  site) 와 `ArmModel.load("piper").fk()` 의 말단 자세가 일치해야 한다 — 부호·
  오프셋 실수를 시뮬 켜기 전에 잡는다 (SO-101 릴레이 방향 실측처럼 뒤늦게
  알지 않는다).
- **그리퍼**: 공식 URDF 에 없다 → MJCF 에서 평행 그리퍼 관절 둘 + 0..100
  정규화(`gripper` 캘리브레이션 0..68000 = 68mm 스트로크와 같은 뜻) 추가.
  실기 데이터셋의 `gripper` 열과 같은 척도여야 시뮬-실기 데이터를 섞을 수 있다.
- **시간**: 실시간 페이싱이 기본(LeRobot 루프가 벽시계다). 물리 500Hz,
  상태 발행 100Hz(robotd 와 동일), 카메라 15~30fps. 스크립트 시연은
  "실시간보다 빠르게" 옵션 — 단 **녹화 중엔 금지**(fps 가 거짓이 된다).

## 7. 순서

1. ✅ **`sim/piper_sim/` + `daemons/simd.py`** (2026-09-09) — `tools/build_sim_scene.py`
   가 공식 URDF(서브모듈)→MJCF 를 굽고 그리퍼(손가락 슬라이드 2×34mm = 68000µm)·
   테이블·큐브·통·카메라(top/front/wrist)·위치 서보를 심는다. 물리 500Hz 실시간,
   상태 **99Hz** 발행, action 소비 + `filter_goal` + deadman, 계약 RPC, `piper-simd` 유닛.
   실측: **FK 대조 0.00mm/0.00°**(같은 AgileX URDF), shm 경로 추종 오차 ≤0.08 norm,
   데드맨 뒤 드리프트 **0.0**, 바닥 필터가 테이블 아래로 가는 목표를 실제로 막음
   (`last_reason: floor`). 잡은 결함 둘: ① `base_link`(정적→world 병합)↔`link1` 메시
   6mm 겹침이 접촉 마찰로 joint1 을 얼렸다 → 그 쌍만 접촉 제외; ② hold 가 매 스텝
   ctrl=qpos 로 재래치해 정지가 미끄러졌다(처짐 7.5, 데드맨 뒤 1.9) → 한 번만 래치.
   서보는 gravcomp+감쇠+kp 상향. 메모: joint5 URDF ±70° vs 캘리브레이션 표 ±65°(표가
   보수적, 테스트는 포함 관계만 본다). 웹 조그 검증은 3단계(`SimArmInfo`) 뒤 —
   지금은 조그가 쓰는 것과 같은 shm 경로(ActionWriter)로 검증했다.
2. ✅ **카메라** (2026-09-09) — `sim/piper_sim/cameras.py`(데몬 쪽, camerad 어휘,
   `cam_` 접두사 RPC) + `backend/app/services/sim_camera_client.py`(게이트웨이,
   `_hub()` 세 번째 가지 — realsense 리터럴 1회 규칙 유지) + 스캔 합류.
   컨트롤은 **v4l2 이름**(auto_exposure·exposure_time_absolute·gain·
   white_balance_*·brightness)의 에뮬레이션(노출×게인=밝기, 색온도=채널 게인)이라
   `piper_cam.controls` 의 자동 모드 순서·단위와 **같은 적용기**가 그대로 걸린다.
   세그먼트는 BGR. 실측: 웹 API 스캔→연결→**14.5fps**(요청 15) 세그먼트→프리뷰
   JPEG→조명 감시 luma, `set_light 0.3` 으로 luma **244→123**, 기존 device_watch
   경로로 **급변 알람 실제 발화**("갑자기 어두워졌습니다 −103/255"). 잡은 결함
   둘: ① EGL 컨텍스트는 스레드 귀속 — probe 가 RPC 스레드에서 렌더러를 만들자
   렌더 스레드가 5분에 EGLError 139회 → 렌더는 렌더 스레드 하나가 요청 큐로
   전부(probe 포함); ② 씬 밝기의 절반이 카메라 헤드라이트라 월드 라이트만
   스케일하면 조명이 안 변한다(244→225, 포화) → 헤드라이트도 함께. 표류 알람은
   작업(수집·추론) 중에만 앵커가 걸리므로 4단계 E2E 에서 본다.
3. **등록 UI** — `SimArmInfo`, 포트 패널 sim 카드, 로봇 카드(capabilities).
   **검증: 등록→세션 저장→게이트웨이 재시작 후 복원.**
4. **스크립트 시연 → 수집 → 학습 → 추론 → 자동 판정** — E2E 한 바퀴.
   **검증: 시연 20 에피소드 → ACT 학습 → 시뮬 추론 성공률이 `/eval/stats`
   에 찍힌다.** 이게 이 기획의 인수 기준이다.
5. v2 — 실물 SO-101 리더 + 시뮬 팔로워, 깊이 카메라, 도메인 무작위화(조명·
   텍스처·물체), 헤드리스 CI 스모크(60초 E2E), MuJoCo 뷰어 창.

## 8. 위험

| 위험 | 대응 |
|---|---|
| URDF→MJCF 변환에서 메시·관성·한계가 어긋남 | MuJoCo 의 URDF 로더로 먼저 열고, §6 FK 대조 테스트를 1단계 완료 조건으로 |
| 그리퍼 모델이 실기와 다른 척도 | 0..100 정규화를 실기 캘리브레이션 뜻(스트로크)에 맞춘다; 실기 데이터셋 한 에피소드를 시뮬에 재생해 파지 폭이 맞는지 본다 |
| 유닛 환경에서 EGL 초기화 실패 | 유닛에 `MUJOCO_GL=egl` + 기동 시 1프레임 렌더 자가진단, 실패하면 `osmesa`(CPU) 폴백 로그 |
| 실시간 페이싱이 학습·추론 GPU 부하 아래서 밀림 | 상태 발행 wall_ns 로 wrapper 가 나이를 본다(이미 그렇다); 밀리면 로그. 렌더 해상도 기본 640×480 |
| 시뮬-실기 관측 격차로 "시뮬에서 되는 정책" 과신 | 이 기획의 목표는 **파이프라인 검증과 상대 비교**다. 실기 성능 예측이 아니라고 문서에 못 박는다 |
| `ArmInfo` 표면이 넓어 `SimArmInfo` 가 반쯤 구현됨 | capabilities 로 없는 기능은 "없음" 선언 → UI 가 안 그림. 호출되면 명확한 오류(조용한 no-op 금지) |

## 열린 질문

- MJCF 정본을 어디에 둘 것인가 — `sim/piper_sim/assets/`(URDF 변환 결과 +
  그리퍼·씬 수작업). URDF 원본(MIT)은 vendor 스냅샷.
- 스크립트 시연의 "사람 같음" — 순수 IK 웨이포인트는 너무 깨끗하다. 속도
  프로파일·소음 주입 정도를 v1 에 넣을지, 실물 리더 수집과 섞을지.
- 시뮬 팔의 id 규약: `sim_follower1`/`sim_leader1` (so101_ 접두사 규칙과
  대칭). 실기 세션과 같은 세션 파일을 쓰되 transport 로 가른다.
