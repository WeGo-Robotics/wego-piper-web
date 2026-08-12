# 카메라 전송 — camerad가 잡고 소비자에게 넘기기

[daemon-inventory.md](daemon-inventory.md)의 부속 문서. **결정 완료.**

## 결정

`/dev/shm` 링버퍼 + **LeRobot 서드파티 카메라 플러그인**.
camerad가 장치를 독점하고, 추론/녹화 프로세스는 공유 메모리에서 raw 프레임을 읽는다.
**LeRobot 수정 0, wrapper 수정 0.**

---

## 왜 이것인가 — 측정 결과

### 후보 3안

| 방식 | 인코딩 | LeRobot 수정 | 비고 |
|---|---|---|---|
| LeRobot 내장 ZMQ 카메라 | JPEG q80 + base64 | 0 | `lerobot/cameras/zmq/`에 이미 있음 |
| v4l2loopback | 없음(raw) | 0 | [REF.md:50-80](../REF.md)이 원래 설계. 커널 모듈 필요 |
| **shm + 커스텀 카메라** | **없음(raw)** | **0** | ← 채택 |

### 내장 ZMQ의 비용 (이 호스트 실측, 왕복 = encode→base64→json→parse→unbase64→decode)

| 해상도 | wire | enc | b64 | json | parse | unb64 | dec | 왕복 | 30fps×3대 |
|---|---|---|---|---|---|---|---|---|---|
| 640×480 | 107K | 0.87 | 0.05 | 0.12 | 0.05 | 0.05 | 0.83 | **1.96ms** | 코어 18% |
| 1280×720 | 320K | 1.58 | 0.13 | 0.28 | 0.11 | 0.11 | 2.18 | **4.38ms** | 코어 39% |
| 1920×1080 | 719K | 3.60 | 0.29 | 0.62 | 0.24 | 0.26 | 4.75 | **9.75ms** | 코어 88% |

**용량은 문제가 아니었다.** base64가 +33%지만 JPEG가 그 앞에서 raw를 1/11로 줄여서
wire는 오히려 raw보다 훨씬 작다 (1080p 30fps 1대 = 21 MB/s, 루프백엔 여유).

**CPU가 문제다. 그리고 base64는 그중 14%뿐이다** — 1080p 왕복 9.75ms 중
JPEG encode+decode가 8.35ms(86%). base64를 없애도 88%→76%밖에 안 준다.
**없애야 할 것은 base64가 아니라 JPEG 코덱 자체다.**

### shm 대비 (동일 호스트)

| 해상도 | 프레임 | 3슬롯 | shm 복사 | JPEG 왕복 | 배수 | 30fps×3대 |
|---|---|---|---|---|---|---|
| 640×480 | 0.88MB | 2.6MB | **0.020ms** | 1.96ms | 99× | 코어 0.2% |
| 1280×720 | 2.64MB | 7.9MB | **0.114ms** | 4.38ms | 38× | 코어 1.0% |
| 1920×1080 | 5.93MB | 17.8MB | **0.249ms** | 9.75ms | 39× | 코어 2.2% |

해상도를 올려도 코어 점유가 2%대에 머문다. 이게 채택 이유다.

용량 걱정은 근거가 없다 — 이 호스트 `/dev/shm`은 31GB이고 1080p 카메라 3대가 53MB다.

### 내장 ZMQ의 또 다른 함정

`ZMQCamera._read_loop`가 레이트 리밋 없는 `while` 루프로 **발행되는 모든 프레임을
recv+decode** 한다. `async_read()`는 그 결과를 꺼낼 뿐이다.
즉 정책이 2Hz로 읽어도 camerad가 30fps로 쏘면 30fps로 디코드한다 —
"추론은 카메라 접근 주기가 기니까 괜찮다"가 성립하지 않는다.
shm은 리더가 읽을 때만 복사하므로 이 문제가 없다.

---

## LeRobot 수정이 0인 이유

체인이 이미 다 깔려 있다.

1. **플러그인 자동 등록** — [`register_third_party_plugins()`](../wrapper/lerobot_wrapper.py#L225)는
   `lerobot_robot_` / `lerobot_camera_` / `lerobot_teleoperator_` / `lerobot_policy_` 로 시작하는
   **설치된 배포판을 전부 import** 한다 (editable 설치 포함). wrapper가 이미 이걸 호출한다.
2. **설정 등록** — `CameraConfig`가 `draccus.ChoiceRegistry`라
   `@CameraConfig.register_subclass("shm")` 한 줄로 `type: "shm"`이 생긴다.
3. **인스턴스 생성** — `make_cameras_from_configs`의 마지막 `else` 분기가
   `make_device_from_device_class(cfg)`로 빠진다. 설정 클래스명에서 `Config`를 떼어
   같은 패키지에서 구현 클래스를 찾는 **공식 확장점**이다.

이미 `vendor/lerobot_robot_piper/`가 같은 메커니즘을 쓰고 있다 — 선례가 있다.

### 패키지

```
vendor/lerobot_camera_pipershm/
  pyproject.toml                    # name = "lerobot_camera_pipershm"
  lerobot_camera_pipershm/
    __init__.py                     # PiperShmCamera 를 export (탐색 후보 경로)
    config_pipershmcamera.py        # @CameraConfig.register_subclass("shm")
    pipershmcamera.py               # class PiperShmCamera(Camera)
```

클래스명 규칙이 강제된다: `PiperShmCameraConfig` → `PiperShmCamera` (`Config` 제거).

구현할 것은 `Camera` 추상 메서드 6개뿐이다 —
`is_connected`(property), `find_cameras`(static), `connect`, `read`, `async_read`, `disconnect`.
`read_latest`는 기본 구현이 있다.

### 바뀌는 곳은 여기 하나

[`_build_cameras_json`](../backend/app/routers/models.py#L118):

```python
{"top": {"type": "opencv", "index_or_path": "/dev/video0"}}
{"top": {"type": "intelrealsense", "serial_number_or_name": "...", "use_depth": True}}
                          ↓
{"top": {"type": "shm", "segment": "piper.cam.top"}}
```

**RealSense 특수사정이 여기서 사라진다.** D405가 color-only면 0fps라 depth를 함께 켜야 하는
문제([models.py:136-141](../backend/app/routers/models.py#L136-L141)의 `is_d405` 분기)가
camerad 안에 갇히고, 소비자는 color raw만 받는다.

---

## 세그먼트 설계

카메라 하나당 세그먼트 하나 (`/dev/shm/piper.cam.<name>`). 수명이 독립적이라 관리가 쉽다.

```
┌─────────────────────────────────────────────┐
│ 헤더 (64B, 캐시라인 정렬)                    │
│  magic u32 │ version u32                    │
│  width u32 │ height u32 │ channels u32      │
│  dtype u32 │ n_slots u32 │ slot_bytes u64   │
│  write_seq u64   ← 발행 카운터               │
│  wall_ns u64     ← 최신 프레임 캡처 시각     │
├─────────────────────────────────────────────┤
│ slot[0]  raw BGR                            │
│ slot[1]                                     │
│ slot[2]                                     │
└─────────────────────────────────────────────┘
```

### seqlock — 찢어진 프레임 방지

```
writer:  slot[(seq+1) % N] 에 프레임 기록  →  write_seq = seq+1
reader:  s1 = write_seq
         slot[s1 % N] 를 자기 버퍼로 복사
         s2 = write_seq
         if s2 - s1 >= N-1:  재시도      # 라이터가 한 바퀴 돌아 덮었을 수 있음
```

N=3이면 30fps 라이터(33ms 간격)에 1080p 복사가 0.249ms라 경합이 사실상 안 난다.
재시도 경로는 있어야 하지만 거의 안 탄다.

> ⚠ x86-64는 TSO라 "프레임 기록 → seq 증가" 순서가 재배열되지 않는다.
> **ARM(Jetson 등)에 올릴 계획이 있으면 명시적 배리어가 필요하다.** 이식 시 확인할 것.

### 알림

`write_seq` 폴링 + 1ms sleep. 30fps에 최대 1ms 추가 지연, CPU는 사실상 0이다.
eventfd/세마포어는 이 지연이 문제가 될 때 생각한다.

### 버스와의 역할 분담

| | 담당 |
|---|---|
| Redis | 제어·메타데이터 ("cam.top이 1280×720으로 준비됨", lease, 상태) |
| shm | 픽셀만 |

**세그먼트의 존재 자체가 lease다** — camerad가 그 카메라를 잡고 있다는 뜻이다.
[daemon-split.md](daemon-split.md) 4단계의 장치 소유권 프로토콜이 여기서 자연스럽게 표현된다.

---

## 정직하게, 제약 두 가지

### 1. zero-copy가 아니다 — memcpy 1회

`Camera.read()`는 NDArray를 반환하고 호출자가 그걸 얼마나 오래 들고 있을지 알 수 없다.
mmap view를 그대로 주면 라이터가 그 밑에서 덮어쓴다. **복사는 반드시 해야 한다.**

다만 1080p 6MB memcpy가 0.249ms고 JPEG 왕복이 9.75ms다. 39배면 충분하다.

### 2. 컨테이너가 `/dev/shm`을 공유해야 한다

- compose: `ipc: host` (호스트 IPC 네임스페이스 공유) 또는 `- /dev/shm:/dev/shm` 마운트
- **Docker 기본 `/dev/shm`은 64MB**다. 마운트 방식이면 `shm_size`를 올려야 한다.
  1080p 3대 = 53MB로 아슬아슬하므로 **`ipc: host`를 권장**한다 (호스트 크기 31GB를 그대로 씀).

---

## 깊이맵을 정책 입력으로 넣기

### 벽 — LeRobot 데이터셋 계층은 미터법 depth를 저장할 수 없다

| 지점 | 사실 |
|---|---|
| `RealSenseCamera.read_depth()` | `(h, w) uint16` (mm) 반환. **존재하지만 아무도 안 부른다** |
| `Robot.get_observation()` | `cam.async_read()`만 호출 → **depth는 막다른 길** |
| [`_cameras_ft`](../vendor/lerobot_robot_piper/lerobot_robot_piper/piper_follower.py#L69) | `(height, width, 3)` 하드코딩 → 3채널 전제 |
| `image_writer.py:55-66` | **`dtype != uint8`이면 거부**하거나 float를 ×255. uint16 경로 없음 |
| `dtype: "video"` | mp4 = 8비트 |

즉 이미지·비디오 두 경로가 전부 uint8로 수렴한다.
**uint16 mm 값을 그대로 넣을 자리가 없다.**

### 답 — 3채널 uint8로 바꿔서 "또 하나의 카메라"로 넣는다

shm 설계에서는 세그먼트를 하나 더 발행하면 끝이다:

```
piper.cam.wrist          ← color        (h, w, 3) uint8
piper.cam.wrist_depth    ← 컬러라이즈드  (h, w, 3) uint8
```

`_build_cameras_json`에 항목 하나 추가:

```python
{"wrist":       {"type": "shm", "segment": "piper.cam.wrist"},
 "wrist_depth": {"type": "shm", "segment": "piper.cam.wrist_depth"}}
```

**LeRobot도 `PiperFollower`도 데이터셋 포맷도 손대지 않는다.**
`observation_features`가 `self.cameras`에서 파생되고 `hw_to_dataset_features`가 거기서 파생되므로
녹화·학습·추론이 전부 자동으로 따라온다. 정책은 `observation.images.wrist_depth`를
그냥 카메라 하나로 본다.

**D405에서는 공짜다.** color가 안 나와서 이미 depth를 강제로 켜고 있는데
([models.py:136-141](../backend/app/routers/models.py#L136-L141)) 지금은 그 스트림을 버리고 있다.

### 인코딩 — 컬러맵 vs 그레이스케일

| 방식 | 장점 | 단점 |
|---|---|---|
| **컬러맵**(turbo/jet) | 사전학습 RGB 인코더가 잘 받는다 | 비선형. mp4 압축이 색 경계를 뭉갠다 |
| **그레이 정규화 → 3ch 복제** | 선형. mp4 휘도 채널이라 압축에 강함 | 256 레벨. 무채색 입력에 인코더가 덜 민감할 수 있음 |
| ~~hi/lo 바이트 분해~~ | 원리상 mm 복원 가능 | **mp4가 lo 바이트를 파괴한다.** PNG(`use_video=False`)로만 가능하고 용량 폭증, 인코더엔 노이즈 |

모방학습에서 정책이 쓰는 건 미터법 정밀도가 아니라 **공간 구조**다.
바이트 분해는 필요 없다. 둘 중 하나로 시작하고 비교한다.

### ⚠ camerad가 파라미터의 단일 소유자여야 한다

컬러맵 종류, **클리핑 범위**, **무효 픽셀 처리**가 전부 데이터셋 계약의 일부다.
녹화 때와 추론 때가 조금이라도 다르면 정책이 조용히 틀린 입력을 받는다.

- **클리핑 범위**: D405는 근거리 카메라다. 범위를 태스크에 맞게 고정해야 한다.
  나중에 범위를 바꾸면 **기존 데이터셋 전체가 무효**가 된다
- **무효 픽셀**: RealSense는 측정 실패 시 0을 낸다. 그대로 컬러맵에 넣으면
  "가장 가까움"으로 보인다. 별도 값/색으로 표시해야 한다
- 이 파라미터를 세그먼트 헤더와 데이터셋 메타에 **기록**해 둔다

[robotd-safety.md](robotd-safety.md)의 필터 설정이 데이터셋 계약이 되는 것과 정확히 같은 문제다.

### 비용

비전 인코더 입력이 하나 늘어난다. 카메라 3대 → 4대면 인코더 연산도 그만큼 는다.
추론 지연과 GPU 메모리에 영향이 있으므로 **depth를 넣기 전과 후의 fps를 실측**해야 한다.

---

## 배포에 미치는 영향 — 미결정 #2가 풀린다

카메라가 shm으로 들어오면 추론/녹화 컨테이너에서 하드웨어 접근이 빠진다.

| | 지금 | shm 이후 |
|---|---|---|
| `privileged: true` | 필요 | **불필요** |
| `/dev:/dev` 통마운트 | 필요 | **불필요** |
| `/run/udev`, `/lib/modules` | 필요 | **불필요** |
| `ipc: host` | — | **필요** (신규) |
| `network_mode: host` | 필요 | **여전히 필요** (CAN) |
| GPU | 필요 | 필요 |

**CAN은 못 뺀다.** LeRobot에 카메라의 플러그인 같은 범용 원격 로봇 전송이 없다
(`lerobot/robots/` 전체를 확인했다. lekiwi가 ZMQ를 쓰지만 특정 로봇 타입이지 전송 계층이 아니다).
다만 SocketCAN은 `/dev` 노드가 아니라 **네트워크 인터페이스**라서 `network_mode: host`만으로
보인다 — raw 소켓 권한(`NET_RAW` 정도)이면 되고 통마운트는 필요 없다.

→ [daemon-inventory.md](daemon-inventory.md)의 미결정 #2가 **"컨테이너, 단 host network + ipc host"** 로 결정된다.

---

## 착수 순서

1. ☑ `vendor/lerobot_camera_pipershm/` 골격 + 세그먼트 포맷 확정
   ([shm/piper_shm/](../shm/piper_shm/) — 게이트웨이·camerad·플러그인이 공유하는 계약)
2. ☑ **소비자 먼저** — 기존 `camera_manager`/`realsense_manager`가 임시로 세그먼트를
   채우고, `PiperShmCamera`로 읽는다. **LeRobot 수정 0으로 `type: "shm"`이 등록되는 것과
   실카메라 프레임이 흐르는 것까지 확인**(D435 640×480 15fps, seqlock 재시도 0).
   추론 1회 실기는 남아 있다
3. ☑ **rsd**([daemons/rsd.py](../daemons/rsd.py) · [rs/piper_rs/](../rs/piper_rs/)) 와
   **camerad**([daemons/camerad.py](../daemons/camerad.py) · [cam/piper_cam/](../cam/piper_cam/)).
   **합치지 않았다** — D405 hang 이 웹캠까지 죽이지 않게. 소유가 겹치지 않는다:
   camerad 는 RealSense 노드를 무조건 건너뛰고 rsd 는 v4l2 를 안 본다
4. ☑ `_build_cameras_json`을 `type: "shm"`으로 전환. `is_d405`/`warmup_s` 분기 제거.
   **`settings.camera_transport`(`direct`|`shm`) 스위치로 넣었다** — 되돌리기가 값 하나다.
   추론 시작 경로도 뒤바뀐다: `direct` 는 카메라를 **해제**하고 `shm` 은 **붙잡는다**
5. ☐ 컨테이너에서 `privileged`/`/dev` 마운트 제거, `ipc: host` 추가.
   Dockerfile/compose 에 `pip install -e bus/ shm/ rs/ cam/ phase/` 도 함께

2단계가 핵심이다 — **데몬을 쪼개기 전에 전송이 되는지부터 확인한다.**

## 검증

- 추론 1회: shm 경로로 정책이 정상 동작하는지 (fps, 지연이 기존과 같은지)
- 녹화 1 에피소드: 데이터셋 프레임이 raw 품질로 저장되는지 (JPEG 이중압축 없음)
- **D405 포함 구성**: color-only 0fps 문제가 camerad 안에서 해결되고 소비자엔 안 보이는지

깊이맵을 쓴다면 추가로:

- depth 세그먼트가 color와 **같은 프레임에서 나왔는지** (시간 정렬 — `wall_ns` 비교)
- 무효 픽셀(0)이 "가장 가까움"으로 보이지 않는지 — 실제 프레임을 눈으로 확인
- 데이터셋 메타에 컬러맵·범위 파라미터가 기록됐는지
- **depth 추가 전후 추론 fps 실측** (인코더 입력이 하나 늘어난다)
- camerad를 강제 kill → 소비자가 `DeviceNotConnectedError`로 깨끗하게 죽는지 (좀비 아님)
- 소비자를 강제 kill → camerad가 계속 살아있고 재접속되는지
- 세그먼트가 남지 않는지 (`ls /dev/shm/piper.cam.*` — unlink 누락은 누수다)
- 장시간(30분+) 추론 후 seqlock 재시도 카운터가 비정상적으로 높지 않은지

## 상태

◐ 진행중 — 1~4 완료(실기 확인: 추론·녹화 모두 shm 경로로 동작). 5(컨테이너) 남음

실기에서 걸린 것 두 가지를 여기 남긴다:

- **세그먼트 소유자는 데몬이다.** 게이트웨이가 "안 쓰는 것 치운다"며 unlink 하면
  발행 중인 파일이 사라져 발행자는 계속 쓰고 소비자는 못 연다. 조용히 깨진다
- **요청 프로파일이 데몬까지 가야 한다.** 안 그러면 librealsense 기본값으로 열리고
  (D405 는 848x480@10) 녹화 루프가 가장 느린 카메라에 묶인다
