# 신기능 설계 문서

> **[../ROADMAP.md](../ROADMAP.md)** — 이 목록과 [refactor/](../refactor/)를 합친 구현 순서.
> 세 기능이 구조 개편과 맺는 관계가 각각 다르므로 착수 전에 먼저 읽는다.

| 기능 | 요지 | 구조 개편과의 관계 | 문서 |
|---|---|---|---|
| 작업 단계 라벨 | `observation.state`에 페이즈 슬롯 추가 + 자동 라벨러 + 편집 UI | 거의 안 겹침 → **지금 착수 가능** | [01-phase-annotation.md](01-phase-annotation.md) |
| 카메라 프로파일 | 노출·WB 등을 이름 붙인 프로파일로 저장, 실행 전 자동 재적용 | 구조 개편이 **절반을 삭제한다** → **뒤로** | [camera-profiles.md](camera-profiles.md) |
| 클라우드 학습 | 외부 GPU에서 `lerobot-train`, 웹에서 동일하게 제어 | 0~2는 독립 리팩터, 3~4는 Redis만 필요 → **쪼개서 앞당김** | [cloud-training.md](cloud-training.md) |

## 왜 순서가 중요한가 (요약)

- **camera-profiles**의 핵심 트리거 지점인 `_release_all_cameras()`를
  [camera-transport](../refactor/camera-transport.md)가 **통째로 삭제한다.**
  먼저 하면 삭제될 코드 위에 기능을 짓는 셈이다.
- **cloud-training** 0~2단계가 `TrainRunner` **이음매를 만들고**,
  [daemon-split](../refactor/daemon-split.md) 6단계는 거기에 `SystemdRunner`를 하나 더 붙이는 일이다.
  동시가 아니라 **순차**라서 0~2를 앞당길 수 있다.
  단 3단계(job 레지스트리)는 상태를 영속화하므로 **Redis 이후** — 아니면 `jobs.json`을
  만들었다가 다시 옮기게 된다.
- **phase-annotation** 1~3단계는 데이터셋 분석 도구라 장치·프로세스 계층을 안 건드린다.
  병렬로 굴려도 안전하다. 단 6단계(추론 경로)만
  [robot-transport](../refactor/robot-transport.md) 뒤로.

자세한 충돌 지도와 단계별 순서는 [../ROADMAP.md](../ROADMAP.md) 참고.
