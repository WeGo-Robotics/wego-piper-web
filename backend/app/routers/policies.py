"""정책 목록 API — 프론트가 정책 종류를 스스로 알지 않도록 한다.

이전에는 `TrainingPage.POLICY_TYPES`, `InferencePage` 의 `<option>` 목록,
`RTC_POLICIES` 가 각각 손으로 적혀 있었고 셋 다 달랐다
(refactor/02-policy-registry.md).
"""

from fastapi import APIRouter

from app.core.policies import spec_for_frontend, ui_spec

router = APIRouter(prefix="/api/policies", tags=["policies"])


@router.get("")
async def list_policies():
    """정책 타입 + 라벨 + 학습/추론/RTC 지원 여부."""
    return spec_for_frontend()


@router.get("/{policy_type}/ui")
async def policy_ui(policy_type: str):
    """그 정책의 화면 스펙 — 학습 필드·경고·프로브 tap.

    이전에는 이 내용이 `TrainingPage.tsx` 의 `POLICY_TRAIN_SCHEMAS`(400줄)와
    `EncoderProbePage.tsx` 의 `policyType === '...'` 분기 6개에 있었다.
    그래서 `pi0_fast`·`tdmpc`·`vqbet` 은 백엔드엔 있는데 화면 스키마가 없어
    **골라도 아무 일도 안 일어났다.**
    """
    return ui_spec(policy_type)
