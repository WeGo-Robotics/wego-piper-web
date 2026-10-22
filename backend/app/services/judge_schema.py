"""분리수거 판단 슬롯 스키마와 기본 규칙 — `/api/vision/judge` 와 `recycling` 플러그인이 같은 것을 쓴다.

예전에는 오케스트레이터 모듈 안에 있었다. 오케스트레이터는 플러그인으로 이관되며 사라졌지만
(feature/plugins.md §12) 판단 **스키마**는 게이트웨이에 남는다 — LLM 클라이언트와 guided decoding
이 여기 있고, 플러그인은 REST(`vision.judge`)로 부른다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, create_model

# 분리수거 판단 기본 규칙 — 운영 정본은 프리셋 스토어로 갈 예정 (llm-integration §4).
# `/api/vision` 테스트 페이지와 `recycling` 플러그인이 같은 시드를 쓴다.
DEFAULT_RULES = """너는 로봇 팔의 분리수거 판단기다. 객체 검출 목록을 보고 다음에 집을 물체
하나와 목적지 통을 정한다.

규칙:
- plastic_bin: bottle(페트병), cup(플라스틱 컵)
- can_bin: 캔 종류
- trash_bin: 그 외 쓰레기, 분류 불확실한 것 전부
- 신뢰도가 가장 높은 물체를 우선한다. 사람·가구는 대상이 아니다.
- 집을 물체가 없으면 target="none", destination="none".
- reason 은 한국어 한 문장으로 짧게.
"""


class JudgeSlots(BaseModel):
    """분리수거 판단 슬롯.

    ⚠ Field description 은 장식이 아니다 — `model_json_schema()` 를 타고 로컬
    모델의 guided decoding 에 들어간다. 설명이 없으면 소형 모델이 reason 을
    폭주시키는 것을 실측했다 ("검출 없음" 입력에서 1024토큰 잡탕).
    """

    target: str = Field(description="집을 물체의 라벨 (검출 목록에 있는 것) 또는 'none'")
    destination: str = Field(description="plastic_bin | can_bin | trash_bin | none")
    reason: str = Field(description="판단 근거, 한국어 한 문장")


def slots_model(skills: dict[str, str] | None) -> type[BaseModel]:
    """이번 회차에 쓸 판단 스키마.

    ⚠ 허용값을 **런타임에** 넣어야 한다. `Field(description=...)` 이 그대로
    guided decoding 스키마로 들어가므로(위 `JudgeSlots` 주석), 등록된 스킬이
    바뀌면 LLM 이 고를 수 있는 값도 같이 바뀌어야 한다. 목록을 코드에 박으면
    가중치를 하나 추가할 때마다 여기를 고쳐야 하고, 안 고치면 **LLM 이 존재하지
    않는 스킬을 고르거나 있는 스킬을 못 고른다.**
    """
    if not skills:
        return JudgeSlots
    allowed = " | ".join(sorted(skills))
    return create_model(
        "JudgeSlotsWithSkill",
        __base__=JudgeSlots,
        skill=(str, Field(description=f"실행할 스킬 ({allowed})")),
    )
