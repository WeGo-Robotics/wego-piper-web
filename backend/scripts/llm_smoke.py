"""llm_client 실왕복 스모크 — llm-integration.md §8 의 검증 항목 세 개를 잰다.

  1. 스키마 왕복: 분리수거 판단이 검증된 슬롯으로 돌아오나
  2. 지연: 판단 1회가 에피소드 경계 예산(수 초) 안인가
  3. 캐시: 2회차부터 cache_read_input_tokens > 0 인가 (규칙 prefix 캐시)

사용:  cd backend && python scripts/llm_smoke.py
전제:  ANTHROPIC_API_KEY (환경변수 또는 backend/.env)
"""

import asyncio
import time

from pydantic import BaseModel

from app.services import llm_client
from app.services.llm_client import judge

RULES = """너는 로봇 팔의 분리수거 판단기다. YOLO 검출 목록을 보고 다음에 집을 물체
하나와 목적지 통을 정한다.

규칙:
- plastic_bin: bottle(페트병), cup(플라스틱 컵)
- can_bin: 캔 종류
- trash_bin: 그 외 쓰레기, 분류 불확실한 것 전부
- 신뢰도가 가장 높은 물체를 우선한다. 사람·가구는 대상이 아니다.
- 집을 물체가 없으면 target="none", destination="none".
"""


class Slots(BaseModel):
    target: str       # 집을 물체 라벨 (검출 목록에 있는 것 또는 "none")
    destination: str  # plastic_bin | can_bin | trash_bin | none
    reason: str       # 한 문장


async def main() -> None:
    detections = "[top 848x480] bottle(0.91) center=(420,260), cup(0.77) center=(180,300)"

    for i in (1, 2):
        before = dict(llm_client.stats)
        t0 = time.monotonic()
        slots = await judge(RULES, detections, Slots, timeout_s=30.0)
        ms = (time.monotonic() - t0) * 1000
        cache_read = llm_client.stats["cache_read_input_tokens"] - before["cache_read_input_tokens"]
        print(f"[{i}회차] {ms:.0f}ms  cache_read={cache_read}  "
              f"target={slots.target} destination={slots.destination}")
        print(f"        reason: {slots.reason}")

    assert llm_client.stats["cache_read_input_tokens"] > 0, (
        "2회차 캐시 미적중 — 프롬프트 조립에 가변 요소가 섞였다 (llm-integration §4)"
    )
    print("OK: 스키마 왕복 + 캐시 적중")


if __name__ == "__main__":
    asyncio.run(main())
