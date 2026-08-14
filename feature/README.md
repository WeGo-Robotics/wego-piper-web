# 신기능 설계 문서

> **[../ROADMAP.md](../ROADMAP.md)** — 이 목록과 [refactor/](../refactor/)를 합친 구현 순서.
> 기능마다 구조 개편과 맺는 관계가 다르므로 착수 전에 먼저 읽는다.

| 기능 | 요지 | 구조 개편과의 관계 | 문서 |
|---|---|---|---|
| 작업 단계 라벨 | `observation.state`에 페이즈 슬롯 추가 + 자동 라벨러 + 편집 UI | 거의 안 겹침 → **지금 착수 가능** | [01-phase-annotation.md](01-phase-annotation.md) |
| 카메라 프로파일 | 노출·WB 등을 이름 붙인 프로파일로 저장, **연결 시** 자동 적용 | ☑ **완료.** 개편이 원인 7개 중 4개를 삭제했고 나머지 셋을 닫았다 — 적용 지점은 데몬 `connect()` 한 곳 | [camera-profiles.md](camera-profiles.md) |
| 클라우드 학습 | 외부 GPU에서 `lerobot-train`, 웹에서 동일하게 제어 | 0~2는 독립 리팩터, 3~4는 Redis만 필요 → **쪼개서 앞당김** | [cloud-training.md](cloud-training.md) |
| 파라미터 프리셋 | 추론·학습 설정을 이름 붙여 저장·재사용 | 추론 부분이 **#1 단계 2(PARAM_SPEC)에 의존** | [parameter-presets.md](parameter-presets.md) |
| 데모 시나리오 격차 | [데모 시나리오](../PiPER_AI_데모_시나리오_정리.md) 수행에 필요한 공통 격차 4개(뎁스 경로·에피소드 루프·센서 입력·양팔 수집) 분석 | camerad 없이 가능한 지름길과 미룰 것을 구분 | [demo-scenario-gaps.md](demo-scenario-gaps.md) |
| 에피소드 오케스트레이터 | 스냅샷→판단→실행→리셋 루프. 스텝은 Python, 시퀀스는 YAML 스펙 (ComfyUI 방식, Lua 기각) | 지금은 백엔드 서비스, Redis 이후 버스 클라이언트로 — 프로토콜은 불변 | [episode-orchestrator.md](episode-orchestrator.md) |
| 수동 조작 + 중력 보상 | 추론 없이 웹 조그(있는 shm 경로에 소비자만 추가) + MIT 모드 중력 보상 드래그 → 손으로 끌며 녹화(단팔 데이터 수집) | 조그는 robotd(3b-5) 완성으로 **지금 가능**. 중력 보상은 **트랙 E(URDF) 의존** — URDF 수혜자가 둘이 된다 | [manual-control.md](manual-control.md) |
| 양팔(bimanual) | G4 구현 — 양팔 조립을 wrapper 즉석 코드에서 LeRobot bi 클래스로 내려 녹화·로컬/gRPC 추론·파킹이 한 구현 공유. 핸드오버(우선순위 2)의 블로커 해소 | robotd/shm 이 팔 단위라 **안 겹침** — bi 클래스는 지금 가능, 실질 전제는 하드웨어(팔 4대 + udev) | [bimanual.md](bimanual.md) |
| 정책 UI 스펙 | 모델별 화면 항목을 YAML 로 선언하고 UI 가 그걸 읽어 동적 구성 | ☑ **완료.** 정책 추가에 손댈 곳이 **6군데 → 0** — 파일 하나면 목록·학습 필드·추론 파라미터·wrapper 클래스가 다 붙는다 | [policy-ui-spec.md](policy-ui-spec.md) |
| 외부 LLM 연동 | 분리수거 판단·플래너용 구조화 출력 클라이언트 — 슬롯 JSON 만, 에피소드 경계에서만, 오케스트레이터 스텝으로. 규칙은 프리셋 스토어 합류, 온프레미스는 로컬 Qwen 어댑터 | 백엔드 서비스 + 외부 HTTP 뿐이라 **아예 안 겹침** — 클라이언트(1단계)는 지금 가능, 스텝 합류는 오케스트레이터 1단계 뒤 | [llm-integration.md](llm-integration.md) |

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
