"""llm_client — 계약 검증 (feature/llm-integration.md §8 체크리스트의 코드 항목).

네트워크 없이 돈다: 클라이언트를 가짜로 갈아끼우고 계약만 본다 —
`judge()` 는 검증된 Pydantic 인스턴스만 돌려주거나 `LLMJudgeError` 를 던진다.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import settings
from app.services import llm_client
from app.services.llm_client import LLMJudgeError, judge


class JudgeResult(BaseModel):
    target: str
    destination: str


def _response(*, stop_reason="end_turn", parsed=None, category=None, cache_read=0):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        parsed_output=parsed,
        usage=SimpleNamespace(
            cache_read_input_tokens=cache_read, input_tokens=100, output_tokens=20
        ),
    )


class FakeClient:
    """`client.with_options(...).messages.parse(...)` 표면만 흉내낸다."""

    def __init__(self, result):
        self.result = result          # 응답 객체 또는 예외
        self.captured = None          # parse 에 넘어간 kwargs
        self.timeout = None
        self.messages = self

    def with_options(self, *, timeout=None, **_):
        self.timeout = timeout
        return self

    async def parse(self, **kwargs):
        self.captured = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # ⚠ 기기 .env 와 무관하게 — 이 머신 기본이 openai_compat(Ollama)로 바뀌자
    # 기본값에 의존하던 테스트가 **실제 로컬 LLM 으로 나가는** 사고가 났다.
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "claude-opus-5")
    monkeypatch.setattr(settings, "llm_base_url", "")
    for k in llm_client.stats:
        llm_client.stats[k] = 0
    yield
    llm_client.reset_client()


def _install(monkeypatch, result) -> FakeClient:
    fake = FakeClient(result)
    monkeypatch.setattr(llm_client, "_client", fake)
    return fake


def test_schema_roundtrip_returns_validated_instance(monkeypatch):
    expected = JudgeResult(target="pet_bottle", destination="plastic_bin")
    fake = _install(monkeypatch, _response(parsed=expected, cache_read=512))

    out = asyncio.run(judge("규칙", "검출: pet_bottle 0.91", JudgeResult, timeout_s=5.0))

    assert out is expected
    # 프롬프트 조립 계약: 규칙은 system + cache_control(§4), 가변 입력은 user 로 뒤에
    assert fake.captured["output_format"] is JudgeResult
    assert fake.captured["model"] == settings.llm_model
    sys_block = fake.captured["system"][0]
    assert sys_block["text"] == "규칙"
    assert sys_block["cache_control"] == {"type": "ephemeral"}
    assert fake.captured["messages"] == [{"role": "user", "content": "검출: pet_bottle 0.91"}]
    # 타임아웃은 요청별로 — SDK 기본 10분은 데모 루프에 못 쓴다
    assert fake.timeout == 5.0
    assert llm_client.stats["cache_read_input_tokens"] == 512


def test_refusal_raises_before_reading_content(monkeypatch):
    """안전 분류기 거부는 HTTP 200 이다 — content 를 읽기 전에 분기해야 한다."""
    _install(monkeypatch, _response(stop_reason="refusal", category="cyber"))

    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "입력", JudgeResult))
    assert e.value.reason == "refusal"
    assert "cyber" in e.value.detail
    assert llm_client.stats["refusals"] == 1


def test_timeout_maps_to_judge_error(monkeypatch):
    import anthropic

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    _install(monkeypatch, anthropic.APITimeoutError(request=req))

    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "입력", JudgeResult, timeout_s=1.0))
    assert e.value.reason == "timeout"
    assert llm_client.stats["failures"] == 1


def test_api_error_maps_to_judge_error(monkeypatch):
    import anthropic

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(529, request=req)
    _install(monkeypatch, anthropic.APIStatusError("overloaded", response=resp, body=None))

    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "입력", JudgeResult))
    assert e.value.reason == "api"
    assert "529" in e.value.detail


def test_missing_parse_result_is_validation_error(monkeypatch):
    _install(monkeypatch, _response(stop_reason="max_tokens", parsed=None))

    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "입력", JudgeResult))
    assert e.value.reason == "validation"


def test_unknown_provider_is_config_error(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "banana")
    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "입력", JudgeResult))
    assert e.value.reason == "config"


def test_control_path_never_imports_llm_client():
    """§3 방어선: wrapper·데몬 어디에도 LLM 코드가 없다 — 지연·장애가 팔에 닿는 경로 차단."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # 주석·문서의 언급은 허용 — 실제 import 문만 잡는다
    hits = subprocess.run(
        ["grep", "-rlE", r"^\s*(from|import)\s+\S*llm_client",
         str(root / "wrapper"), str(root / "daemons")],
        capture_output=True, text=True,
    ).stdout.strip()
    assert hits == "", f"제어 경로에 llm_client import 발견: {hits}"


# ── openai_compat (로컬 vLLM/Ollama) ──

def _local(monkeypatch, handler):
    """MockTransport 를 꽂고 설정을 로컬 프로바이더로 돌린다."""
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "llm_base_url", "http://local.test/v1")
    monkeypatch.setattr(settings, "llm_model", "qwen-test")
    monkeypatch.setattr(llm_client, "_http_transport", httpx.MockTransport(handler))


def _chat(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def test_local_roundtrip_delegates_schema_but_validates(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat('{"target": "bottle", "destination": "plastic_bin"}'))

    _local(monkeypatch, handler)
    out = asyncio.run(judge("규칙", "검출", JudgeResult))

    assert out == JudgeResult(target="bottle", destination="plastic_bin")
    assert seen["url"] == "http://local.test/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "qwen-test"
    # 서버측 스키마 강제(guided decoding / format) 위임 — 하지만 신뢰하지 않는다
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["properties"].keys() >= {"target", "destination"}
    assert llm_client.stats["output_tokens"] == 10


def test_local_schema_violation_retries_once_with_feedback(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json=_chat("정중한 사과의 말씀과 함께..."))
        return httpx.Response(200, json=_chat('{"target": "cup", "destination": "plastic_bin"}'))

    _local(monkeypatch, handler)
    out = asyncio.run(judge("규칙", "검출", JudgeResult))

    assert out.target == "cup"
    assert len(calls) == 2
    # 재요청에는 위반 내용이 되먹여져야 한다
    retry_msgs = calls[1]["messages"]
    assert retry_msgs[-1]["role"] == "user"
    assert "스키마를 위반" in retry_msgs[-1]["content"]


def test_local_double_violation_is_validation_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat("JSON 이 아님"))

    _local(monkeypatch, handler)
    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "검출", JudgeResult))
    assert e.value.reason == "validation"


def test_local_http_error_maps_to_api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _local(monkeypatch, handler)
    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "검출", JudgeResult))
    assert e.value.reason == "api"
    assert "500" in e.value.detail


def test_local_without_base_url_is_config_error(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "llm_base_url", "")
    with pytest.raises(LLMJudgeError) as e:
        asyncio.run(judge("규칙", "검출", JudgeResult))
    assert e.value.reason == "config"


def test_per_call_provider_and_model_override(monkeypatch):
    """스텝이 "이 판단은 로컬 소형으로"를 고를 수 있어야 한다 — 설정은 anthropic 인 채."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat('{"target": "t", "destination": "d"}'))

    monkeypatch.setattr(settings, "llm_base_url", "http://local.test/v1")
    monkeypatch.setattr(llm_client, "_http_transport", httpx.MockTransport(handler))
    assert settings.llm_provider == "anthropic"  # 기본은 그대로

    out = asyncio.run(judge("규칙", "검출", JudgeResult,
                            provider="openai_compat", model="tiny-local"))
    assert out.target == "t"
    assert seen["body"]["model"] == "tiny-local"


def test_anthropic_per_call_model_override(monkeypatch):
    expected = JudgeResult(target="a", destination="b")
    fake = _install(monkeypatch, _response(parsed=expected))
    asyncio.run(judge("규칙", "검출", JudgeResult, model="claude-haiku-4-5"))
    assert fake.captured["model"] == "claude-haiku-4-5"
