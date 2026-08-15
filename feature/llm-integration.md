# 외부 LLM 연동 — 판단·계획을 위한 구조화 출력 클라이언트

> **◐ 1단계 완료.** [llm_client.py](../backend/app/services/llm_client.py) —
> `judge()` 계약 + Anthropic 프로바이더(`messages.parse` 구조화 출력) + 설정
> (`PIPER_LLM_PROVIDER/MODEL/BASE_URL`) + refusal/타임아웃/검증 예외
> (`LLMJudgeError.reason`) + 진단 카운터. 스키마 왕복·refusal 분기·프로바이더
> 오류·"제어 경로에 import 없음" 가드까지 pytest 8개
> ([test_llm_client.py](../backend/tests/test_llm_client.py)).
> **◐ 3단계(로컬 프로바이더)도 완료.** `openai_compat` — vLLM/Ollama 의
> `/v1/chat/completions` 에 `response_format: json_schema` 로 스키마를 위임하되
> **신뢰하지 않는다**: Pydantic 검증 + 위반 내용을 되먹인 1회 재요청 (§2 설계
> 그대로). 이 경로에 외부 폴백은 없다 — 온프레미스 선택이 조용히 밖으로 새지
> 않는다. `judge(provider=, model=)` 호출별 오버라이드로 스텝이 "이 판단은
> 로컬 소형, 저 계획은 Claude"를 고를 수 있다. 설정에 `PIPER_LLM_API_KEY`
> (vLLM `--api-key` 용) 추가. MockTransport 테스트 6개.
>
> **로컬 실측 완료** (사용자 공간 Ollama `~/tools/bin/ollama`, 5090 에서 정책
> 서버와 공존 — 합계 12GB/32GB): 분리수거 판단 스모크 기준
> - `qwen2.5:3b`: 스키마 준수 ✓, **판단 오류** (target 에 통 이름을 넣음) — 탈락
> - `qwen2.5:7b`: 스키마 준수 ✓, 판단 정답 ✓, 웜 **326ms** / 콜드 1.5s
>
> 시나리오의 *"이 수준의 판단은 로컬 소형 LLM 으로 충분"* 은 **7b 부터** 성립한다.
> Ollama 는 유닛이 아니라 수동 실행 상태 — 상시 운용이 정해지면 유닛으로 뺀다.
>
> 남은 확인: `ANTHROPIC_API_KEY` 를 `backend/.env` 에 넣은 뒤
> `scripts/llm_smoke.py` (Claude 경로 지연 + 2회차 캐시 적중).

[episode-orchestrator.md](episode-orchestrator.md) 스텝 표의 **"외부 HTTP (LLM) | 신규"** 칸을 채우는 설계.
분리수거 데모(YOLO→LLM→VLA, 시나리오 순위 6)의 "판단" 칸과 장기 플래너(시나리오 5장)가
같은 클라이언트를 쓴다.

> 원칙 셋 — 전부 기존 문서가 이미 정한 것이다:
> 1. **LLM은 슬롯만 출력한다.** 코드가 템플릿을 조립한다 (시나리오 3.1.1,
>    [episode-orchestrator §3](episode-orchestrator.md))
> 2. **에피소드 경계에서만 부른다.** 30fps 제어 루프에는 절대 안 들어간다 —
>    지연 예산이 수 초인 자리(스냅샷→판단)에만
> 3. **스키마 검증 가능한 JSON.** 자유 텍스트·코드 생성 금지 — 검증 실패가
>    파이프라인 앞단에서 잡히게

---

## 1. 어디에 쓰이나 — 시나리오가 정한 자리

| 용도 | 입력 → 출력 | 근거 |
|---|---|---|
| **분리수거 판단** | YOLO 검출 결과(라벨·좌표 목록, **텍스트**) → `{대상, 목적지}` 슬롯 | [시나리오 §3.1](../PiPER_AI_데모_시나리오_정리.md), [demo-scenario-gaps §2 순위 6](demo-scenario-gaps.md) |
| **플래너** (장기) | 작업 지시 → 스텝 시퀀스 JSON ([오케스트레이터 스펙](episode-orchestrator.md) 스키마로 검증) | 시나리오 5장 — "플래너는 스텝 시퀀스 JSON을 출력하는 스텝일 뿐" |
| (확장) 스냅샷 판단 | G3 스냅샷 이미지 → 슬롯 | API가 이미지 입력을 지원하므로 계약 변경 없이 열린다. 단 시나리오가 VLM 통합안을 기각했으므로(아래 §7) **텍스트 우선** |

인식은 YOLO, 판단은 LLM — 이 분업은 시나리오가 확정했다
("좌표 정밀도가 YOLO 대비 낮고 커스텀 클래스 파인튜닝이 번거로움").

---

## 2. 핵심 설계 — 계약 하나, 프로바이더 둘

### 호출 계약

백엔드 서비스 `services/llm_client.py` 하나. 오케스트레이터 스텝이 **직접 호출**한다
(HTTP 자기호출 금지 — [episode-orchestrator §2](episode-orchestrator.md)의 기존 규칙).

```python
async def judge(system: str, user: str, schema: type[BaseModel],
                *, timeout_s: float = 30.0) -> BaseModel:
    """프롬프트 → 스키마 검증된 슬롯. 검증 실패·타임아웃은 예외로 — 폴백은 호출자가 정한다."""
```

반환이 **Pydantic 모델**인 것이 계약의 전부다. 프로바이더가 무엇이든 스키마 검증을
통과한 객체만 스텝에 돌아온다 — "LLM은 슬롯만" 원칙이 타입 수준에서 강제된다.

### 프로바이더 A — Anthropic API (기본)

Claude는 구조화 출력을 API가 보장한다. 파싱·재시도 코드가 필요 없다:

```python
from anthropic import AsyncAnthropic          # backend는 FastAPI/async — async 클라이언트

client = AsyncAnthropic()                     # ANTHROPIC_API_KEY 는 환경에서
response = await client.messages.parse(
    model=settings.llm_model,                 # 기본 "claude-opus-5"
    max_tokens=1024,                          # 슬롯 출력은 작다
    system=[{"type": "text", "text": rules_prompt,
             "cache_control": {"type": "ephemeral"}}],   # §4 캐시
    messages=[{"role": "user", "content": detections_text}],
    output_format=JudgeResult,                # Pydantic 클래스 → 검증된 인스턴스
)
slots = response.parsed_output
```

- SDK가 429/5xx 지수 백오프 재시도를 내장한다(`max_retries` 기본 2) — **자체 재시도 금지**
- 타임아웃은 요청별 `client.with_options(timeout=…)` — SDK 기본 10분은 데모 루프에 못 쓴다
- `stop_reason == "refusal"` 분기 필수 — 안전 분류기가 거부하면 content 를 읽기 전에
  폴백으로 (§3)

### 프로바이더 B — 로컬 OpenAI 호환 엔드포인트 (온프레미스)

시나리오가 못 박았다: *"온프레미스 고객 대응: 이 수준의 판단은 로컬 소형 LLM(Qwen 등)으로 충분"*
([시나리오 3.1](../PiPER_AI_데모_시나리오_정리.md)). vLLM·Ollama 가 여는
`/v1/chat/completions` 를 두 번째 어댑터로 받는다.

- 스키마 강제는 서버 기능에 위임(vLLM guided decoding, Ollama `format`) —
  **하지만 신뢰하지 않는다.** 공통 계층이 Pydantic 검증 + 1회 재요청을 수행한다.
  Claude 경로는 이 검증이 항상 통과하고, 로컬 경로는 여기서 걸러진다
- 오프라인 동작이 목적이므로 이 경로에 외부 폴백은 없다 — 실패 = §3 폴백

### 설정 (기기별)

| 키 | 기본 | 비고 |
|---|---|---|
| `PIPER_LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai_compat` |
| `PIPER_LLM_MODEL` | `claude-opus-5` | 로컬이면 Qwen 계열 모델명 |
| `PIPER_LLM_BASE_URL` | — | `openai_compat` 전용 |
| `ANTHROPIC_API_KEY` | — | `.env` — 코드·저장소에 넣지 않는다 |

전부 기기별 설정이다(온프레미스 여부가 기기 속성) — `settings.config_dir` 경계
(ROADMAP "기기별 설정 분리"). 외부 API 로 데이터를 보내는 것은 ROADMAP 확정 결정
"데이터 반출 허용" 범위 안이다.

---

## 3. 안전·견고성 — 데모가 LLM 때문에 멈추면 안 된다

| 방어선 | 내용 |
|---|---|
| **제어 루프 밖** | 호출 지점은 오케스트레이터 스텝뿐. wrapper·robotd·브리지 어디에도 LLM 코드가 없다 — 지연·장애가 팔에 닿을 경로 자체가 없다 |
| 타임아웃·취소 | 스텝은 이미 `asyncio.wait_for` + 취소 지원이 계약이다 ([episode-orchestrator §2](episode-orchestrator.md)). **E-stop 이 현재 스텝을 cancel 하면 LLM 호출도 함께 죽는다** — 추가 배선 없음 |
| 재시도 | 전송 오류·429·5xx 는 SDK 몫. 스키마 검증 실패는 공통 계층이 1회 재요청 |
| 폴백 | 그래도 실패하면(다운·refusal·타임아웃) 스텝이 정한다: **규칙 기반 기본값**(예: 미분류 → 잡동사니 통) 또는 **회차 스킵 + 기록**. `on_failure` 스펙 필드가 이미 그 자리다 |
| 무인 구동 | 30분 무인 데모에서 LLM 이 죽으면 루프는 폴백으로 계속 돈다 — LLM 은 품질을 올리는 부품이지 단일 장애점이 아니다 |

---

## 4. 규칙 스토어와 프롬프트 캐시

### 규칙은 이름 붙은 설정이다

분리수거 분류 규칙("기름 묻은 종이는 일반쓰레기") 은 코드가 아니라 **운영자가 편집하는
데이터**다. G2 격차표의 "LLM 규칙 스토어"가 이것이다. 저장은
[parameter-presets](parameter-presets.md) 의 **공통 프리셋 스토어에 합류**한다 —
따로 만들면 그 문서가 경고한 "일곱 번째 이름 없는 프리셋"이 된다.

### 캐시 배치 — 규칙은 앞에, 검출 결과는 뒤에

프롬프트 캐시는 **앞부분(prefix) 일치**다. 에피소드마다 반복 호출되는 판단이므로:

```
[system: 규칙 프롬프트 + 출력 스키마 안내]   ← 고정. cache_control 여기
[user:   이번 회차의 YOLO 검출 목록]         ← 매번 다름. 캐시 뒤
```

- 규칙 텍스트에 타임스탬프·회차 번호를 절대 섞지 않는다 — 한 바이트가 캐시 전체를 무효화한다
- 검증: 응답 `usage.cache_read_input_tokens` 가 두 번째 호출부터 0 이 아니어야 한다.
  0 이면 프롬프트 조립에 가변 요소가 섞인 것
- 규칙(프리셋)을 바꾸면 캐시가 새로 써진다 — 정상이고, 회차 반복이 곧 다시 적중시킨다

---

## 5. 기존 인프라와의 배선

| 배선 | 내용 |
|---|---|
| 오케스트레이터 | `llm_judge`·`llm_plan` 스텝 구현체가 `llm_client.judge()` 호출. ctx 에서 읽고 슬롯을 ctx 에 병합 — 스텝 프로토콜 그대로 |
| 저널링 | 스텝 ctx 입출력 JSONL 기록이 이미 설계돼 있다 ([episode-orchestrator §4](episode-orchestrator.md)) — **프롬프트·응답·지연·토큰이 공짜로 남는다.** "실패 원인 3분됨(YOLO/LLM/VLA)" 교육 콘텐츠의 재료 |
| WS 파이프라인 뷰 | `orchestrator` 이벤트에 판단 결과 슬롯 포함 → 3.1.1 "YOLO 박스 → LLM 명령문 → VLA 실행" 뷰. [ws_messages.py](../backend/app/core/ws_messages.py) 계약에 타입으로 |
| eval_log | 판단 슬롯으로 조립된 task 문자열을 평가 레코드에 기록 — G3 의 "eval 에 task 미기록" 확장과 같은 지점 |
| 프리셋 스토어 | 규칙 = 프리셋 (§4). UI 편집도 프리셋 UI 패턴 재사용 |
| 텔레메트리 | 호출 수·실패 수·폴백 발동 수·토큰 사용량 카운터 — 브리지 진단 카운터와 같은 노출 방식 |
| 로봇·데몬 계층 | **변화 0.** 백엔드 서비스 + 외부 HTTP 뿐이라 구조 개편과 아예 안 겹친다 |

---

## 6. 순서

| 단계 | 내용 | 전제 | 비고 |
|---|---|---|---|
| 1 | `llm_client` 서비스 — 계약 + Anthropic 프로바이더 + 설정 + refusal/타임아웃 처리 | 없음 — **지금 가능**, 로봇 없이 테스트 가능 | pytest 로 스키마 왕복 검증 |
| 2 | 규칙 스토어(프리셋 합류) + 분리수거 판단 스텝 + 파이프라인 뷰 이벤트 | [episode-orchestrator](episode-orchestrator.md) 1단계 | 시나리오 순위 6 가동 |
| 3 | 로컬 프로바이더 (OpenAI 호환 어댑터 + 검증 계층) | 1 · 온프레미스 요구 발생 시 | Qwen 스키마 준수율 실측 후 |
| 4 | 플래너 — 오케스트레이터 스펙 스키마를 출력하는 스텝 | 2 · 오케스트레이터 2단계(YAML 스펙) | 시나리오 5장 |
| 5 | (확장) 스냅샷 이미지 판단 | G1 뎁스/스냅샷 경로 | 계약 변경 없음 — content 에 이미지 블록 추가 |

1단계가 하드웨어·구조 개편과 완전히 독립이라 **아무 때나 시작할 수 있다.**

---

## 7. 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| **VLM 하나로 인식+판단 통합** (Qwen-VL 등) | 시나리오가 이미 기각 — 좌표 정밀도가 YOLO 대비 낮고 커스텀 클래스 파인튜닝이 번거롭다. 데모 신뢰성에서 분리 구조 우위 ([시나리오 3.1](../PiPER_AI_데모_시나리오_정리.md)) |
| **자유 텍스트/코드 생성** | LLM 에게 명령문·코드를 통째로 생성시키면 검증 불가. 슬롯 JSON + 코드 템플릿 조립이 확정 원칙 (3.1.1) — 구조화 출력 API 가 이 원칙을 그대로 구현한다 |
| **제어 경로(wrapper) 안에서 호출** | 판단 칸의 소유자는 오케스트레이터다 (G2). wrapper 에 넣으면 추론 프로세스가 외부 API 지연·장애에 물리고, E-stop 취소 경로도 이중이 된다 |
| **LangChain 등 프레임워크** | 호출 표면이 `judge()` 하나다. 추상화 계층이 갚을 게 없고 의존성만 는다 — Lua·Temporal 을 기각한 [episode-orchestrator §6](episode-orchestrator.md)과 같은 잣대 |
| **자체 재시도/백오프 구현** | SDK 가 429/5xx 지수 백오프를 내장한다. 우리가 얹을 것은 타임아웃과 폴백 정책뿐 |
| **Claude 를 OpenAI 호환 셤으로 호출** | 공식 SDK 가 구조화 출력(`messages.parse`)·캐시·타입 예외를 제공한다. 셤은 그걸 다 버리고 어댑터 하나를 아끼는 거래 — 손해다. 호환 어댑터는 **로컬 모델 전용** |

---

## 8. 검증 체크리스트

- [ ] 스키마 왕복: `judge()` 가 검증된 Pydantic 인스턴스만 돌려주나 (오염 입력·검증 실패 재요청 포함)
- [ ] 분리수거 프롬프트 실측: YOLO 라벨 목록 → 분류 정확도 (규칙 프리셋 몇 벌로 비교)
- [ ] 지연 실측: 판단 1회가 에피소드 경계 예산(수 초) 안인가 — 캐시 적중 전/후 각각
- [ ] 캐시 적중: 2회차부터 `usage.cache_read_input_tokens > 0`
- [ ] refusal·타임아웃·네트워크 단절 각각에서 루프가 폴백으로 계속 도나 (30분 무인 시나리오)
- [ ] E-stop → 진행 중 LLM 스텝 취소 확인
- [ ] (3단계) 로컬 Qwen: 스키마 준수율 + 판단 정확도가 데모 기준을 넘나
- [ ] API 키가 저장소·로그·저널 어디에도 안 남나 (저널은 프롬프트만, 키는 헤더라 안 남는 게 정상 — 확인)
