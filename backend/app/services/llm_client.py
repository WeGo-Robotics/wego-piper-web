"""외부 LLM 판단 클라이언트 — 프롬프트 → 스키마 검증된 슬롯 (feature/llm-integration.md).

오케스트레이터 스텝이 **직접 호출**한다 (HTTP 자기호출 금지). 반환이 Pydantic
모델인 것이 계약의 전부다 — "LLM 은 슬롯만 출력한다"가 타입 수준에서 강제된다.

## 원칙 (문서가 정한 것)

- **에피소드 경계에서만 부른다.** 30fps 제어 루프에는 절대 안 들어간다.
- **제어 경로에 없다.** wrapper·robotd·브리지 어디에도 이 모듈의 import 가 없어야 한다.
- **실패는 예외 하나로.** 폴백(규칙 기반 기본값·회차 스킵)은 호출자가 정한다 —
  여기서 조용히 기본값을 지어내면 판단 실패가 화면에서 사라진다.
- **자체 재시도 금지.** 429/5xx 지수 백오프는 SDK 가 내장한다 (max_retries 기본 2).

## 프롬프트 캐시 (§4)

캐시는 앞부분(prefix) 일치다. 규칙(system)은 앞에 cache_control 로 고정하고,
매 회차 달라지는 검출 결과는 user 로 뒤에 넣는다. **system 텍스트에 타임스탬프·
회차 번호를 절대 섞지 않는다** — 한 바이트가 캐시 전체를 무효화한다.
적중 확인: 두 번째 호출부터 `stats["cache_read_input_tokens"]` 가 올라야 한다.
"""

import logging
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 진단 카운터 — 브리지 진단 카운터와 같은 노출 방식 (필요 시 라우터가 읽는다)
stats = {
    "calls": 0,
    "failures": 0,
    "refusals": 0,
    "cache_read_input_tokens": 0,
    "input_tokens": 0,
    "output_tokens": 0,
}


class LLMJudgeError(RuntimeError):
    """판단 실패 — 호출자(스텝)가 `on_failure` 폴백을 정한다.

    reason: config | refusal | timeout | api | validation
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


_client = None  # lazy — import 시점에 API 키를 요구하지 않는다


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        # 키는 환경변수 또는 backend/.env 의 ANTHROPIC_API_KEY (settings 별칭이 읽는다).
        # 둘 다 없으면 SDK 기본 해석에 맡긴다 — 호출 시점에 인증 오류로 드러난다.
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    return _client


def reset_client() -> None:
    """테스트·설정 변경용."""
    global _client
    _client = None


async def judge(
    system: str,
    user: str,
    schema: "type[BaseModel]",
    *,
    timeout_s: float = 30.0,
) -> "BaseModel":
    """프롬프트 → 스키마 검증된 슬롯. 검증 실패·타임아웃은 예외로.

    system = 고정 규칙(캐시됨), user = 이번 회차의 가변 입력(검출 목록 등).
    """
    provider = settings.llm_provider
    if provider == "anthropic":
        return await _judge_anthropic(system, user, schema, timeout_s)
    if provider == "openai_compat":
        # 3단계 (온프레미스 vLLM/Ollama) — 요구가 생기면 구현한다.
        # 조용히 anthropic 으로 폴백하지 않는다: 온프레미스를 골랐다는 것은
        # 데이터가 밖으로 나가면 안 된다는 뜻일 수 있다.
        raise LLMJudgeError("config", "openai_compat 프로바이더는 아직 미구현 (llm-integration §6 3단계)")
    raise LLMJudgeError("config", f"알 수 없는 LLM 프로바이더: {provider}")


async def _judge_anthropic(
    system: str, user: str, schema: "type[BaseModel]", timeout_s: float
) -> "BaseModel":
    import anthropic

    client = _get_client()
    stats["calls"] += 1
    try:
        # parse() = 구조화 출력 — 스키마 위반 응답이 아예 생성되지 않고,
        # SDK 가 Pydantic 인스턴스로 검증해 돌려준다. 파싱·재요청 코드가 필요 없다.
        response = await client.with_options(timeout=timeout_s).messages.parse(
            model=settings.llm_model,
            max_tokens=1024,  # 슬롯 출력은 작다
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},  # 규칙은 회차마다 같다 — §4
            }],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except anthropic.APITimeoutError as e:
        stats["failures"] += 1
        raise LLMJudgeError("timeout", str(e)) from e
    except anthropic.APIConnectionError as e:
        stats["failures"] += 1
        raise LLMJudgeError("api", f"연결 실패: {e}") from e
    except anthropic.APIStatusError as e:
        # 429/5xx 는 SDK 가 이미 재시도한 뒤다 — 여기 온 것은 최종 실패
        stats["failures"] += 1
        raise LLMJudgeError("api", f"HTTP {e.status_code}: {e.message}") from e

    usage = getattr(response, "usage", None)
    if usage is not None:
        stats["cache_read_input_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        stats["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        stats["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

    # ⚠ refusal 분기 필수 — content 를 읽기 전에. 안전 분류기가 거부하면
    # HTTP 200 + stop_reason="refusal" 로 온다 (에러가 아니다).
    if response.stop_reason == "refusal":
        stats["refusals"] += 1
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise LLMJudgeError("refusal", f"category={category}")

    parsed = response.parsed_output
    if parsed is None:
        # Claude 경로에서는 사실상 안 온다 (max_tokens 잘림 등 엣지) — 그래도 잡는다
        stats["failures"] += 1
        raise LLMJudgeError("validation", f"stop_reason={response.stop_reason} 에서 파싱 결과 없음")
    return parsed
