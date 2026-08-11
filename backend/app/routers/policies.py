"""정책 목록 API — 프론트가 정책 종류를 스스로 알지 않도록 한다.

이전에는 `TrainingPage.POLICY_TYPES`, `InferencePage` 의 `<option>` 목록,
`RTC_POLICIES` 가 각각 손으로 적혀 있었고 셋 다 달랐다
(refactor/02-policy-registry.md).
"""

from fastapi import APIRouter

from app.core.policies import spec_for_frontend

router = APIRouter(prefix="/api/policies", tags=["policies"])


@router.get("")
async def list_policies():
    """정책 타입 + 라벨 + 학습/추론/RTC 지원 여부."""
    return spec_for_frontend()
