# 에피소드 오케스트레이터 — 스냅샷→판단→실행→리셋 루프

[demo-scenario-gaps.md](demo-scenario-gaps.md)의 **G2**를 구현하는 설계.
팝콘 데모(Phase 1~2)·분리수거(YOLO→LLM→VLA)·자동 리셋(무인 연속 시연)·장기 플래너가
전부 이 하나의 몸통을 공유한다.

> 설계 원칙: **스텝은 Python, 시퀀스는 데이터.**
> ComfyUI가 그렇다 — 스크립트 언어 없이 "Python 클래스로 등록된 노드 + JSON 그래프 + 순서 실행기"가 전부다.
> 임베디드 Lua 등은 채택하지 않는다 (근거는 §6).

---

## 1. 요구사항 — 루프의 실제 모양

```
스냅샷(1회 Python 호출)
  → 판단(argmax 계산 or YOLO subprocess + LLM HTTP)
  → 지시문 확정(ZMQ task 변경)
  → 실행(종료 조건까지 대기)
  → 판정(로드셀/스캐너 이벤트 or 타임아웃)
  → 기록(eval_log) → 리셋(ZMQ reset) → 반복
```

스텝 종류가 이질적이다:

| 스텝 종류 | 기존 인프라 |
|---|---|
| subprocess 실행 | [process_manager.py](../backend/app/services/process_manager.py) |
| ZMQ 전송 (task/pause/reset) | [zmq_bridge.py](../backend/app/services/zmq_bridge.py), [params.py](../backend/app/routers/params.py) |
| 단발 Python 호출 (스냅샷, argmax) | 신규 — [demo-scenario-gaps.md](demo-scenario-gaps.md) G1·G3 |
| 외부 HTTP (LLM) | 신규 |
| **이벤트 대기** (로드셀 임계치, 스캐너 삑, 타임아웃) | 신규 — G3의 외부 이벤트 리스너 |

그리고 **전 구간이 E-stop으로 즉시 취소 가능**해야 한다. 이건 그래프(DAG) 문제가 아니라
**취소 가능한 async 상태기계** 문제다 — 파이프라인이 선형이므로 그래프 에디터도 필요 없다
(ComfyUI 공수의 대부분이 에디터인데, 우리는 그 부분을 통째로 생략한다).

---

## 2. 스텝 프로토콜 (ComfyUI의 "노드"에 해당)

```python
class Step(Protocol):
    name: str
    async def run(self, ctx: dict) -> dict:   # ctx에서 읽고, 결과를 ctx에 병합
        ...
```

- **`ctx`가 노드 간 배선 역할** — `snapshot` 스텝이 `ctx["depth_path"]`를 넣으면
  `heightmap_argmax` 스텝이 읽는 식. ComfyUI의 출력→입력 연결과 같은 일을 dict 하나로 한다
- 각 스텝은 `asyncio.wait_for` 타임아웃 + 취소 지원.
  **E-stop 트리거 시 현재 스텝 task를 cancel하고 루프 종료**
- 스텝 구현체는 기존 서비스를 **직접 호출**한다 —
  자기 자신에게 HTTP를 치지 말고 `zmq_bridge.send_params()`, `process_manager`를 부른다

## 3. 시나리오 스펙 (ComfyUI의 "그래프 JSON"에 해당)

```yaml
name: popcorn-phase1
loop: {max_episodes: 30, stop_on: estop}
steps:
  - {step: snapshot, camera: "rs:XXXX:depth"}
  - {step: heightmap_argmax, out: target_xy}
  - {step: set_task, template: "scoop at {target_xy}"}
  - {step: wait_done, judge: loadcell, threshold_g: 20, timeout_s: 40}
  - {step: eval_record}
  - {step: reset}
on_failure: {retry: 1, then: reset}
```

"스텝은 Python, 시퀀스는 데이터" 분리가 핵심인 이유:

1. **[cli_mapping.py](../backend/app/core/cli_mapping.py)와 같은 철학** — 선언적 매핑 + 얇은 빌더.
   이 코드베이스가 이미 잘 쓰는 패턴이다
2. **LLM 플래너(시나리오 문서 5장)가 공짜로 따라온다.**
   플래너는 "스텝 시퀀스 JSON을 출력하는 스텝"일 뿐이 된다.
   LLM에게 코드를 생성시키지 않고 **스키마 검증 가능한 JSON**을 생성시킨다 —
   시나리오 문서 3.1.1의 "LLM은 슬롯만 출력, 코드에서 템플릿 조립" 원칙과 일치
3. 표현력이 더 필요해지는 지점(조건 분기, 재시도)은 `on_failure`, `when` 같은
   **선언 필드로 스펙을 넓힌다** — 튜링 완전 언어로 도망가지 않는다

## 4. 기존 인프라와의 배선

| 배선 | 내용 |
|---|---|
| WS 이벤트 | 스텝 전이마다 발행 → 3.1.1 "파이프라인 뷰"(YOLO 박스 → LLM 명령문 → VLA 실행)가 이 이벤트의 시각화. [refactor #12](../refactor/12-ws-message-contract.md) WS 계약에 `orchestrator` 타입으로 합류 |
| 저널링 | 에피소드마다 각 스텝의 `ctx` 입출력을 JSONL로 기록 → "실패 원인 3분됨"(YOLO/LLM/VLA) 교육 콘텐츠가 자동으로 쌓임. [eval_log](../backend/app/routers/eval_log.py)의 상위호환 |
| 배타 가드 | [exclusivity.py](../backend/app/services/exclusivity.py)에 `Activity.ORCHESTRATOR` 등록. **루프 안에서는 프로세스 재시작 대신 ZMQ reset으로 회차를 돌린다** — `require_idle`이 STOPPING도 실행 중으로 보므로([exclusivity.py:86](../backend/app/services/exclusivity.py#L86)) 재시작 방식은 회차마다 파킹+CAN+카메라 재연결 수 초를 문다 |
| 리셋 | 이미 완성된 경로 재사용: `POST /api/params/reset` → [lerobot_wrapper.py:608-639](../wrapper/lerobot_wrapper.py#L608-L639) (큐 초기화 → 2단계 파킹 → 재개) |

---

## 5. 진화 경로

| 단계 | 내용 | 이걸로 되는 것 |
|---|---|---|
| 1 | 백엔드 안의 `EpisodeOrchestrator` 서비스 — async 상태기계, 스텝 5~6개 하드코딩으로 시작 | 팝콘 Phase 1, 자동 리셋(30분 무인) |
| 2 | 스텝 레지스트리 + YAML 시나리오 스펙 분리 | 분리수거(YOLO→LLM), 바코드 재시도 — 스펙 추가로 해결 |
| 3 | 구조 개편 후: 오케스트레이터는 Redis 버스의 클라이언트 하나가 됨 ([daemon-split](../refactor/daemon-split.md)) | 스텝 구현체가 직접 호출 → 버스 명령으로 바뀔 뿐, **프로토콜과 스펙은 그대로** |
| 4 | LLM 플래너 = 스펙을 생성하는 스텝. 스킬 메타데이터(전제/사후 조건)를 스펙 스키마에 포함 | 시나리오 문서 5장. "인터페이스 통일"이 이 시점에 이미 끝나 있음 |

실행기 교체 여지: 재시도/재계획 로직이 복잡해지면 상태기계를 **행동 트리**(py_trees 등,
로보틱스 표준 관용구 — 재시도/폴백/시퀀스가 언어 차원 제공)로 갈아끼울 수 있다.
스텝 프로토콜만 지키면 실행기는 갈아끼우기 쉽다. 시작은 선형 루프 + retry 카운터로 충분하다.

---

## 6. 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| **임베디드 Lua** | Lua가 이기는 환경은 호스트가 C/C++일 때(게임, OpenResty, Redis). 호스트가 이미 Python이라 얻는 게 없다 — 모든 서비스 호출(ZMQ, numpy, pyrealsense)마다 바인딩 수작업, 스택트레이스가 언어 경계에서 끊김, 팀 유지보수 언어 +1. "사용자가 시나리오를 수정"이 목적이면 답은 YAML 스펙이지 튜링 완전 언어가 아니다 |
| **Temporal / Prefect / n8n** | 분산 내구성·수평 확장용 설계. 로봇 한 대의 에피소드 루프에는 운영 비용만 남는다 |
| **ComfyUI식 그래프 에디터** | 파이프라인이 선형이라 DAG 에디터가 불필요. 진행 시각화는 고정 파이프라인 뷰(§4 WS 이벤트)로 충분 |
| **처음부터 행동 트리** | 과함. §5의 교체 여지로만 남긴다 |
