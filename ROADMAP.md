# 구현 순서 — 리팩터링과 신기능을 안 꼬이게

## 현재 위치 (2026-08-13)

**구조 개편 본선 3b-1~5 완료.** 게이트웨이가 CAN 도 카메라도 직접 열지 않고,
데몬 넷이 장치를 소유하며, 컨테이너에는 GPU + `ipc: host` 만 남았다.

| 단계 | | 상태 |
|---|---|---|
| Phase 0 | 배타 모드 가드 · 추론 파라미터 드리프트 | ☑ |
| Phase 1 | 계약 세우기 (WS·robot_type·관절 수·정책 레지스트리) | ☑ |
| Phase 2 | 저위험 추출 · cloud-training 0~2 · 프리셋 1~3 | ☑ |
| Phase 3a | phase-annotation 1~3 | ☑ |
| **3b-1~3** | 버스 · estopd · 브리지 → Redis | ☑ |
| **3b-3.5** | job 레지스트리 · WS `job_id` | ☑ (4 = SSH 러너는 원격 머신 필요) |
| **3b-4** | shm 전송 — 카메라 · 로봇 | ☑ 실기 확인 |
| **3b-5** | rsd · camerad · robotd | ☑ 실기 확인 |
| **3b-6** | systemd 유닛화 | ◐ 학습·정책서버 ☑ / encoderd·xferd 남음 |
| 3b-7 | 실행별 컨테이너 분리 | ◐ 단일 백엔드 컨테이너는 실기 확인 |
| 3b-8 | 게이트웨이 정리 | ◐ |
| Phase 4 | 깊이맵 · camera-profiles · phase-annotation 4~8 · cloud-training 5~12 | ☐ |

### 바로 이어서 할 것

1. **encoderd · xferd 를 `SystemdProcess` 로** — 정책 서버가 만든 자리를 그대로 따라간다
   ([policy_server_manager.py](backend/app/services/policy_server_manager.py) 의 `_make_process`).
   `encoder_probe.py` 와 `datasets.py` 의 `_upload_pm`·`_edit_pm` 이 아직 `ProcessManager()` 를 직접 만든다
2. **깊이맵 인코딩** — 대역폭 제약은 USB 3 으로 풀렸다(네 스트림 15fps 동시 확인).
   남은 것은 클리핑 범위·컬러맵·무효픽셀·메타 기록 설계 ([camera-transport.md](refactor/camera-transport.md))
3. **실기 체크리스트 B~I** — A(E-stop 일부)·G(카메라)·J(버스)만 닫혔다

### 사람이 해야 하는 것 (sudo·하드웨어)

| | 왜 |
|---|---|
| `loginctl enable-linger $USER` | **지금 꺼져 있다.** 로그아웃 시 학습·개발서버가 통째로 죽고, systemd 러너를 켜도 자식 프로세스로 떨어진다 |
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
| [camera-profiles](feature/camera-profiles.md) | 구조 개편이 **이 기능의 절반을 삭제한다** | **뒤로** |
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
| (1)(2)(4)(5)(7) 저장·매칭·적용 순서 | **남는다** — 다만 camerad 안의 코드로 |

### `_build_cameras_json` — 3중 충돌

camera-transport(`type: "shm"`), camera-profiles(하드코딩 제거), 깊이맵(세그먼트 추가)이
**같은 함수**를 고친다. 한 번에.

### `TrainManager` — cloud-training과 daemon-split이 같은 이음매

cloud-training 0~2가 `TrainRunner` 이음매를 만들고,
daemon-split 6의 systemd 유닛화는 그 이음매에 **`SystemdRunner`를 하나 더 붙이는 일**이다.
동시에 할 일이 아니라 **순차**다 — 그래서 0~2를 앞당길 수 있다 (§3 참고).

단 cloud-training 3(job 레지스트리)은 상태를 영속화하므로, Redis 이전에 하면
`jobs.json`을 만들었다가 Redis로 다시 옮기게 된다. **3은 Redis 이후.**

### WS 계약 — 4중 충돌

[#12](refactor/12-ws-message-contract.md)가 계약을 세우기 전에 cloud-training(`job_id`),
camera-profiles(`camera_profile` 이벤트), phase-annotation(페이즈 텔레메트리)이 각자 추가하면
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
| camera-profiles 트리거 배선 **before** camera-transport | 삭제될 코드 위에 짓는다 |
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

### Phase 3b — 구조 개편 본선 (1~5 ☑ / 6~8 ◐)

| # | 작업 | 비고 |
|---|---|---|
| 1 | ☑ `piper_bus/` 계약 + Redis ([bus/](bus/)) | 데몬 0개 |
| 2 | ☑ [estopd](daemons/estopd.py) — **게이트웨이를 얼려도 팔이 선다** | 최소 크기, 최대 안전 이득, 시범 케이스 |
| 3 | ☑ 브리지 3개 → Redis | 경계는 그대로, 전송만. 주소 3개 → `PIPER_REDIS_URL` 1개 |
| **3.5** | cloud-training **3 ☑**(job 레지스트리 · WS `job_id`) / **4 대기**(SSH 러너) | 3 은 클라우드 없이도 값이 난다 — **서버 재시작에도 학습이 보인다.** 4 는 검증할 원격 머신이 필요하다 |
| 4 | **shm 전송 계층** — 카메라 **1~5 ☑**(추론·녹화 실기 확인) · [로봇](refactor/robot-transport.md) **2~4 ☑**(실기 확인) / 5~6 남음 | 데몬 분리의 전제조건. 깊이맵은 전송 검증 후로 미룸 |
| 5 | **rsd ☑ · camerad ☑ · robotd ☑** (버스 RPC · shm 발행 · 안전층) | **합치지 않았다** — D405 hang 이력. 게이트웨이는 이제 카메라 장치를 전혀 안 연다 |
| 6 | ◐ **학습 러너 ☑ · 정책서버 ☑** ([systemd_process.py](backend/app/services/systemd_process.py)) / encoderd·xferd 남음 | 유닛이 소유자라 게이트웨이 재시작에도 산다. journald 로 **로그까지 이어 읽는다** |
| 7 | ◐ infer / record 컨테이너화 — **단일 백엔드 컨테이너는 실기 확인** (하드웨어 권한·호스트 네트워크 둘 다 제거, GPU + `ipc: host` 만 남음). 실행별로 쪼개는 것은 6단계 이후 | GPU + `ipc: host`만 |
| 8 | ◐ 게이트웨이 정리 — `robot_manager` 995→248줄, `camera_manager` 685→298줄, `arm_bridge` 삭제 | `services/`에 스캐너만 남는다 |

### Phase 4 — 기능 완성

| 기능 | 상태 |
|---|---|
| ~~parameter-presets 4~5~~ | ☑ 완료 — 추론 프리셋 + 프리셋별 성공률 |
| **camera-profiles** | camerad 위에서. 트리거 배선(4단계)이 통째로 빠지고 나머지도 한 프로세스 안에 모인다. **프리셋 스토어 공유** |
| **phase-annotation 4~8** | UI · 굽기 · 추론 경로 |
| **cloud-training 5~12** | 데이터 전송 · 체크포인트 회수 · 비용 가드 · 유료 프로바이더 |
| **[robotd-safety](refactor/robotd-safety.md)** | **별도 트랙.** URDF 확보라는 독립 선결 조건. robotd(3b-5)가 서면 언제든 |
| 자체 Hub 서버 | 여기까지 온 뒤 판단 |

---

## 5. 병렬 트랙

```
A (구조)    Phase 0 → 1 → 3b(1→2→3→3.5→4→5→6→7→8) ────→ Phase 4
B (데이터)       phase-annotation 1~3 ──→ 4~5 ─────────→ 6~8 (3b-4 이후)
C (잡일)         Phase 2 (#3~#6, #11) 아무때나
D (학습)         cloud-training 0~2 ──────→ 3~4 (3b-3 이후) ──→ 5~12
E (기구학)          URDF 확보 ─────────────────────────→ robotd-safety (3b-5 이후)
```

B·C·D·E는 A를 거의 안 막는다. **E의 URDF 확보는 지금 바로 시작한다** —
외부 자산이라 리드타임이 있고 코드 의존이 없다.

---

## 6. 남은 미결정

| 질문 | 언제까지 | 영향 |
|---|---|---|
| 동시 학습 job 수 상한 | ~~3b-3.5 전~~ → **4단계(SSH 러너) 전** | 레지스트리는 N개를 담게 만들고 상한만 `MAX_CONCURRENT_JOBS=1` 로 뒀다 — 클라우드가 붙을 때 상수 하나만 고치면 된다 |
| E-stop이 무엇을 죽이는가 | 3b-2 전 | #10과 estopd 분리가 같이 걸려 있다 |
| URDF를 구할 수 있는가 | 트랙 E 시작 전 | 못 구하면 robotd-safety 트랙 자체가 없다 |
| camerad/rsd 합칠 것인가 | 3b-5 전 | 크래시 격리 vs 단순함 |

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
