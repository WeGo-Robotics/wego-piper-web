# 데몬 목록 — 무엇을 쪼갤 것인가

[daemon-split.md](daemon-split.md)의 부속 문서. 각 프로세스가 **무엇을 소유하는지**로 경계를 긋는다.

판정 기준은 하나다 — **"이것이 죽으면 무엇이 같이 죽어야 하는가?"**
같이 죽어야 하는 것끼리 한 프로세스, 아니면 다른 프로세스.

---

## 전체 목록

**장치를 소유하는 데몬**(상시, 호스트) — 하드웨어를 영구 점유하고 shm으로 넘긴다.

| # | 데몬 | 소유 | 현재 코드 |
|---|---|---|---|
| 1 | `piper-estopd` ☑ | heartbeat 시계 | [daemons/estopd.py](../daemons/estopd.py) — **분리 완료** |
| 2 | `piper-robotd` | CAN 소켓, 팔 상태, 프리셋/파킹, **기구학 안전 필터** | [robot_manager.py](../backend/app/services/robot_manager.py) (922) |
| 3 | `piper-camerad` | `/dev/video*` (v4l2) | [camera_manager.py](../backend/app/services/camera_manager.py) (637) |
| 4 | `piper-rsd` | RealSense USB 파이프라인 | [realsense_manager.py](../backend/app/services/realsense_manager.py) (615) |

**작업 데몬**(요청 시) — 하드웨어를 **직접 열지 않는다**. shm으로 읽고 쓴다.

| # | 데몬 | 소유 | 배포 | 현재 코드 |
|---|---|---|---|---|
| 5 | `piper-infer@<run>` | 추론 프로세스, GPU | 컨테이너 (GPU + `ipc: host`) | [process_manager.py](../backend/app/services/process_manager.py) 전역 인스턴스 |
| 6 | `piper-record@<sess>` | 녹화 프로세스 | 컨테이너 (GPU + `ipc: host`) | [record_manager.py](../backend/app/services/record_manager.py) (118) |
| 7 | `piper-train@<job>` | 학습 프로세스, GPU | 컨테이너 (GPU만) | [train_manager.py](../backend/app/services/train_manager.py) (216) |
| 8 | `piper-policysrv` | gRPC 정책 서버, GPU | 컨테이너 (GPU만) | [policy_server_manager.py](../backend/app/services/policy_server_manager.py) (62) |
| 9 | `piper-encoderd` | 인코더 프로브 세션(최대 8), GPU | 컨테이너 (GPU만) | [encoder_probe.py](../backend/app/services/encoder_probe.py) (281) |
| 10 | `piper-xferd` | Hub 업로드/다운로드, 데이터셋 편집 | 호스트 | [`_upload_pm`](../backend/app/routers/datasets.py#L21), [hub_client.py](../backend/app/services/hub_client.py) |

**인프라**(상시, 호스트)

| # | | 소유 | 현재 코드 |
|---|---|---|---|
| 11 | `redis` | 버스 (제어·메타데이터) | (신규) |
| 12 | `piper-gateway` | HTTP/WS 표면만 | `backend/app` 나머지 |

**데몬이 아닌 것** (게이트웨이에 남는다): [dataset_scanner.py](../backend/app/services/dataset_scanner.py),
[model_scanner.py](../backend/app/services/model_scanner.py) — 파일시스템 조회, 상태 없음.

**사라지는 것**: [zmq_bridge](../backend/app/services/zmq_bridge.py)(파라미터) ·
[control_bridge](../backend/app/services/control_bridge.py)(녹화 제어) → Redis로 흡수.
[preview_bridge](../backend/app/services/preview_bridge.py)(녹화 중 JPEG 프리뷰) → **불필요해진다.**
camerad가 이미 모든 프레임을 shm에 발행하므로 녹화 중이든 아니든 게이트웨이가 거기서 바로 읽는다.

---

## 개별 근거

### 1. `piper-estopd` — 제일 먼저 뗀다

지금 [asyncio.create_task](../backend/app/services/estop_watchdog.py#L43)라 게이트웨이 이벤트 루프가
막히면 워치독도 멈춘다. 실제로 D405 UVC 컨트롤 질의가 커널 D-state로 이벤트 루프 전체를 먹통으로
만든 전례가 있다 — **그 순간 E-stop이 죽어 있었다.**

77줄이고 의존이 `process_manager.kill()` 하나뿐이라 가장 작은 시범 케이스다.
분리 후 kill 대상은 "PID"가 아니라 **"버스에 정지 명령 + systemd 유닛 stop"** 이 된다.

> 무엇을 죽일지는 [10-exclusive-mode-guard.md](10-exclusive-mode-guard.md)의 활동 표와 같은 사실이다.
> 현재는 전역 `process_manager`(추론)만 죽이고 녹화는 안 죽인다. 함께 정해야 한다.

### 2. `piper-robotd` — 가장 크고, CAN의 유일한 소유자

922줄에 CAN 스캔/USB 복구/팔 제어/프리셋/세션이 다 들어있다. 데몬화하면서 내부를 더 쪼갤지는
별개 판단이고, **프로세스 경계로는 하나가 맞다** — 전부 같은 CAN 소켓을 만지기 때문이다.

USB 컨트롤러 리바인딩([`recover_usb_controllers`](../backend/app/services/robot_manager.py#L214),
`/sys` 쓰기)이 여기 있어서 **호스트 배포가 사실상 강제**다.

[robot-transport.md](robot-transport.md)에 따라 **LeRobot도 CAN을 직접 열지 않고 이 데몬을 거친다.**
그래서 robotd는 원래 `robot_manager`가 하던 일에 더해 **팔에 명령하는 네 주체
(추론·텔레오퍼레이션 녹화·웹 수동 제어·파킹) 전부의 관문**이 된다. 여기 추가되는 것:

- **데드맨** — 소비자가 죽거나 멈추면 팔을 세운다. E-stop watchdog(#1)과 별개로
  CAN을 쥔 프로세스 안에 있는 최종 방어선이다
- **하드 리밋** — 관절 범위·최대 변화량. 프록시(컨테이너 안)를 신뢰하지 않는다
- **기구학 안전 필터** — FK로 바닥면·자기충돌 방지 → [robotd-safety.md](robotd-safety.md)

기구학이 들어가면서 robotd가 **캘리브레이션의 단일 소유자**가 되는 것이 중요해진다.
FK는 관절 0점과 방향에 전적으로 의존하므로, 캘리브레이션이 다른 곳에 복제되면
필터가 조용히 틀린 판단을 한다 ([05-joint-calibration.md](05-joint-calibration.md) 참고).

### 3+4. `piper-camerad` / `piper-rsd` — 합칠지 쪼갤지가 결정 포인트

지금은 [camera_manager가 realsense_hub를 내부에서 호출](../backend/app/routers/models.py#L62)하는 구조라
하나로 합치는 게 자연스럽다. **그런데 쪼갤 이유가 하나 있다:**

librealsense는 D405에서 SIGABRT(-6)로 죽은 이력이 있다
([cameras.py:20-24](../backend/app/routers/cameras.py#L20) 주석이 이 사고를 기록하고 있다).
합치면 RealSense가 죽을 때 일반 UVC 카메라까지 같이 죽는다.
**"이것이 죽으면 무엇이 같이 죽어야 하는가"** 기준으로는 **쪼개는 쪽**이다.

### 5+6. `piper-infer` / `piper-record` — 컨테이너 (결정됨)

원래는 난제였다. 둘 다 GPU(정책) + `/dev/video`(카메라) + CAN(로봇)을 **전부** 요구해서,
컨테이너로 두면 지금처럼 `privileged` + `/dev` 통마운트가 필요하고 호스트로 두면
LeRobot+CUDA 전체를 호스트에 설치해야 했다.

**카메라와 로봇을 둘 다 shm으로 넘기면서 풀렸다**
([camera-transport.md](camera-transport.md), [robot-transport.md](robot-transport.md)).
소비자가 장치를 하나도 직접 열지 않으므로 컨테이너에서 하드웨어 접근이 전부 빠진다:

| | 지금 | 카메라 shm 후 | + 로봇 shm 후 |
|---|---|---|---|
| `privileged: true` | 필요 | **불필요** | 불필요 |
| `/dev:/dev` 통마운트 | 필요 | **불필요** | 불필요 |
| `/run/udev`, `/lib/modules` | 필요 | **불필요** | 불필요 |
| `network_mode: host` (CAN) | 필요 | 필요 | **불필요** |
| `ipc: host` | — | **필요** (신규, `/dev/shm` 공유) | 필요 |
| GPU | 필요 | 필요 | 필요 |

**GPU + `ipc: host`만 남는다.** Redis는 유닉스 소켓을 볼륨으로 마운트하면 네트워크도 필요 없다.

> 참고로 [`inference_mode == "server"`](../backend/app/routers/models.py#L271) gRPC 경로를 쓰면
> 정책만 따로 뗄 수도 있다. 하지만 shm으로 해결됐으므로 배포를 위해 억지로 서버 모드를
> 강제할 이유는 없어졌다. 두 모드 모두 그대로 지원한다.

### 7+8+9. GPU 3형제 — 컨테이너, 이견 없음

train / policy server / encoder probe 셋 다 GPU만 쓰고 하드웨어를 안 만진다.
의존성 지옥(CUDA·torch·torchcodec)이 여기 살고, 컨테이너가 정확히 이걸 위해 있다.

`piper-encoderd`가 데몬인 이유: [세션을 8개까지 메모리에 유지](../backend/app/services/encoder_probe.py#L30)한다.
게이트웨이에 두면 재시작 때마다 날아간다.

### 10. `piper-xferd` — 잡다한 파일 작업 한 곳

- Hub 업로드 ([`_upload_pm`](../backend/app/routers/datasets.py#L21) — 이미 별도 ProcessManager로 분리돼 있다)
- Hub 다운로드 ([hub_client.py](../backend/app/services/hub_client.py) — `run_in_executor`로 게이트웨이 스레드풀 점유 중)
- 데이터셋 편집 ([datasets.py:76](../backend/app/routers/datasets.py#L76))

**데이터셋 편집이 전역 `process_manager`를 쓴다 — 추론과 같은 인스턴스다.**
즉 추론 중 데이터셋 편집을 걸면 서로를 덮어쓴다. 데몬을 나누면 자연히 분리된다.

느리고 실패해도 안전한 작업들이라 한 데몬에 모아도 무방하다.

---

## 장치 소유권 중재 — 해소됨

프로세스를 쪼갤 때 원래 제일 어려운 부분이었다. 카메라와 CAN의 규칙이 서로 달랐기 때문이다.
**둘 다 같은 방식으로 풀렸다** — 장치를 데몬이 영구 소유하고, LeRobot 쪽은
서드파티 플러그인으로 shm에서 읽는다 ([camera-transport.md](camera-transport.md),
[robot-transport.md](robot-transport.md)). 소유권이 이전되지 않으니 중재할 것이 없다.

아래는 원래 문제가 무엇이었는지의 기록이다.

### 카메라 — 해결됨: camerad가 영구 소유

원래는 배타적 이전이 필요했다. 한 프로세스만 `/dev/video*`를 열 수 있어서, 지금은 게이트웨이가
[`_release_all_cameras()`](../backend/app/routers/models.py#L56)로 강제 해제한 뒤 추론을 띄운다:

> *"웹 프리뷰가 RealSense USB 디바이스를 쥔 채로 추론 subprocess가 같은 디바이스를 열려다
> 충돌해 카메라가 먹통이 된다."*

**[camera-transport.md](camera-transport.md)의 shm 방식이 이 문제를 없앤다** —
camerad가 장치를 계속 쥐고 있고 소비자는 공유 메모리만 읽는다. 소유권이 이전되지 않으므로
lease 협상 자체가 불필요하고, 웹 프리뷰와 추론이 **동시에** 같은 프레임을 볼 수도 있다
(지금은 불가능하다). `_release_all_cameras()`가 통째로 사라진다.

세그먼트의 존재가 곧 "camerad가 이 카메라를 잡고 있다"는 신호라 별도 프로토콜이 필요 없다.

### CAN — 해결됨: robotd가 영구 소유

원래는 카메라와 규칙이 달랐다. [`_clear_arm_errors`](../backend/app/routers/models.py#L77) 주석:

> *"팔과의 CAN 통신은 백엔드 robot_manager가 직접 보유하므로(추론 subprocess와 별개),
> 시작은 subprocess 기동 전에, 종료는 subprocess 정지 후에 호출하여 버스 경합을 피한다."*

소유권 이전이 아니라 "조용히 하기"라서 **quiesce(정숙) 상태**가 필요했다.

**[robot-transport.md](robot-transport.md)의 프록시 드라이버가 이 문제를 없앤다** —
LeRobot이 CAN을 직접 열지 않고 robotd가 영구 독점하므로 경합할 상대가 없다.
호출 순서로 표현되던 암묵적 프로토콜이 사라지고, quiesce 상태를 설계할 필요도 없어진다.

**즉 장치 소유권 중재 문제가 카메라·CAN 양쪽 모두 해소됐다.**

---

## stdout 파싱이 사라지는 자리

현재 텔레메트리는 백엔드가 CAN을 폴링해서 만드는 게 아니라
**wrapper stdout을 정규식으로 긁어서** 만든다 ([ws.py:60-85](../backend/app/routers/ws.py#L60-L85)):

```python
_RE_FPS.search(clean_line)   # "fps: 20.1" 같은 로그 줄에서 숫자 추출
_RE_OBS.search(clean_line)
_RE_LOOP.search(clean_line)
```

LeRobot이 로그 문구를 한 글자 바꾸면 **조용히 텔레메트리가 멈춘다.** 에러도 안 난다.
데몬 분리 후에는 wrapper가 구조화된 텔레메트리를 버스에 직접 publish하므로 이 정규식 3개가 사라진다.
**이것이 "subprocess 최소화"의 실질적 수확 중 하나다.**

---

## 착수 순서

[daemon-split.md](daemon-split.md)의 6단계에 이 목록을 대응시키면:

| 단계 | 대상 | 왜 이 순서 |
|---|---|---|
| 1 ☑ | `piper_bus/` + `redis`(#11) | [bus/](../bus/) — 계약이 먼저. 데몬 0개 |
| 2 ☑ | `piper-estopd`(#1) | 최소 크기, 최대 안전 이득, 시범 케이스 |
| 3 | 브리지 3개 → Redis | 프로세스 경계는 그대로, 전송만 교체 |
| 4 | **shm 전송 계층** — [카메라](camera-transport.md) + [로봇](robot-transport.md) | ⚠ 아래 데몬들의 전제조건 |
| 5 | `robotd`(#2) → `camerad`(#3) → `rsd`(#4) | 큰 것부터. 각각 독립 검증 |
| 6 | `train`(#7) → `policysrv`(#8) → `encoderd`(#9) → `xferd`(#10) | systemd 유닛화, stdout 파싱 제거 |
| 7 | `infer`(#5) / `record`(#6) | 컨테이너에 GPU + `ipc: host`만 남는다 |
| 8 | `piper-gateway`(#12) 정리 | `services/`에 스캐너만 남는다 |

**4단계는 daemon-split.md의 원래 6단계 목록에 없던 것이다.** 위 "장치 소유권 중재"에서
드러난 전제조건이라 추가했다.

**[robotd-safety.md](robotd-safety.md)(기구학 안전 필터)는 이 순서와 별도 트랙이다.**
5단계에서 robotd가 서고 나면 언제든 붙일 수 있고, URDF 확보라는 독립적인 선결 조건이 있다.
분리 작업의 경로에 두면 URDF를 기다리느라 전체가 멈춘다.

---

## 미결정

1. **`camerad`/`rsd` 합칠 것인가** — 크래시 격리(쪼갬) vs 단순함(합침). 위 3+4 참고.
   shm 전송을 쓰면 세그먼트가 카메라마다 독립이라 **쪼개도 소비자 쪽 변화가 없다** — 쪼개는 쪽이 유리해졌다
2. ~~**`infer`/`record` 배포 위치**~~ → **결정: 컨테이너, GPU + `ipc: host`만.**
   카메라·로봇 shm으로 하드웨어 접근이 전부 빠지면서 풀렸다 (`network_mode: host`도 불필요)
3. **`record`가 GPU를 쓰는가** — 비디오 인코딩 경로가 NVENC인지 CPU인지에 따라
   [10-exclusive-mode-guard.md](10-exclusive-mode-guard.md)의 배타 표가 달라진다
4. **`robotd`를 팔마다 하나씩 둘 것인가** — 지금은 한 프로세스가 모든 CAN 인터페이스를 관리한다.
   양팔(bimanual)에서 한쪽 문제가 다른 쪽을 죽이지 않게 하려면 나누고 싶지만,
   **[robotd-safety.md](robotd-safety.md)의 기구학 필터가 이걸 사실상 막는다** —
   양팔 구성에서는 두 팔이 서로 충돌할 수 있고, 그걸 보려면 필터가 **두 팔의 자세를 동시에**
   알아야 한다. 프로세스를 나누면 팔 간 충돌 검사를 할 수 없다. → **한 프로세스 유지 쪽**
5. **URDF 확보** — [robotd-safety.md](robotd-safety.md)의 선결 조건.
   저장소에 URDF·xacro·메시가 하나도 없다. AgileX `piper_description`을 가져오거나
   DH 파라미터를 확정해야 기구학 필터를 시작할 수 있다

## 상태

☐ 미착수 — 미결정 1·3·4·5와 daemon-split.md 미결정 1~5 해소 후 착수
(#2는 결정 완료)
