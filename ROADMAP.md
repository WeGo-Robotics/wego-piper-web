# 구현 순서 — 리팩터링과 신기능을 안 꼬이게

## 현재 위치 (2026-08-15)

**구조 개편 본선 3b 완료 (1~8).** 게이트웨이가 CAN 도 카메라도 직접 열지 않고,
데몬 넷이 장치를 소유하며, 컨테이너에는 GPU + `ipc: host` 만 남았다.
오래 도는 프로세스(학습·정책서버·업로드·편집·페이즈)는 systemd 유닛이라
**게이트웨이를 재시작해도 살아있고 journald 로 로그까지 이어 읽는다** —
실제 학습(ACT 20000스텝)으로 확인했다: 2000스텝에서 게이트웨이를 죽였는데
학습은 계속 돌았고, 다시 띄우니 6000 → 7000/20000 으로 진행률까지 이어졌다.

마지막 둘(7·8)은 **더 해서 얻을 게 없다**는 결론으로 닫았다 — 추론·녹화를 유닛으로
감싸도 estopd 가 죽이므로 수명이 안 늘고(실측 2.5초), 게이트웨이에는 이미 장치를
여는 코드가 없다. 빠뜨린 게 아니라 안 하기로 정한 것이라 근거를 3b 절에 적어 뒀다.

**학습이 다른 기계에서 돈다 — [SSH 러너](backend/app/services/training/runners/ssh.py).**
`PIPER_TRAIN_SSH_HOST` 하나로 학습만 원격 tmux 세션에 나간다. `LocalRunner`/
`SystemdRunner` 와 같은 이음매라 위쪽(메트릭 파서 → WS)은 로그가 어디서 왔는지
모른다. 실기 확인: 300스텝 완주(`progress 1.0`, 로그 280줄), 러너를 버렸다가
**재부착하니 없던 동안의 로그 138줄을 처음부터 다시 읽고** 총 스텝 3000 까지 되찾았다.
막고 있던 것은 "검증할 원격 머신"이었는데, 원격 추론이 붙으면서 그 전제가 사라졌다.
**배타 가드도 같이 배웠다** — 원격 학습은 이 기계의 GPU 를 안 쓰므로 추론·녹화를
막지 않는다(러너의 `occupies_local_gpu`). 이걸 안 하면 원격으로 보내도 여전히
409 라서 기능을 켠 값이 안 났다.

**제품처럼 배포한다 — 두 번째 기계(192.168.0.120)가 돈다.**
소스 체크아웃이 아니라 아티팩트다: 이미지 / 데몬 wheel / 데몬 소스 **세 레이어**를
따로 올린다 ([deploy/RELEASE-CHECKLIST.md](deploy/RELEASE-CHECKLIST.md)).
`v0.2.0` → `v0.3.8` 까지 하루에 열 번 넘게 올렸고, 배포마다 레이어를 **먼저 재고**
바뀐 것만 보낸다 (frontend 만이면 27MB, backend 가 끼면 3.4GB).

**원격 추론이 기계를 건넜다.** 두 번 돌렸다 — 먼저 `.42` 루프백(790스텝),
그다음 **`.42` 클라이언트 → `.120` 정책 서버**(1064스텝 / 53초).
**네트워크가 사실상 공짜다**: 둘 다 중앙값 **19.90fps**(목표 20)이고 액션 큐도
75 → 69 로 거의 안 줄었다. 청크 100개를 미리 받아두는 구조라 왕복 지연이 제어
루프에 안 묻는다. 연결에 4.3초 걸린 것은 서버가 체크포인트를 읽는 시간이다.

그 전까지 이 경로는 **한 번도 돈 적이 없었다** — `main()` 이 `_paused` 를 `global`
선언 없이 대입해 제어 루프 첫 줄에서 죽고 있었다 (`Total steps: 0`). 돌려봐야만
드러나는 종류라 이제 정적 테스트가 막는다.

기계 간 실행에서 걸린 둘은 전부 **경로와 포트**였다. 체크포인트 경로는 **서버
컨테이너 기준**이어야 하고(`/data/models/...` — 호스트 홈은 컨테이너가 못 본다),
정책 서버 포트는 브리지 네트워크라 `ports: !override` 로 내보내야 했다.

> **반대 방향(`.42` 서버 + `.120` 로봇)은 아직 안 돌았다.** `.120` 의 RealSense
> color 가 rsd 에 안 보인 게 이유였는데 **지금은 보인다**(`rs:335122270699:color`).
> `.120` → `.42:8088` 도달도 확인했다. 남은 것은 카메라가 정책과 안 맞는다는 것뿐이다 —
> 체크포인트는 `top`·`hand` 둘을 480x640 으로 요구하는데 `.120` 에는 RealSense 하나와
> USB 웹캠 하나가 있다.

**장치가 사라지면 말한다.** USB 를 뽑아도 화면이 마지막 상태에 머물고 추론만 뒤늦게
"세그먼트가 없습니다"로 죽던 것을 고쳤다. **판정은 장치를 쥔 데몬이 한다** —
camerad 는 `/dev/videoN`, robotd 는 `/sys/class/net/can0`, rsd 는
`/sys/bus/usb/devices/<포트>` 소멸을 1초마다 본다. 게이트웨이의 세그먼트 신선도 판정은
데몬이 얼어붙어 스스로 결론을 못 낼 때를 위한 **보조**로 남는다.

**시스템 메시지 인터페이스** — 경보·실패가 어느 페이지에서든 같은 자리에 뜬다.
`window.alert` 17곳, `window.confirm` 13곳을 걷어냈다. 취향이 아니라 안전이다:
둘 다 이벤트 루프를 멈춰 E-stop heartbeat 를 끊는다.

**Phase 4 두 번째 — [policy-ui-spec](feature/policy-ui-spec.md) 1~3.**
모델별 화면 항목이 `policies/*.yaml` 로 내려갔고 `core/policies.py` 는 로더가 됐다.
공개 함수 결과는 골든 테스트로 고정했다. **LeRobot 에서 기본값을 생성하게 한 것이
바로 값을 했다** — 옛 화면 테이블이 노출하던 **없는 필드 둘**을 잡았다
(`pi0_fast.freeze_vision_encoder`, `vqbet.n_action_steps`). 둘 다 꺼진 정책이라
아무도 안 밟았지만, 켰으면 학습 시작에서 알 수 없는 설정 키로 죽었을 것이다.

**Phase 4 첫 기능이 닫혔다 — [camera-profiles](feature/camera-profiles.md).**
구조 개편의 배당금을 처음 실제로 받은 사례다: 원인 7개 중 넷이 코드를 쓰기도 전에
사라졌고(해상도 전달·해제 후 재적용·트리거 6개 배선), 남은 셋만 구현했다.
적용 지점은 데몬 `connect()` 한 곳이다. D405 로 확인 — 컨트롤을 전부 기본값으로
되돌린 뒤 **연결만 했는데** 노출·화이트밸런스가 복원됐다.

| 단계 | | 상태 |
|---|---|---|
| Phase 0 | 배타 모드 가드 · 추론 파라미터 드리프트 | ☑ |
| Phase 1 | 계약 세우기 (WS·robot_type·관절 수·정책 레지스트리) | ☑ |
| Phase 2 | 저위험 추출 · cloud-training 0~2 · 프리셋 1~3 | ☑ |
| Phase 3a | phase-annotation 1~3 | ☑ |
| **3b-1~3** | 버스 · estopd · 브리지 → Redis | ☑ |
| **3b-3.5** | job 레지스트리 · WS `job_id` · **SSH 러너** | ☑ 실기 확인 (300스텝 완주 · 재부착) |
| **3b-4** | shm 전송 — 카메라 · 로봇 | ☑ 실기 확인 |
| **3b-5** | rsd · camerad · robotd | ☑ 실기 확인 |
| **3b-6** | systemd 유닛화 | ☑ 학습·정책서버·xferd(재부착 실기 확인) **+ 장치 데몬 넷** (`deploy/install-daemons.sh`) |
| **3b-7** | 컨테이너화 | ☑ 단일 백엔드 컨테이너 실기 확인. **실행별 분리·encoderd 는 안 한다** (아래) |
| **3b-8** | 게이트웨이 정리 | ☑ 장치 접근 0 — 남은 것은 UI 상태라 게이트웨이 것이 맞다 (아래) |
| **배포** | 두 번째 기계(.120) — 이미지·wheel·데몬소스 3레이어 | ☑ [RELEASE-CHECKLIST](deploy/RELEASE-CHECKLIST.md) · `v0.3.8` |
| **운영 가시성** | 장치 사라짐 경보 · 시스템 메시지 · 블로킹 다이얼로그 제거 | ☑ |
| Phase 4 | 깊이맵(인코딩 ☑ / 정책 입력 ☐) · **camera-profiles ☑ · policy-ui-spec ☑** · phase-annotation 4~8 · cloud-training 5~12 | ◐ |

### Phase 4 세부

| 기능 | 상태 | 남은 것 |
|---|---|---|
| [camera-profiles](feature/camera-profiles.md) | ☑ | v4l2 웹캠에서의 확인만 (지금 RealSense 둘뿐이라 menu 스위치 경로는 단위 테스트만) |
| 깊이맵 정책 입력 | ◐ | 인코딩·메타·범위조절 ☑ / **녹화→학습→추론 한 바퀴 + fps 측정** ☐ |
| [policy-ui-spec](feature/policy-ui-spec.md) | ☑ | 8단계 전부. **정책 추가 = 파일 하나** — 실기로 확인 |
| [phase-annotation](feature/01-phase-annotation.md) 4~8 | ☐ | UI · 굽기 · 추론 경로 |
| [cloud-training](feature/cloud-training.md) 5~12 | ☐ | 데이터 전송 · 체크포인트 회수 · 비용 가드 |
| 원격 추론 (.120 → .42) | ◐ | gRPC 경로 ☑ (루프백으로 790스텝) / **기계 간**은 .120 카메라가 모자라 대기 |
| [bimanual](feature/bimanual.md) 수집·학습·추론 | ◐ | 소프트웨어 전 구간 ☑ — bi 클래스 4개(WeGo repo + shm) · 녹화/추론 복수화 · 좌/우 등록(side) · grpc 즉석 조립 삭제. **실기는 팔 4대 + udev 4이름 대기** (§7 체크리스트) |

### 바로 이어서 할 것

1. **깊이맵을 실제 정책 입력으로** — 인코딩·메타 기록·범위 조절은 ☑ 다
   (`rs/piper_rs/depth.py`, `meta/piper_cameras.json`, 카메라 설정 모달).
   남은 것은 **녹화 → 학습 → 추론을 한 바퀴 돌려 fps 를 재는 것** — 인코더 입력이
   하나 늘면 얼마나 느려지는지는 돌려봐야 안다
2. **.120 의 RealSense color 스트림** — rsd 가 depth·infrared 는 보는데 color 를 못 본다.
   그거 하나 때문에 기계 간 원격 추론이 막혀 있다 (정책이 `top`+`hand` 둘을 요구)
3. **실기 체크리스트** — B·C·D 와 E 절반이 남았다. 팔이 움직이는 것이라 사람이 있어야 한다.
   오늘 A 가 **덤으로 닫혔다**: heartbeat 없이 추론을 시작하니 estopd 가 2초 만에
   `code -9` 로 죽였고, 그때 로그에 `데드맨 (can1): 300ms 동안 명령 없음 — 현재 자세로
   정지` 가 찍혔다 — E-stop 과 안전층이 같이 검증됐다
4. **데이터셋 경로가 둘로 갈린다** — 목록에 뜨는 이름이 학습에 그대로 안 먹힌다.
   로컬 데이터셋은 `~/.cache/huggingface/lerobot/` 인데 `settings.datasets_dir` 는
   `~/.cache/huggingface/hub` 다. 재부착 검증 중에 걸렸다 — 없는 repo 로 학습을
   걸면 HF 404 로 죽는다

### 사람이 해야 하는 것 (sudo·하드웨어)

| | 왜 |
|---|---|
| ~~`loginctl enable-linger`~~ | ☑ 켜짐. `PIPER_PROCESS_RUNNER=systemd` 로 다섯 소유자가 유닛으로 뜬다 |
| ~~데몬 넷을 유닛으로~~ | ☑ `deploy/install-daemons.sh`. 수동 실행은 **죽어도 아무도 모른다** — robotd 가 조용히 죽어 추론이 세그먼트 없다고 죽은 적이 있다 |
| [udev 규칙 적용](deploy/udev/99-piper-can.rules) + 로봇 재등록 | `can0`/`can1` 이 포트를 바꿔 꽂으면 **뒤바뀐다**. 실제로 겪었고, 등록이 살아 있었으면 leader 팔에 follower 명령이 갔을 것이다 |
| joint3 캘리브레이션 결정 | raw 2103 인데 범위 최대가 0 — 지금 녹화하는 데이터에서 상단이 잘린다. 넓히면 기존 데이터셋·모델과 어긋난다 |

[refactor/](refactor/) 13개 + 구조 개편 5문서, [feature/](feature/) 3개를 한 순서로 놓는다.

---

## 0. 확정된 결정

순서를 정하는 전제들. 이미 답이 나왔다.

| 결정 | 내용 | 영향 |
|---|---|---|
| **배포** | 로봇마다 별도 인스턴스 | systemd 최상위 + LeRobot/CUDA만 컨테이너. `install.sh`와 **기기별 설정 분리**가 요구사항 |
| **데이터 반출** | 허용 (HF Hub 포함 전 경로) | cloud-training 0~12 전체가 범위 안 |
| **데이터 집계** | HF Hub private repo | N대의 데이터를 모으는 경로 = 클라우드 학습에 보내는 경로. **이미 구현돼 있다** ([datasets.py:183](backend/app/routers/datasets.py#L183)) |
| **자체 Hub** | 나중에 사내 서버로 대체 | **HF API 호환**이어야 한다 (아래) |

### 기기별 설정 분리

N대에 같은 소프트웨어가 나가므로, 무엇이 이미지에 들어가고 무엇이 기기에 남는지를
처음부터 갈라야 한다. 나중에 분리하려면 N대를 전부 손봐야 한다.

| 기기별 (이미지 밖) | 공통 (이미지 안) |
|---|---|
| `robot_id`, CAN 인터페이스 이름, 카메라 USB 포트/시리얼 | 코드·의존성, LeRobot·CUDA 스택 |
| **관절 캘리브레이션** (팔마다 0점이 다름) | systemd 유닛 정의, URDF 자체 |
| **카메라 프로파일** (조명·렌즈가 현장마다 다름) | 컬러맵·클리핑 범위 (데이터셋 계약) |
| **URDF 오프셋·바닥면 높이** (설치 높이가 다름) | 기본 파라미터 |

전부 `settings.config_dir` 아래로 모이는 것들이다 ([config.py:50](backend/app/core/config.py#L50)).
여기를 **기기별 상태의 단일 경계**로 못 박고 데이터 루트와 함께 백업·이관 단위로 다룬다.

> ⚠ **한 대에서 맞춘 [robotd-safety](refactor/robotd-safety.md) 필터 설정을 다른 대에 쓰면
> 팔이 바닥을 친다.** 설치 높이가 다르면 FK 결과가 달라지는데 필터는 그걸 모른다.

### 자체 Hub — HF API 호환이어야 하는 이유

HF 저장소가 git + LFS인 건 맞지만 **클라이언트가 git으로 말하지 않는다.**
`huggingface_hub`는 REST API로 통신하고 git은 그 뒤의 저장 계층이다.

결정적인 제약: **LeRobot 내부가 `huggingface_hub`를 직접 부른다.** 데이터셋 로드도
`push_to_hub`도 우리 코드를 안 거친다. 우리 쪽에 스토리지 추상화를 만들어도 LeRobot은
여전히 huggingface.co를 본다. "LeRobot을 수정하지 않는다"는 원칙을 지키는 한
**레버는 `HF_ENDPOINT` 하나뿐이다.**

우리가 쓰는 API 표면은 좁다 — `list_models`/`list_datasets`, `model_info`/`dataset_info`,
`snapshot_download`, `whoami`, 업로드, 그리고 LeRobot 내부의 로드/push.

**좋은 소식**: `models--org--name/snapshots/hash/` 로컬 캐시 레이아웃은 서버가 아니라
**클라이언트가 정한다.** endpoint를 바꿔도 그대로다 →
[#11](refactor/11-hf-cache-layout.md)과 두 스캐너는 자체 서버로 옮겨도 그대로 쓴다.

**정직한 위험**: 부분 구현은 조용히 깨진다 (`huggingface_hub`는 404에 예외 대신 빈 결과를
돌려주는 경로가 있다). 그리고 HF가 대용량 전송을 LFS → Xet으로 옮기는 중이라
자체 서버가 이걸 따라가야 하는지 **버전 고정 시점에 확인 필요**.

서버 자체는 Phase 4 이후 판단해도 늦지 않다. **지금 할 일은 Phase 1의 배선 두 줄뿐.**

---

## 1. 핵심 판단 — 세 기능은 성격이 완전히 다르다

"기능 먼저 vs 리팩터 먼저"로 물으면 답이 안 나온다. 기능마다 답이 다르다.

| 기능 | 구조 개편과의 관계 | 결론 |
|---|---|---|
| [camera-profiles](feature/camera-profiles.md) | 구조 개편이 **이 기능의 절반을 삭제한다** | **뒤로** → ☑ 실제로 삭제됐고, 남은 절반도 완료 |
| [cloud-training](feature/cloud-training.md) | 0~2는 **독립 리팩터**, 3~4는 Redis 이후 | **쪼개서 앞당김** |
| [phase-annotation](feature/01-phase-annotation.md) 1~3 | 거의 **안 겹친다** | **지금** |
| [parameter-presets](feature/parameter-presets.md) | 학습은 독립 · 추론은 **#1 단계 2에 의존** | **쪼갬** |

---

## 2. 충돌 지도

같은 코드를 여러 작업이 건드리는 지점. 순서가 틀리면 두 번 고친다.

### `_release_all_cameras()` — 가장 큰 충돌

camera-profiles는 트리거 4·5·6단계를 **이 함수 직후**에 배선한다
("해제 직후, subprocess 기동 전이 유일하게 안전한 창").
그런데 [camera-transport](refactor/camera-transport.md)는 **이 함수를 통째로 삭제한다.**

| camera-profiles가 든 원인 7개 | camera-transport 이후 |
|---|---|
| (3) 하드웨어 리셋이 컨트롤을 되돌린다 | camerad 내부 문제로 축소 |
| (6) 해제 후 다시 맞춰주는 단계가 없다 | **해제를 안 하므로 소멸** |
| RealSense 실행 중 컨트롤 접근 금지 | camerad만 접근 → 경합 자체가 없음 |
| 트리거 6개 배선 | camerad 기동 시 1회 |
| (4)(5) 해상도·FPS가 장치에 안 간다 | ☑ **해결됨** — `prepare_cameras(w,h,fps)` → 데몬 `connect()` → `resolve()` |
| (1)(2)(7) 저장·매칭·적용 순서 | **남는다** — 다만 camerad 안의 코드로 |

**실제로 그렇게 됐다.** 위는 예측이었고, 3b-4/3b-5 이후 확인한 결과가 같다 —
7개 중 4개(4·5·6 + 트리거 배선)가 사라졌다. 상세는
[feature/camera-profiles.md](feature/camera-profiles.md) 머리의 상태표.

### ~~`_build_cameras_json` — 3중 충돌~~ ☑ 해소

camera-transport(`type: "shm"`), camera-profiles(하드코딩 제거), 깊이맵(세그먼트 추가)이
**같은 함수**를 고친다 — 예정대로 한 번에 끝났다. camera-profiles 몫이던
"하드코딩 제거"는 별도 작업이 아니라 shm 전환에 딸려 왔다: 발행자(데몬)가 값을 정하니
실행 경로가 해상도를 조립할 일 자체가 없다.

### `TrainManager` — cloud-training과 daemon-split이 같은 이음매

cloud-training 0~2가 `TrainRunner` 이음매를 만들고,
daemon-split 6의 systemd 유닛화는 그 이음매에 **`SystemdRunner`를 하나 더 붙이는 일**이다.
동시에 할 일이 아니라 **순차**다 — 그래서 0~2를 앞당길 수 있다 (§3 참고).

단 cloud-training 3(job 레지스트리)은 상태를 영속화하므로, Redis 이전에 하면
`jobs.json`을 만들었다가 Redis로 다시 옮기게 된다. **3은 Redis 이후.**

### WS 계약 — 4중 충돌

[#12](refactor/12-ws-message-contract.md)가 계약을 세우기 전에 cloud-training(`job_id`),
~~camera-profiles~~(WS 이벤트 없이 폴링으로 끝냈다), phase-annotation(페이즈 텔레메트리)이 각자 추가하면
**16종이 20종 되고 타입은 여전히 없다.**

### 나머지

| 충돌 | 내용 |
|---|---|
| `observation.state` 7→8 | phase-annotation이 바꾼다 → [#8](refactor/08-piper-joints.md)이 먼저 |
| `robot_type` | robot-transport가 새 타입 추가 → [#9](refactor/09-robot-type.md)가 먼저 |
| 배타 모드 가드 | cloud-training·phase-annotation·daemon-split이 전부 항목 추가 → [#10](refactor/10-exclusive-mode-guard.md)이 먼저. 지금 8곳 → 나중에 12곳 |
| `HfApi()` 두 곳 | endpoint 설정이 한쪽에만 걸리면 나머지가 조용히 huggingface.co를 본다 |

---

## 3. 절대 하면 안 되는 순서

| 금지 | 이유 |
|---|---|
| ~~camera-profiles 트리거 배선 **before** camera-transport~~ | ☑ 지켜짐 — 트리거는 배선하지 않았고, camera-transport가 그 자리를 없앴다 |
| cloud-training 3 **before** #12 · Redis | WS 문자열이 늘고, `jobs.json`을 다시 옮긴다 |
| phase-annotation 굽기(6) **before** #8 | "7"이 박힌 자리가 늘어난다 |
| robot-transport **before** #9 | 새 타입 추가에 5곳 수정 |
| daemon-split 5(robotd/camerad) **before** shm 전송 계층 | 장치 소유권 이전 문제가 재현된다 |
| 아무거나 **before** #10 | 배타 규칙이 8곳 → 12곳 |

---

## 4. 순서

### Phase 0 — 지금 위험한 것 ☑ 완료

| 작업 | 왜 지금 |
|---|---|
| [#10 배타 모드 가드](refactor/10-exclusive-mode-guard.md) | **안전.** 녹화 중 학습·추론이 시작되고, `/inference/start-custom`은 녹화 중에도 카메라를 뺏는다. 게다가 후속 3개의 전제 |
| [#1](refactor/01-inference-params.md) 드리프트 2건만 핀포인트 | 지금 UI 값이 유실된다 |

### Phase 1 — 계약 세우기 ☑ 완료

전부 **나중 작업이 소비하는 것**들이다. 여기서 안 하면 각 기능이 자기 방식으로 확장한다.

| 작업 | 결과 |
|---|---|
| [#12](refactor/12-ws-message-contract.md) + [#13](refactor/13-process-state-union.md) WS 계약 | `core/ws_messages.py` + `types/ws.ts` 판별 유니언. **오타가 컴파일 에러**가 됐다 |
| [#9 robot_type](refactor/09-robot-type.md) | 프론트 5곳 제거. 로봇 페이지 선택이 무시되던 **버그도 함께 수정** |
| [#8 PIPER_JOINTS](refactor/08-piper-joints.md) | `/inference/validate` 응답에 `robot_joints`. 새 엔드포인트 없이 해결 |
| [#2 정책 레지스트리](refactor/02-policy-registry.md) | `core/policies.py`. `sac` 제거 · `pi0_fast` 학습 추가 · **`pi05`→`pi0` 오태깅 수정** |
| `HF_ENDPOINT` 배선 + `HfApi()` 통합 | `settings.hf_endpoint` → `HfApi` 1개 + **subprocess env 로 LeRobot 까지** |
| **`robot_id` 를 settings에** | `settings.resolved_robot_id` (비면 호스트명) |

검증: pytest **51개** · `npm run build` · ruff 통과.
실기 확인이 남은 것은 각 문서의 "검증" 절 참고.

### Phase 2 — 저위험 (병렬 가능) ☑ 완료

| 작업 | 비고 |
|---|---|
| [#3](refactor/03-wrapper-bootstrap.md) [#4](refactor/04-err-bits.md) [#5](refactor/05-joint-calibration.md) [#6](refactor/06-joint-names-frontend.md) [#11](refactor/11-hf-cache-layout.md) | 동작 변화 0. 아무것도 안 막는다. **#5는 robotd 캘리브레이션 단일 소유자화의 전제**라 Phase 3 전에 |
| **cloud-training 0~2** | 메트릭 파서 분리 · `TrainRunner` Protocol · `build_train_args` 경로 분리. 문서 자체가 *"클라우드와 무관한 순수 리팩터, 지금 당장 해도 손해 없음"* 이라 한다 |
| **[parameter-presets](feature/parameter-presets.md) 1~3** | 공통 프리셋 스토어 + **로봇 프리셋 이관** + 학습 프리셋. 추론 프리셋만 `PARAM_SPEC` 을 기다린다 |

검증: pytest **106개** · `npm run build` · ruff 통과. 실기 확인은 각 문서의 "검증" 절 참고.

### Phase 3a — phase-annotation 1~3 (독립 트랙) ☑ 완료

`wrapper/phase_fsm.py` + 분석 배치 + API. **백엔드 구조 개편과 파일이 거의 안 겹친다.**
문서 자체가 *"1~2단계만 해도 에피소드 품질 검사 도구로 쓸 수 있다"* 고 한다.

> 4~5(UI)는 #12 뒤면 언제든. **6(추론 경로)만 robot-transport 뒤로** — wrapper의 obs 조립 지점이 바뀐다.

### Phase 3b — 구조 개편 본선 ☑ 완료

| # | 작업 | 비고 |
|---|---|---|
| 1 | ☑ `piper_bus/` 계약 + Redis ([bus/](bus/)) | 데몬 0개 |
| 2 | ☑ [estopd](daemons/estopd.py) — **게이트웨이를 얼려도 팔이 선다** | 최소 크기, 최대 안전 이득, 시범 케이스 |
| 3 | ☑ 브리지 3개 → Redis | 경계는 그대로, 전송만. 주소 3개 → `PIPER_REDIS_URL` 1개 |
| **3.5** | cloud-training **3 ☑**(job 레지스트리 · WS `job_id`) **4 ☑**([SSH 러너](backend/app/services/training/runners/ssh.py)) | 3 은 클라우드 없이도 값이 난다 — **서버 재시작에도 학습이 보인다.** 4 는 원격 머신을 기다렸는데, 원격 추론이 붙으면서 그 전제가 풀렸다 |
| 4 | **shm 전송 계층** — 카메라 **1~5 ☑**(추론·녹화 실기 확인) · [로봇](refactor/robot-transport.md) **2~4 ☑**(실기 확인) / 5~6 남음 | 데몬 분리의 전제조건. 깊이맵은 전송 검증 후로 미룸 |
| 5 | **rsd ☑ · camerad ☑ · robotd ☑** (버스 RPC · shm 발행 · 안전층) | **합치지 않았다** — D405 hang 이력. 게이트웨이는 이제 카메라 장치를 전혀 안 연다 |
| 6 | ☑ 학습 러너 · 정책서버 · xferd(업로드·편집·페이즈) — 선택은 [`make_process()`](backend/app/services/systemd_process.py) 한 곳 | 유닛이 소유자라 게이트웨이 재시작에도 산다. journald 로 **로그까지 이어 읽는다** |
| 7 | ☑ **단일 백엔드 컨테이너** (하드웨어 권한·호스트 네트워크 둘 다 제거, GPU + `ipc: host` 만 남음). 실행별 분리와 encoderd 는 **안 한다** | 아래 |
| 8 | ☑ 게이트웨이 정리 — `robot_manager` 995→713줄, `camera_manager` 685→482줄, `arm_bridge` 삭제. **장치를 여는 코드가 0** | 남은 것은 UI 상태다 |

#### 7·8 을 "안 한다"로 닫은 이유

원래 문장은 *"infer/record/encoderd 를 실행별 컨테이너로"*(7)와 *"스캐너만
남는다"*(8)였다. 둘 다 **더 해서 얻을 게 없다**는 결론이라 ☑ 로 닫는다.
빠뜨린 게 아니라 **안 하기로 정한 것**이다.

**추론·녹화를 유닛/컨테이너로 안 쪼갠다 — 수명이 안 늘어난다.**
유닛의 값은 "게이트웨이가 죽어도 산다"인데, 이 둘은 **게이트웨이가 죽으면 죽는 게
설계된 동작이다.** heartbeat 가 끊기면 estopd 가 버스에 올라온 PID 를 SIGKILL 한다
— 실측했다: 녹화 자리에 프로세스를 올리고 heartbeat 를 끊자 **2.5초 뒤 signal 9**,
`reason=heartbeat_timeout`. 유닛으로 감싸도 estopd 는 MainPID 를 죽인다.
`ESTOP_TARGETS` 에서 녹화를 빼기로 결정한다면 그때 다시 볼 일이고, **그 결정이 먼저다**
(refactor #10 이 "로봇을 움직이는 것 전부"로 정한 자리다).

> ⚠ 이 확인 과정에서 CLAUDE.md 의 *"녹화 중 브라우저 종료 시 에피소드를 독립적으로
> 완결한다"* 가 **더 이상 참이 아님**이 드러났다. #10 이 E-stop 범위를 넓히면서
> 바뀐 것이고, 문서만 그대로였다. CLAUDE.md 를 고쳐 두었다.

**encoderd 를 안 만든다.** [daemon-inventory](refactor/daemon-inventory.md#L40) 가 이미
*"유닛으로 감쌀 일이 아니다"* 라고 적었다. 프로브는 몇 초짜리 `subprocess.run` 이고
남는 것은 numpy 배열뿐이다. 상시 서버로 떼서 얻는 것은 "재시작 때 세션 8개가 안
날아간다" 하나인데, 그건 다시 돌리면 되는 것이다.

**게이트웨이는 이미 정리됐다.** `robot_manager` 는 전부 `_call()` 버스 RPC 고
`camera_manager` 는 `_hub()` 위임이다 — **두 파일 어디에도 장치를 여는 코드가 없다.**
줄 수가 목표만큼 안 준 것은 그 뒤에 기능이 붙어서다(프로파일 키·컨트롤 적용 위임).
남은 상태는 등록·역할·슬롯·별칭·파킹 프리셋인데 이건 **장치 소유가 아니라 UI 상태**라
게이트웨이가 갖는 게 맞다. "스캐너만 남는다"가 현실보다 좁게 적혀 있던 것이다.

### Phase 4 — 기능 완성

| 기능 | 상태 |
|---|---|
| ~~parameter-presets 4~5~~ | ☑ 완료 — 추론 프리셋 + 프리셋별 성공률 |
| ~~camera-profiles~~ | ☑ **완료** — 컨트롤 값 저장(`presets` domain=camera) · v4l2 안정 키 · 자동 모드 순서. 적용은 데몬 `connect()` 한 곳. D405 로 확인(되돌린 뒤 연결만으로 복원) |
| **phase-annotation 6~8** | 굽기 · 학습 · 추론 경로. ~~4~5(편집 UI)~~ 는 [episode-editor](feature/episode-editor.md) 로 ☑ — 분류기는 `python -m piper_phase` 단독 실행 |
| **[episode-editor](feature/episode-editor.md) 1~3** | ☑ **완료** — `/episodes` 뷰어(동영상 기본 + 프레임 캐시 폴백) · §3.5 수동 분석(파라미터·미리보기) · 타임라인 수작업 편집(분할 S/병합 M/드래그/undo). 남은 것: 4(삭제·task 이관 + **사이드카 재매핑 버그**) · 5(굽기 UI) |
| **분리수거 파이프라인 (YOLO→LLM→VLA)** | 앞 두 칸 ☑ — [yolod](daemons/yolod.py)(shm→버스, 유닛) · [llm_client](backend/app/services/llm_client.py)(Claude/로컬 이중 프로바이더, **이 머신 기본 = Ollama qwen2.5:7b, 7b 미만 판단 오류 실측**) · `/vision` 테스트 페이지. 몸통 1단계도 ☑ — [orchestrator.py](backend/app/services/orchestrator.py) 분리수거 최소 루프(검출→판단→task→대기→리셋, 판정은 타임아웃). 남은 것: 오케스트레이터 2단계(YAML 스펙) · G3 판정 · 실물 데모 |
| **cloud-training 5~12** | 데이터 전송 · 체크포인트 회수 · 비용 가드 · 유료 프로바이더 |
| **[robotd-safety](refactor/robotd-safety.md)** | **별도 트랙.** URDF 확보라는 독립 선결 조건. robotd(3b-5)가 서면 언제든 |
| **[manual-control](feature/manual-control.md)** | 웹 조그(1)·MIT 스파이크(2)는 robotd 위에서 **지금 가능**, 병렬 트랙. 중력 보상(3~4)은 **트랙 E(URDF) 의존**, 키네스테틱 녹화(5)는 그 뒤 |
| **[bimanual](feature/bimanual.md)** | G4 구현 — bi 클래스 3개(WeGo repo)로 녹화·추론·파킹 단일화. robotd 변경 0이라 구조 개편과 **안 겹침.** 전제는 하드웨어뿐(팔 4대 + udev 4개 확장) |
| ~~[policy-ui-spec](feature/policy-ui-spec.md)~~ | ☑ **완료** — 정책 하나 추가에 손댈 곳이 **6군데 → 0**. 게이트웨이·wrapper·프론트가 같은 YAML 을 읽는다 |
| **[layout-redesign](feature/layout-redesign.md)** | 좌측 세로 내비 + 상단 상태바. **지금 가능** — 프론트 껍데기 + 요약 API 하나뿐이고 페이지 내용은 안 건드린다. 1단계만으로 메뉴 한계가 풀린다 |
| ~~[llm-integration](feature/llm-integration.md) 1·3~~ | ☑ **완료** — `judge()` 계약 + Anthropic·로컬(vLLM/Ollama) 이중 프로바이더 + 호출별 오버라이드. 남은 것: 2(규칙 프리셋 합류) · 4(플래너) — episode-orchestrator 뒤 |
| 자체 Hub 서버 | 여기까지 온 뒤 판단 |

---

## 5. 병렬 트랙

```
A (구조)    Phase 0 → 1 → 3b(1→2→3→3.5→4→5→6→7→8) ☑ ──→ Phase 4  ← 지금 여기
B (데이터)       phase-annotation 1~3 ☑ ──→ 4~5(episode-editor) ☑ ──→ 6~8 (3b-4 이후)
C (잡일)         Phase 2 (#3~#6, #11) 아무때나
D (학습)         cloud-training 0~2 ──────→ 3~4 (3b-3 이후) ──→ 5~12
E (기구학)          URDF 확보 ─────────────────────────→ robotd-safety (3b-5 이후)
                                                      └→ manual-control 3~5 (중력 보상·키네스테틱 녹화)
```

B·C·D·E는 A를 거의 안 막는다. **E의 URDF 확보는 지금 바로 시작한다** —
외부 자산이라 리드타임이 있고 코드 의존이 없다. 수혜자가 둘로 늘었다
(안전 필터 + [중력 보상](feature/manual-control.md) — 후자는 질량·관성 파라미터까지 필요).

---

## 6. 남은 미결정

| 질문 | 언제까지 | 영향 |
|---|---|---|
| 동시 학습 job 수 상한 | ~~3b-3.5 전~~ → **원격 학습을 실제로 쓰기 전** | 레지스트리는 N개를 담게 만들고 상한만 `MAX_CONCURRENT_JOBS=1` 로 뒀다 — 상수 하나다. SSH 러너가 붙어 **로컬 GPU 를 안 먹는 학습**이 생겼으니 이제 상한을 나눠야 할 이유가 실재한다 |
| ~~E-stop이 무엇을 죽이는가~~ | ~~3b-2 전~~ | ☑ #10 에서 "로봇을 움직이는 것 전부"로 정했다 |
| **녹화도 heartbeat 로 죽일 것인가** | 녹화 유닛화를 다시 볼 때 | 지금은 죽인다(실측 2.5초). 브라우저를 닫으면 **인코딩 중이던 에피소드를 잃는다** — CLAUDE.md 가 반대로 적고 있던 것을 이번에 고쳤다. 안전(아무도 안 보는데 팔이 움직인다) 대 데이터 손실의 맞교환이라 사람이 정할 일이다 |
| URDF를 구할 수 있는가 | 트랙 E 시작 전 | 못 구하면 robotd-safety 트랙 자체가 없다. [manual-control](feature/manual-control.md)의 중력 보상도 같이 막힌다 (관성 파라미터까지 필요) |
| ~~camerad/rsd 합칠 것인가~~ | ~~3b-5 전~~ | ☑ **안 합친다.** D405 의 UVC 질의가 커널 D-state 로 프로세스를 통째로 먹통으로 만든 전례가 결정적이었다. 다만 순수 로직은 공유한다 — 컨트롤 적용 순서가 `piper_cam.controls` 한 벌이다 |

**Phase 0~2 착수를 막는 질문은 없다.**

---

## 7. 요약

> **계약을 먼저 세우고**(Phase 0~1),
> **삭제될 코드 위에 기능을 짓지 않고**(camera-profiles를 뒤로),
> **이음매를 먼저 만들어 재사용한다**(cloud-training 0~2 → SSH 러너 → SystemdRunner).
> **안 겹치는 것은 병렬로**(phase-annotation, URDF, 잡일).

이번 정리에서 바뀐 것: **cloud-training이 데몬 분리를 안 기다린다.**
0~2는 순수 리팩터라 Phase 2로, 3~4는 Redis만 있으면 되므로 3b-3.5로 당겼다.
클라우드 학습이 구조 개편 완료 전에 실제로 돈다.

### 두 번째 원칙의 실측값

"camera-profiles를 뒤로"가 얼마를 아꼈는지 이제 셀 수 있다. 원래 문서의 작업 7개 중
**셋이 코드 한 줄 없이 사라졌고**(트리거 6개 배선, `_open_cap` 의 `cap.set`/`actual_*` 분리,
`_build_cameras_json` 하드코딩 제거), 마이그레이션 1개는 불필요해졌다.
먼저 했다면 그 넷을 만들었다가 3b-4 에서 지웠을 것이다.

같은 논리가 아직 안 끝난 것들에도 걸려 있다 — phase-annotation 6(굽기)은 `#8` 뒤,
cloud-training 3 은 Redis 뒤. **§3 의 금지 순서를 계속 지킨다.**
