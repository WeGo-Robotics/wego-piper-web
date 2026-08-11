"""활동 상태 API — 무엇이 실행 중이고 무엇이 막혀 있는가.

프론트가 시작 버튼 비활성화에 쓴다. 이전에는 각 페이지가 자기 상태만 알고
교차 조건을 손으로 적었는데(전부 달랐다), 이제 규칙이 백엔드 한 곳에만 있다.
"""

from fastapi import APIRouter

from app.services.exclusivity import snapshot

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
async def get_activity():
    """실행 중인 활동 + 활동별로 막고 있는 것들.

    in-memory boolean 만 읽으므로 자주 불러도 된다.
    """
    return snapshot()
