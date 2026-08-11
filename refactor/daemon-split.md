# 데몬 분리 — 백엔드를 게이트웨이로 (구조 개편)

> **이 문서는 01~09와 성격이 다르다.** 01~09는 "한 사실이 두 곳에 적혀 있는 곳"을 고치는
> 국소 수정이고, 이것은 **프로세스 경계를 다시 긋는 구조 개편**이다.
> 아래 결정 1~3은 방향이 정해졌고, 마지막 [미결정](#미결정) 절이 착수 전에 답해야 할 것들이다.

## 목표

백엔드를 **개별 프로세스들과 프론트엔드를 엮어주는 얇은 게이트웨이**로 축소한다.
데몬들은 독립 프로세스로 떼어내 메시지 버스로만 통신하고, 웹서버가 소유하는 subprocess는 0으로 만든다.

---

## 현황

### `services/`에 성격이 다른 셋이 섞여 있다

| 종류 | 파일 | 특징 |
|---|---|---|
| **상시 실행체** | robot_manager(922), camera_manager(637), realsense_manager(615), train_manager, record_manager, policy_server_manager, process_manager, estop_watchdog | 모듈 싱글턴 + 스레드/asyncio 루프 + subprocess 소유. 요청 밖에서 살아있고 `start()/stop()`이 있다 |
| **IPC 어댑터** | zmq_bridge(5555), preview_bridge(5556), control_bridge(5557) | 소켓만 들고 있고 상태 없음. 이름이 이미 일관됨 |
| **무상태 조회** | dataset_scanner, model_scanner, hub_client, encoder_probe | 호출하면 끝. 수명주기 없음 |

922줄짜리 하드웨어 소유자와 192줄짜리 HTTP 조회 헬퍼가 같은 폴더에 있다.
`manager`/`service`가 **아무 제약도 말하지 않는 이름**이라 이렇게 됐다.

### subprocess 현황

| 위치 | 무엇 | 이행 후 |
|---|---|---|
| [process_manager.py:87](../backend/app/services/process_manager.py#L87) `create_subprocess_exec` | LeRobot CLI / wrapper 스크립트 실행. **유일한 spawn 지점** — train·record·policy_server가 각자 `ProcessManager` 인스턴스를 갖고, 추론·데이터셋 편집은 전역 싱글턴을 쓴다 | **systemd 유닛으로 이관** → 웹서버에서 사라짐 |
| [robot_manager.py:43](../backend/app/services/robot_manager.py#L43), [:208](../backend/app/services/robot_manager.py#L208) | `ip link` 등 CAN 구성 | robot 데몬 안으로 따라 이동. 유지 |
| [camera_manager.py:20](../backend/app/services/camera_manager.py#L20) | v4l2 조회 | camera 데몬 안으로 이동. 유지 |
| [encoder_probe.py:134](../backend/app/services/encoder_probe.py#L134) | 인코더 프로브 | 유지 |

**subprocess 호출 수는 별로 안 준다. 다만 웹서버가 소유하는 subprocess가 0이 된다.**
목표를 이렇게 잡아야 정확하다. 실제로 없어지는 것은 이쪽이다:

- 백엔드가 pid를 들고 있기
- 상태를 백엔드 메모리에 두기 (→ 리로드하면 유실)
- **stdout을 정규식으로 긁어 상태를 알아내기** ([process_manager.py](../backend/app/services/process_manager.py)의 `_read_stdout`/`_read_stderr`)
- `train_manager.restore_running_process()` 같은 복원 코드 (→ systemd가 한다)

### 이미 어긋나 있는 것

[CLAUDE.md](../CLAUDE.md)와 [REF.md](../REF.md)는 *"웹 서버 ↔ LeRobot 프로세스 ↔ E-stop watchdog 각각 독립 프로세스"* 를
설계 원칙으로 못 박고 있으나, 실제 [estop_watchdog.py:43](../backend/app/services/estop_watchdog.py#L43)은
`asyncio.create_task` — **웹서버와 같은 이벤트 루프**다. 루프가 막히면 워치독도 같이 멈춘다.
(이벤트 루프 전체가 먹통이 된 전례가 있다: D405 UVC 컨트롤 질의가 커널 D-state로 hang.)

이 개편은 미관 문제가 아니라 **명문화된 안전 요구사항을 처음으로 실제로 만족시키는 작업**이다.

---

## 결정 1 — 명칭

| 이름 | 무엇 | 어디 |
|---|---|---|
| **daemon** | 독립 프로세스. 하드웨어/작업 수명주기를 소유하고 버스에만 말한다 | `daemons/robot/`, `daemons/camera/`, `daemons/train/` … |
| **bus** | 토픽 이름 + 메시지 스키마 = 데몬과 백엔드가 **공유하는 계약** | `piper_bus/` (양쪽이 import) |
| **scanner / client / probe** | 무상태 조회. 데몬이 아니므로 이동 대상 아님 | 지금 이름이 이미 정확하다 |

`daemon`을 고른 이유: **검증 가능한 규칙을 담는 유일한 이름**이다 —
*"요청이 없어도 살아있고, 자기 수명주기와 상태를 가진다."*
새 모듈을 어디 둘지 기계적으로 판정되고, 프로세스로 떼어낸 뒤에도 이름을 다시 안 바꾼다.

탈락:

- `service` — systemd 뉘앙스는 맞지만 지금 디렉토리명과 충돌하고, `manager`와 마찬가지로 무제약이라 다시 잡탕이 된다
- `worker` — 잡 큐 소비를 함의하는데 그런 구조가 아니다
- `supervisor` — 자식 감시만 해당된다
- `agent` — LLM 쪽으로 오염된 단어

백엔드 쪽에는 `client`/`proxy` 계층을 따로 두지 않는다. 게이트웨이가 충분히 얇아지면
라우터가 버스를 직접 읽고 쓰는 게 더 짧다.

---

## 결정 2 — 통신: Redis(데이터 평면) + systemd(제어 평면)

### 데이터 평면 = Redis

이 저장소 기준으로 D-Bus가 지는 지점이 구체적이다.

- **프레임.** `preview_zmq_address:5556`으로 JPEG를 흘린다. D-Bus는 바이너리 스트림용이 아니라
  preview만 다른 전송을 병행하게 되고, 통일하려던 게 둘로 늘어난다.
  Redis는 `SETEX cam:{id}:frame`이면 끝이고 지금의 단일-JPEG 폴링 UI와 모양이 같다.
- **컨테이너 경계.** `network_mode: host` + `privileged` 구성에서 데몬을 쪼개면
  D-Bus는 세션 버스 소켓 마운트가 골치다. Redis는 호스트:포트라 고민이 없다.
- **상태가 남는다 — 이게 제일 크다.** 지금은 서버를 리로드하면 `train_manager` 상태가 날아가는데
  subprocess는 계속 돈다. 상태가 Redis에 있으면 백엔드는 **재시작해도 다시 읽으면 그만인
  무상태 게이트웨이**가 되고, 그 버그 클래스가 구조적으로 사라진다.
- ZMQ 소켓 3개(5555/5556/5557)가 Redis 하나로 접힌다.

트래픽 4종이 전부 Redis 원시 타입에 대응된다:

| 트래픽 | 예 | Redis |
|---|---|---|
| 명령 (요청/응답) | 녹화 시작, 파라미터 변경, E-stop | Streams + 응답 키, 또는 pub/sub + ack |
| 상태 (최신값만) | 관절 상태, 프로세스 상태 | Key (+ pub/sub 알림) |
| 로그 (fan-out) | 학습/추론 로그 → WS | Streams (재생 가능, 소비자 그룹) |
| 프레임 (바이너리) | 카메라 preview JPEG | Key + TTL |

### 제어 평면 = systemd

D-Bus가 진짜로 이기는 유일한 영역(프로세스 시작/정지/재시작/복원)은 **systemd를 직접 쓰면 얻는다.**
`piper-train@<job>.service` 같은 템플릿 유닛으로 띄우면 데몬조차 `Popen`하지 않는다.

딸려오는 것:

- `systemctl status piper-*` / `journalctl -u piper-train` — 호스트 데몬이든 컨테이너 데몬이든 **한 방식으로** 본다
- `Restart=on-failure`, `After=`/`Requires=` 로 기동 순서 (redis → 데몬 → 게이트웨이)
- **로그아웃해도 안 죽는다.** 지금 `Linger=no`라 세션이 끊기면 tmux/uvicorn/학습이 통째로 kill되는데,
  시스템 유닛으로 가면 그 문제 자체가 없어진다
- E-stop watchdog이 진짜 독립 프로세스가 된다

---

## 결정 3 — 배포: systemd가 위, 컨테이너는 일부 유닛의 패키징 수단

**둘은 배타적 선택이 아니다.** systemd = 수명주기 관리, 컨테이너 = 의존성 패키징.

### 지금 컨테이너는 이미 컨테이너가 아니다

[docker-compose.yml](../docker-compose.yml)은 격리를 전부 꺼놨다 —
`privileged: true`, `network_mode: host`, `/dev:/dev` 통마운트, `/lib/modules`, `/run/udev`.
게다가 주석에 적힌 사전 요구사항이 전부 호스트 작업이다 (CAN 구성, v4l2loopback 적재,
RealSense udev 규칙, 데이터 루트 생성).

남은 이점은 **의존성 패키징 하나**다. 그건 진짜로 가치가 있다 —
CUDA 런타임 / torch / LeRobot / torchcodec(NVDEC)을 호스트에 재현하는 건 이미 겪은 고통이다.

핵심은 **데몬을 쪼개면 조각마다 다르게 다룰 수 있다**는 점이다.
지금은 한 덩어리라 제일 까다로운 요구(GPU + CAN + `/dev` + 커널 모듈)의 합집합을 하나가 다 짊어진다.

### 분할

| 데몬 | 배포 | 이유 |
|---|---|---|
| robot(CAN), camera(v4l2), realsense | **호스트 + systemd** | 컨테이너가 방해만 된다. USB 리바인딩(`/sys` 쓰기), udev, video 그룹 권한, 커널 모듈 — 지금까지 사고 난 지점이 전부 이 경계다. 의존성은 python-can/pyrealsense2 정도로 가볍다 |
| train, inference/policy_server | **컨테이너** (systemd 유닛이 기동) | 의존성 지옥이 여기 산다. 대신 `/dev/video`도 CAN도 필요 없고 GPU만 뚫으면 된다 |
| gateway, redis, estop | **호스트 + systemd** | 순수 파이썬 + 소켓. 컨테이너 쓸 이유가 없다 |

컨테이너 유닛은 Podman Quadlet(`.container` → systemd 유닛)이 제일 깔끔하다.
Docker로 남기려면 `ExecStart=docker run --rm` 유닛도 된다.

### 대가 (정직하게)

- 배포 경로가 두 갈래가 된다. `docker compose up -d` 한 줄 → 유닛 파일 여러 개 + 설치 스크립트
- `dev.sh`를 다시 짜야 한다 (하드웨어 데몬을 호스트에 깔아야 개발이 된다)
- → `install.sh` 하나로 감싼다 (유닛 배치 + udev 규칙 + venv 구성).
  어차피 지금도 compose 주석의 호스트 사전작업을 수동으로 하고 있다.

### 기기별 설정 분리 — 로봇마다 인스턴스라면 이게 핵심이다

로봇 N대에 같은 소프트웨어가 나간다면, **무엇이 이미지에 들어가고 무엇이 기기에 남는지**를
처음부터 갈라야 한다. 나중에 분리하려면 N대를 전부 손봐야 한다.

| 기기별 (이미지 밖) | 공통 (이미지 안) |
|---|---|
| CAN 인터페이스 이름 (`can_follower1` …) | 코드·의존성 전부 |
| 카메라 USB 포트 / RealSense 시리얼 | LeRobot·CUDA 스택 |
| **관절 캘리브레이션** (팔마다 0점이 다르다) | systemd 유닛 정의 |
| **카메라 프로파일** (조명·렌즈가 현장마다 다르다) | 기본 파라미터 |
| **URDF 오프셋·바닥면 높이** (설치 높이가 다르다) | URDF 자체 |
| 로봇 식별자 / 호스트명 | |

전부 이미 `settings.config_dir` 아래에 모이는 것들이다
([config.py:50](../backend/app/core/config.py#L50)). 여기를 **기기별 상태의 단일 경계**로 못 박고,
데이터 루트(`/srv/piper-data`)와 함께 백업·이관 단위로 다룬다.

이 결정은 아래 세 문서에 직접 걸린다:

- [robot-transport.md](robot-transport.md) — robotd가 캘리브레이션 단일 소유자
- [robotd-safety.md](robotd-safety.md) — URDF·바닥면 높이가 기기별. **한 대에서 맞춘 필터 설정을
  다른 대에 그대로 쓰면 안 된다**
- [camera-transport.md](camera-transport.md) — 컬러맵·클리핑 범위는 공통(데이터셋 계약),
  노출·WB는 기기별

> **부수 효과: 데이터가 N곳에 흩어진다.** 로봇마다 인스턴스면 데이터셋도 로봇마다 쌓인다.
> 학습하려면 어딘가로 모아야 하고, 그 "어딘가"가 사내 서버인지 클라우드인지가
> [cloud-training](../feature/cloud-training.md)의 데이터 반출 문제와 같은 질문이 된다.

---

## 새로 생기는 위험 — 버스 계약이 새 중복원이다

프로세스를 쪼개면 **토픽 이름과 메시지 필드가 정확히 이 폴더가 다루는 그 문제**가 된다.
[04-err-bits.md](04-err-bits.md)(`_ERR_BITS`가 프로세스 경계를 넘어 복붙된 건)가 딱 그 예고편이다.

→ `piper_bus/`를 **처음부터** 단일 계약 패키지로 못 박고 시작한다.
토픽 상수, 메시지 스키마, 상태 enum이 전부 여기 한 곳에만 있어야 한다.
데몬을 하나씩 떼기 전에 이 패키지가 먼저 있어야 한다.

---

## 이행 순서

각 단계가 끝난 시점에 시스템이 동작해야 한다. 빅뱅 금지.
어떤 프로세스를 몇 개로 쪼개는지는 **[daemon-inventory.md](daemon-inventory.md)** 에 별도로 정리했다.

1. ☑ **`piper_bus/` 계약 패키지 + Redis** — [bus/piper_bus/](../bus/piper_bus/). `pip install -e bus/` 로 게이트웨이·데몬이 같은 패키지를 import 한다.
2. ☑ **estopd 분리** — [daemons/estopd.py](../daemons/estopd.py) +
   [systemd 유닛](../deploy/systemd/piper-estopd.service).
   **버스로 "정지해줘"를 보내지 않는다** — 게이트웨이가 응답 못 하는 상황이 이 데몬의 존재 이유라,
   게이트웨이가 Redis 에 올려둔 활동 PID 를 읽어 **직접 SIGKILL** 한다.
   게이트웨이를 `SIGSTOP` 으로 얼려도 팔이 서는 것을 회귀 테스트로 고정했다.
3. **ZMQ 브리지 3개를 Redis로 교체** — 프로세스 경계는 그대로 두고 전송만 바꾼다.
   wrapper 쪽도 같이 바뀌므로 여기서 계약 패키지가 검증된다.
4. **장치 lease / quiesce 프로토콜 정의** — ⚠ 아래 단계들의 전제조건.
   카메라는 배타적 소유권 이전, CAN은 공유 버스 시분할로 **규칙이 서로 다르다.**
   지금은 이 규칙이 라우터 코드에 암묵적으로 박혀 있다
   ([daemon-inventory.md](daemon-inventory.md#핵심-난제--장치-소유권-중재) 참고).
   명시적 프로토콜 없이 쪼개면 카메라 먹통 사고가 재현된다.
5. **하드웨어 데몬 분리** (robot → camera → realsense) — 하나씩. 제일 큰 덩어리(922줄)가 먼저인
   이유는 `restore_session()`/모션 감지 스레드가 데몬 모델에 가장 잘 맞기 때문이다.
6. **`ProcessManager` → systemd 템플릿 유닛** — train/policy_server/encoder/transfer 먼저,
   record/inference는 마지막(장치를 셋 다 요구해 배포 위치 결정이 필요하다).
   stdout 파싱이 여기서 없어진다.
7. **게이트웨이 정리** — `services/`에 무상태 조회만 남고, `main.py`의 lifespan이 비워진다.

---

## 미결정

착수 전에 답해야 한다.

1. ~~**배포 대수**~~ → **결정: 로봇마다 별도 인스턴스.** 아래 "기기별 설정 분리" 절 참고.
2. **E-stop이 무엇을 죽이는가.** 지금 [estop_watchdog.py:58](../backend/app/services/estop_watchdog.py#L58)은
   **전역 `process_manager`만** kill한다. 그런데 train/record/policy_server는 각자 별도
   `ProcessManager` 인스턴스를 갖는다 — 즉 **E-stop은 추론(과 데이터셋 편집)만 죽이고 녹화는 안 죽인다.**
   녹화도 텔레오퍼레이션으로 로봇을 움직인다. 의도된 것인지 확인하고, 데몬 모델에서
   "E-stop이 어떤 데몬을 정지시키는가"를 명시적으로 정의해야 한다.
3. **wrapper/의 위치.** `wrapper/*.py`는 이미 별도 인터프리터(`settings.local_python`,
   `settings.grpc_python`)에서 도는 준-데몬이다. `daemons/` 아래로 흡수할지, LeRobot 접점으로 남길지.
4. **`process_manager` 전역 싱글턴의 겸업.** 추론([models.py:320](../backend/app/routers/models.py#L320))과
   데이터셋 편집([datasets.py:76](../backend/app/routers/datasets.py#L76))이 같은 인스턴스를 쓴다.
   또 [ws.py:98-99](../backend/app/routers/ws.py#L98-L99)가 이 전역에만 로그 콜백을 걸어서
   프로세스마다 로그 경로가 제각각이다. 데몬으로 쪼개면 자연히 분리되지만, 분리 후
   프론트엔드가 보는 로그 스트림 계약이 어떻게 되는지 정해야 한다.
5. **01~09와의 순서.** #1·#2(추론 파라미터·정책 레지스트리)는 프론트↔백 계약을 새로 만드는 일이라
   이 개편과 범위가 겹친다. 버스 계약을 먼저 만들고 그 위에서 정리하는 게 나을 수 있다.

---

## 검증

단계마다:

- 프론트엔드 변경은 `cd frontend && npm run build` (`npx tsc --noEmit`은 루트 tsconfig가 참조 전용이라 no-op)
- 데몬 분리 후 **실제 하드웨어로** 한 번씩: 로봇 연결 → 카메라 미리보기 → 녹화 1 에피소드 → 추론 1회
- **E-stop을 매 단계 실제로 눌러본다.** 브라우저 탭을 강제로 닫아 heartbeat를 끊는 경로도 함께
  (추론 중에는 `window.confirm` 류의 블로킹 UI가 heartbeat를 막아 2초 타임아웃을 유발한 전례가 있다)
- `systemctl stop` / 프로세스 강제 kill 후 게이트웨이가 상태를 Redis에서 올바르게 복원하는지
- 백엔드만 재시작했을 때 학습·녹화가 계속 돌고 UI가 그 상태를 그대로 보여주는지 (현행 버그의 회귀 테스트)

## 상태

☐ 미착수 — 미결정 1~5 해소 후 착수
