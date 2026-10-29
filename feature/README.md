# 신기능 설계 문서

> **[../ROADMAP.md](../ROADMAP.md)** — 이 목록과 [refactor/](../refactor/)를 합친 구현 순서.
> 기능마다 구조 개편과 맺는 관계가 다르므로 착수 전에 먼저 읽는다.

| 기능 | 요지 | 구조 개편과의 관계 | 문서 |
|---|---|---|---|
| 작업 단계 라벨 | `observation.state`에 페이즈 슬롯 추가 + 자동 라벨러 + 편집 UI | 거의 안 겹침 → **지금 착수 가능** | [01-phase-annotation.md](01-phase-annotation.md) |
| 카메라 프로파일 | 노출·WB 등을 이름 붙인 프로파일로 저장, **연결 시** 자동 적용 | ☑ **완료.** 개편이 원인 7개 중 4개를 삭제했고 나머지 셋을 닫았다 — 적용 지점은 데몬 `connect()` 한 곳 | [camera-profiles.md](camera-profiles.md) |
| 클라우드 학습 | 외부 GPU에서 `lerobot-train`, 웹에서 동일하게 제어 | 0~2는 독립 리팩터, 3~4는 Redis만 필요 → **쪼개서 앞당김** → ☑ 0~4 실기 확인 | [cloud-training.md](cloud-training.md) |
| 외부 학습 서버 (Vast.ai) | 임대 GPU 조달·파기·왕복 전송·비용 가드·도우미 — cloud-training 5~8 구체화 | 검증된 SSH 러너·레지스트리 위에 **프로바이더 층만 얹는다** — 러너 신규 없음. ☐ 09-14 작업 분해 W0~W5 (컨테이너에 ssh 가 없다는 전제 추가) | [vast-training.md](vast-training.md) |
| 파라미터 프리셋 | 추론·학습 설정을 이름 붙여 저장·재사용 | 추론 부분이 **#1 단계 2(PARAM_SPEC)에 의존** | [parameter-presets.md](parameter-presets.md) |
| 데모 시나리오 격차 | [데모 시나리오](../PiPER_AI_데모_시나리오_정리.md) 수행에 필요한 공통 격차 4개(뎁스 경로·에피소드 루프·센서 입력·양팔 수집) 분석 | camerad 없이 가능한 지름길과 미룰 것을 구분 | [demo-scenario-gaps.md](demo-scenario-gaps.md) |
| ~~에피소드 오케스트레이터~~ | ⛔ **폐기 → [플러그인](plugins.md) §12 로 대체** (2026-10-20). 1단계 루프는 번들 플러그인 `recycling` 으로, 2단계(YAML 스텝)는 만들지 않는다 | 외부 API `missions` 표면은 그대로, 구현만 플러그인 위로 | [episode-orchestrator.md](episode-orchestrator.md) |
| 텔레오퍼레이션 | 리더암 릴레이 · 관절 조그 · **엔드포인트(SDK 온보드 IK)** 셋을 한 화면에. 조그는 [manual-control](manual-control.md) 재사용 | shm 명령 경로가 이미 있어 조그는 소비자 하나. 엔드포인트만 `filter_goal` 이 안 걸려 별도 제한이 필요 | [teleoperation.md](teleoperation.md) |
| 수동 조작 + 중력 보상 | 추론 없이 웹 조그(있는 shm 경로에 소비자만 추가) + MIT 모드 중력 보상 드래그 → 손으로 끌며 녹화(단팔 데이터 수집) | 조그는 robotd(3b-5) 완성으로 **지금 가능**. 중력 보상은 **트랙 E(URDF) 의존** — URDF 수혜자가 둘이 된다 | [manual-control.md](manual-control.md) |
| 양팔(bimanual) | G4 구현 — 양팔 조립을 wrapper 즉석 코드에서 LeRobot bi 클래스로 내려 녹화·로컬/gRPC 추론·파킹이 한 구현 공유. 핸드오버(우선순위 2)의 블로커 해소 | robotd/shm 이 팔 단위라 **안 겹침** — bi 클래스는 지금 가능, 실질 전제는 하드웨어(팔 4대 + udev) | [bimanual.md](bimanual.md) |
| 정책 UI 스펙 | 모델별 화면 항목을 YAML 로 선언하고 UI 가 그걸 읽어 동적 구성 | ☑ **완료.** 정책 추가에 손댈 곳이 **6군데 → 0** — 파일 하나면 목록·학습 필드·추론 파라미터·wrapper 클래스가 다 붙는다 | [policy-ui-spec.md](policy-ui-spec.md) |
| 서비스 재시작 | 유닛·게이트웨이 상태와 **낡은 코드 감지**(기동 시각 vs 소스 mtime), 버튼 한 번 재시작 | 데몬 분리의 부작용을 정면으로 다룬다 — 유닛이 늘어날수록 값이 커진다 | [service-restart.md](service-restart.md) |
| 회색 카드 보정 | 자동 WB·노출을 카드에 맞춘 뒤 **잠근다**. 못 믿을 측정(포화·얼룩)은 거절 | 값 저장·적용은 [카메라 프로파일](camera-profiles.md)이 이미 한다 — 보정은 재기만 | [gray-card-calibration.md](gray-card-calibration.md) |
| 조명 변화 감시 | 밝기·색 특징을 버스로 발행(`piper:light:<cam>`, vision 패턴). 소비자: 알람(EWMA 급변, 3×3 그리드로 팔 통과 구분) + 수집·추론 화면 실시간 표시 + 에피소드 뷰어(저장 영상을 **볼 때** 같은 자로 잰다 — 소급 적용) | 측정 수학은 piper_cam(graycard 옆), 발행은 device_watch 루프 — 데몬 신규 없음 | [lighting-watch.md](lighting-watch.md) |
| 깊이 배경 제거 | 깊이 `far_mm` 밖을 **컬러에서** 지워 정책이 배경에 안 물들게. 깊이를 못 읽은 픽셀은 남긴다(D405 는 42%가 그렇다) | rsd 소유라 녹화·추론이 같은 처리를 받는다. `rs.align` 은 켠 장치에서만 | [depth-background-mask.md](depth-background-mask.md) |
| 화면 레이아웃 개편 | 좌측 세로 내비(그룹) + 상단 상태바(도는 활동·로봇/캠 개수). 상단 가로 내비가 한계 — **모델·데이터셋은 이미 자리가 없어 밀려나 있다** | 프론트 껍데기 + 요약 API 하나라 **아예 안 겹침.** 값은 `/api/activity` 와 `device_watch` 가 이미 갖고 있다 | [layout-redesign.md](layout-redesign.md) |
| 외부 LLM 연동 | 분리수거 판단·플래너용 구조화 출력 클라이언트 — 슬롯 JSON 만, 에피소드 경계에서만, 오케스트레이터 스텝으로. 규칙은 프리셋 스토어 합류, 온프레미스는 로컬 Qwen 어댑터 | 백엔드 서비스 + 외부 HTTP 뿐이라 **아예 안 겹침** — 클라이언트(1단계)는 지금 가능, 스텝 합류는 오케스트레이터 1단계 뒤 | [llm-integration.md](llm-integration.md) |
| ACT-S (구 ACT-Aux) | ◐ 1~2단계 ☑. ACT 를 **상속**해 행동 + 작업 단계(stage)를 함께 내는 `act_s` 정책. 기존 ACT 무수정, `lerobot_policy_*` 플러그인 규약으로 꽂는다. 라벨은 phase-annotation 의 FSM 사이드카를 subtask 로 굽는다 | 설치 패키지(`custom_policies/`) 하나 + wrapper 10줄이라 **거의 안 겹침**. 관측 차원을 안 바꿔 refactor #8 과 무관 | [act-s.md](act-s.md) |
| ACT-delta | ◐ 1~4 ☑. 액션을 절대 위치 대신 **관절 이동 변화량**으로 학습 — 영점이 바뀌어도 액션이 안 망가진다. 데이터셋을 `<이름>_delta` 사본으로 바꾸고(정책 코드 무수정), 추론이 체크포인트의 학습 데이터셋을 보고 알아서 절대로 되돌린다. 남은 것: A/B 실기 | 변환기는 굽기 옆, 복원은 wrapper 한 자리 — 정책·학습 코드와 **안 겹침** | [act-delta.md](act-delta.md) |
| ACT-DINOv3 | ◐ 1단계 ☑. ACT 의 비전 백본(ResNet18)만 **DINOv3** 로 바꾼 `act_dinov3`. 토큰 수·나머지 구조는 ACT 그대로라 A/B 의 차이가 백본뿐. 가중치는 승인 없는 timm 사본, 체크포인트는 오프라인으로 열린다. 남은 것: A/B 실기 | ACT-S 와 같은 패키지 — 정책 하나가 늘 뿐 | [act-dinov3.md](act-dinov3.md) |
| ACT 변형 (조사) | ACT 에서 바꿀 수 있는 자리 넷(입력·출력·디코더·추론)과 후보 열 개, **실기 증상으로 고르는 표**, 백본 계열 비교(정규화·크기·지금 되는 것). 권하는 순서: ACT↔DINOv3 A/B → 백본 셋 비교 → 흐름정합 헤드 | 문서뿐 — 코드 변경 없음 | [act-variants.md](act-variants.md) |
| SmolVLA-S | ◐ 1단계·워밍스타트 ☑. 같은 stage 헤드를 SmolVLA 의 VLM prefix 출력에 붙인 `smolvla_s`. 굽기·wrapper 배선은 ACT-S 것 그대로. smolvla 체크포인트·smolvla_base 에서 type 만 바꾼 사본으로 이어 학습(ACT-S ← act 도 같은 길). 남은 것: 실기 A/B | ACT-S 와 같은 패키지 — 정책 하나가 늘 뿐 | [smolvla-s.md](smolvla-s.md) |
| SO-101 180° 거치 | 리더를 반 바퀴 돌려 놓고 잡는 옵션 — **부호 표 하나**로 닫힌다 | [so101d](so101d.md) §5a 관절 매칭의 옵션. POSE 는 크로스 모델에서 막혀 있어 해당 없다 | [so101-flipped.md](so101-flipped.md) |
| 시뮬 환경 | 하드웨어 없이 등록→수집→학습→추론 왕복. `simd` 하나가 세계(팔+카메라)를 든다 | 계약(shm·RPC)을 그대로 재사용해 **데몬 하나가 늘 뿐** — ◐ 1~3단계 ☑, 4단계(E2E 한 바퀴)가 인수 기준 | [sim-env.md](sim-env.md) |
| 시뮬 데이터 출처 | 데이터셋·가중치가 **어느 세계에서 나왔는지** 스스로 말하게 한다 | 물체는 이미 사이드카에 통째로 남는다(실측). 빈 곳은 **메시·세계 버전·가중치로의 전달** 셋 | [sim-provenance.md](sim-provenance.md) |
| 시뮬 탑뷰 높이 | 탑뷰 카메라를 **z 만** 올리고 내린다 — fovy 고정이라 그게 곧 줌이다 | 가상환경 편집의 다음 손잡이. 지금 높이(0.6m)는 테이블 y 의 69% 만 보여 **클릭으로 못 놓는 자리**가 있다. 재컴파일이 아니라 `cam_pos` 한 줄(실측) | [sim-topview-height.md](sim-topview-height.md) |
| 가상환경 편집 | 테이블 위 사물을 사람이 올린다. 정본은 **가상환경 JSON**, MJCF 는 `MjSpec` 으로 굽는다 | sim-env 5단계(물체 무작위화)를 앞당긴 것. 물체가 `cube` 하나로 박힌 자리 다섯을 id 로 일반화하는 일이 1단계 | [sim-scene-editor.md](sim-scene-editor.md) |
| 현장 배포·운영 검토 | 대회·일반 사용자 환경 일곱 항목 — 공유 Vast 계정 · Windows/Mac · 부팅 실패 안내 · 오프라인(USB·로컬 서버) 설치 · 바탕화면 아이콘 · Colab · CI 설치 검증 후 릴리스. 항목마다 지금·조사·권고·규모 | 코드 변경은 작다 — 대부분 배포 스크립트·unitd·프론트 관문 층. 릴리스 절차만 게이트가 하나 는다 | [field-deployment.md](field-deployment.md) |
| 다국어 | 화면 언어 선택(ko·en 시작, **파일 하나 = 언어 하나**). 키는 한국어 원문, 백엔드 문장은 키의 모양으로 맞춘다 | 프론트 + 도구 뿐, 백엔드·데몬 무수정 — **언어를 바꿔도 새로고침하지 않는다**(E-stop heartbeat) | [i18n.md](i18n.md) |
| 카메라 해상도 | ☑ 학습 때 해상도를 모델이 알고(학습 데이터셋 → 체크포인트 표식) 추론 전에 대조 — 비율이 다르면 막고 크기만 다르면 경고. 요청 해상도를 게이트웨이가 저장해 데몬 재시작·USB 재연결 뒤에도 유지 | 게이트웨이 서비스 하나 + 검증·시작 한 줄씩 + 화면. 데몬·wrapper 무수정 | [camera-resolution.md](camera-resolution.md) |
| 스트림 기준 카메라 | 등록한 카메라는 **사용자가 이름 붙이는 shm 스트림**(영문, 기본 장치명+번호, 별칭과 통합)으로 받아간다. 연결된 것만 선택, 스캔은 직접 열어 사진만, 유휴 자동 종료 옵션 | 게이트웨이 레지스트리 + 데몬 세 곳(이름 수신·발행자 pid·probe) + 선택기. wrapper·플러그인 무수정. ◐ 기본 전송 shm·레지스트리·개명·별칭 통합·선택기·데몬 이름 발행·유휴 종료 ☑, 스캔 썸네일(실기 선행) ☐ | [stream-based-cameras.md](stream-based-cameras.md) |
| 플러그인 | 사용자 파이썬 스크립트를 **폴더(매니페스트+main.py)** 로 얹어 웹에서 켜고 끈다. 카메라는 shm, 검출·상태는 버스, 추론 시작은 REST(실행마다 발급하는 범위 토큰, `*` 허용). 자기 화면은 **같은 포트로 프록시 + iframe** — heartbeat 가 브라우저 탭에 살기 때문 | detectord 와 같은 자리(`make_process`)라 데몬·전송·포트 신규 없음. E-stop 대상, 재부착 없음(수동 재시작). **오케스트레이터와 detectord 를 흡수한다**(분리수거 루프 = 번들 `recycling`, 검출기 = 번들 `detector` — 검출기 동시 여러 개가 이걸로 된다). 프론트는 페이지 하나 + 프록시 블록. ☐ 기획 확정 | [plugins.md](plugins.md) |
| 가중치 분석기 | 추론 분석 화면의 탭 — 체크포인트 개요(손실·오프라인 오차·실기 성공을 step 축 하나에), 데이터셋 에피소드 open-loop 평가(페이즈별 오차), 학습 프레임을 복제 도장으로 고친 장면의 반응 | LeRobot 을 부르는 스크립트 하나(`wrapper/offline_eval.py`) + 작업 유닛. GPU 배타에 한 줄. ◐ 1단계 진행 | [weight-analyzer.md](weight-analyzer.md) |
| 에피소드 분석기 | 에피소드 화면 **POS분석** 탭 — 여러 데이터셋의 파지 순간(GRASP 시작)을 말단 3D + 마디별 색 스틱 + 관절별 분포로. 점 → 뷰어 그 프레임 | 페이즈 사이드카·FK 를 **읽기만** 한다 — 새 검출기·데몬 없음. ◐ 1단계 ☑ | [episode-analyzer.md](episode-analyzer.md) |
| 시나리오 설계기 | ◐ 1단계 ☑ (설계 화면·분류 템플릿·작업판 PDF, 수집 → 시나리오 탭). 시나리오 종류별 **템플릿**(분류·조립 먼저 — YAML 선언 + Python 생성기)에 작업판 격자·물체·규칙·실패 비율을 채우면 칸×종류·순서가 고른 시도 목록을 만든다(블록마다 균형 — 중간에 멈춰도 고르다). 분류는 하나씩이 기본·여러 개 연속도. 작업판 A4 분할 PDF(기본은 사람 눈에만 보이는 흐린 판 + 가시성 검사), 수집 중엔 지금/다음 배치·음성·task 자동, 에피소드↔시도 바인딩을 사이드카로. 우연한 실패는 라벨 붙여 남기고 **미결을 학습 시작에서 반드시 묻는다** | 공통 균형기 + 템플릿 + 화면 + 사이드카. 저장한 설정은 프리셋 스토어, 계획은 데이터셋 `meta/` — **새 스토어·데몬 없음**. wrapper 는 저장 순간 바인딩 한 줄, 학습 제외는 `--dataset.episodes` | [scenario-designer.md](scenario-designer.md) |
| 로컬 AI CLI 협력 | ☑ 1~5 + 설정 탭 + 우측 채팅 (꾸러미 · 스킬·훅 · MCP 서버·리소스 · ext 읽기 전용 · 에피소드 묶음 · 설정 → 에이전트 · 웹 채팅은 코드 수정 없이 관찰/조종 모드). 코딩 에이전트(이 머신은 Claude Code)가 로봇 옆에서 일하는 다섯 자리 — 에이전트용 컨텍스트 꾸러미(실패 자리에서 복사 한 번) · 플러그인 루프 스킬·훅 · **MCP 서버 `piper-mcp`(stdio — 에이전트가 죽으면 heartbeat 가 끊겨 팔이 선다, 자기 미션에만 박동)** · ext v1 읽기 전용 · 에피소드 키프레임 묶음. 웹에서 에이전트 띄우기는 보류 | 발판이 이미 둘([plugins §7-2](plugins.md)·[external-api §4](external-api.md)). 전부 REST 위라 데몬·전송·포트 신규 없음, 장치 수준은 여전히 안 연다. 런타임 플래너 폐기 결정은 유지 | [agent-cli.md](agent-cli.md) |
| 학습 로더 진단 | ◐ 1~4 ☑ (원격 CPU 남음). 메트릭 카드가 step 시간을 GPU 연산·데이터 대기로 나눠 보여 주고, 로더가 GPU 를 굶길 때 **workers 권장값**을 실측으로 계산(N·(updt_s+data_s)/updt_s). 다음 학습 폼에도 같은 값을 채운다. 판정은 대기 비율·CPU·설정값으로만 — GPU 병목 처방은 하지 않는다 | 백엔드 순수 함수 하나 + 상태 필드 + 카드·폼 화면. LeRobot·러너 무수정 | [train-loader-workers.md](train-loader-workers.md) |

## 왜 순서가 중요한가 (요약)

- **camera-profiles** ☑ — 핵심 트리거 지점이던 `_release_all_cameras()`를
  [camera-transport](../refactor/camera-transport.md)가 **통째로 삭제했다.**
  뒤로 미룬 판단이 맞았다: 트리거 6개 배선과 해상도 전달 작업이 그대로 증발했고,
  적용 지점은 데몬 `connect()` 한 곳으로 모였다.
- **cloud-training** 0~2단계가 `TrainRunner` **이음매를 만들고**,
  [daemon-split](../refactor/daemon-split.md) 6단계는 거기에 `SystemdRunner`를 하나 더 붙이는 일이다.
  동시가 아니라 **순차**라서 0~2를 앞당길 수 있다.
  단 3단계(job 레지스트리)는 상태를 영속화하므로 **Redis 이후** — 아니면 `jobs.json`을
  만들었다가 다시 옮기게 된다.
- **phase-annotation** 1~3단계는 데이터셋 분석 도구라 장치·프로세스 계층을 안 건드린다.
  병렬로 굴려도 안전하다. 단 6단계(추론 경로)만
  [robot-transport](../refactor/robot-transport.md) 뒤로.
- **parameter-presets** 는 이미 여섯 군데에 흩어진 "이름 없는 프리셋"을 흡수하는 일이라,
  **[camera-profiles](camera-profiles.md)와 반드시 같은 스토어를 써야 한다.**
  따로 만들면 일곱 번째가 된다. 추론 프리셋은 `PARAM_SPEC` 뒤, 학습 프리셋은 먼저 가능.

자세한 충돌 지도와 단계별 순서는 [../ROADMAP.md](../ROADMAP.md) 참고.
